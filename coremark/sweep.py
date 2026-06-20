#!/usr/bin/env python3
"""CoreMark parameter sweep: patch cheshire_pkg.sv, synth, run, collect score.

Usage:
    sweep.py                         # run all pending sweep points
    sweep.py --dry-run               # print plan and exit
    sweep.py --only baseline         # run a single point
    sweep.py --from lsu_pipe_0_0     # resume from a specific point
    sweep.py --no-synth              # skip synthesis (reuse last bitstream)
    sweep.py --bitstream ras_8       # restore saved bitstream before programming
"""

import contextlib
import csv
import datetime
import json
import os
import re
import shutil
import sys
import argparse

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_SCRIPT_DIR)
sys.path.insert(0, _SCRIPT_DIR)
import fpga as _fpga

CHESHIRE_PKG = os.path.join(_REPO_ROOT, "hw", "cheshire_pkg.sv")
RESULTS_CSV = os.path.join(_SCRIPT_DIR, "sweep_results.csv")
BITSTREAM_DIR = os.path.join(_SCRIPT_DIR, "bitstreams")
BITSTREAM_DEFAULT = os.path.join(
    _REPO_ROOT, "target", "xilinx", "out", f"cheshire.{_fpga.FPGA_CLASS}.bit"
)

# Must stay in sync with cheshire_pkg.sv DefaultCfg.
DEFAULTS = {
    "Cva6RASDepth": 2,
    "Cva6BTBEntries": 32,
    "Cva6BHTEntries": 128,
    "Cva6NrLoadPipeRegs": 1,
    "Cva6NrStorePipeRegs": 0,
    "Cva6NrLoadBufEntries": 2,
    "Cva6MaxOutstandingStores": 7,
    "Cva6RVC": 1,
    "Cva6RVB": 0,
}

# Sweep table: (run_name, {param: override_value, ...})
# Empty dict = baseline (all DEFAULTS).
SWEEPS = [
    # Baseline
    ("baseline", {}),
    # Branch predictor
    ("ras_1", {"Cva6RASDepth": 1}),
    ("ras_4", {"Cva6RASDepth": 4}),
    ("ras_8", {"Cva6RASDepth": 8}),
    ("btb_0", {"Cva6BTBEntries": 0}),
    ("btb_16", {"Cva6BTBEntries": 16}),
    ("btb_64", {"Cva6BTBEntries": 64}),
    ("btb_128", {"Cva6BTBEntries": 128}),
    ("bht_32", {"Cva6BHTEntries": 32}),
    ("bht_256", {"Cva6BHTEntries": 256}),
    # Compressed ISA
    ("no_rvc", {"Cva6RVC": 0}),
    # LSU pipelining
    ("lsu_pipe_0_0", {"Cva6NrLoadPipeRegs": 0, "Cva6NrStorePipeRegs": 0}),
    ("lsu_pipe_2_1", {"Cva6NrLoadPipeRegs": 2, "Cva6NrStorePipeRegs": 1}),
    # LSU buffer sizing
    ("lsu_buf_1_4", {"Cva6NrLoadBufEntries": 1, "Cva6MaxOutstandingStores": 4}),
    ("lsu_buf_8_12", {"Cva6NrLoadBufEntries": 8, "Cva6MaxOutstandingStores": 12}),
    # Bitmanip
    ("rvb", {"Cva6RVB": 1}),
    (
        "combined_opt",
        {
            "Cva6RASDepth": 8,
            "Cva6BTBEntries": 128,
            "Cva6NrLoadBufEntries": 8,
            "Cva6MaxOutstandingStores": 12,
        },
    ),
]


def _patch_pkg(text: str, params: dict) -> str:
    """Apply integer value overrides to DefaultCfg entries in cheshire_pkg.sv source text."""
    for name, val in params.items():
        text, n = re.subn(
            rf"(\b{re.escape(name)}\s*:\s*)\d+",
            lambda m, v=val: m.group(1) + str(v),
            text,
        )
        if n == 0:
            raise ValueError(f"Parameter '{name}' not found in cheshire_pkg.sv")
    return text


@contextlib.contextmanager
def patched_pkg(overrides: dict):
    """Context manager: patch DefaultCfg in cheshire_pkg.sv, restore on exit."""
    if not overrides:
        yield
        return
    original = open(CHESHIRE_PKG).read()
    try:
        patched = _patch_pkg(original, overrides)
        with open(CHESHIRE_PKG, "w") as f:
            f.write(patched)
        yield
    finally:
        with open(CHESHIRE_PKG, "w") as f:
            f.write(original)


def parse_score(uart_log: str) -> tuple[float | None, float | None]:
    """Parse (iter_per_sec, coremark_per_mhz) from a UART log file; either may be None."""
    text = open(uart_log).read()
    m = re.search(r"CoreMark 1\.0\s*:\s*([0-9.]+)", text)
    iter_per_sec = float(m.group(1)) if m else None
    if iter_per_sec is None:
        m = re.search(r"Iterations/Sec\s*:\s*([0-9.]+)", text)
        iter_per_sec = float(m.group(1)) if m else None
    m = re.search(r"CoreMark/MHz\s*:\s*([0-9.]+)", text)
    mhz_score = float(m.group(1)) if m else None
    return iter_per_sec, mhz_score


def load_done(csv_path: str) -> set[str]:
    """Return the set of run names that already have a score in the results CSV."""
    if not os.path.exists(csv_path):
        return set()
    with open(csv_path) as f:
        return {row["name"] for row in csv.DictReader(f) if row.get("score")}


def append_result(csv_path, name, overrides, score, mhz_score, log_dir, error=""):
    """Append one sweep result row to the CSV, writing the header if the file is new."""
    exists = os.path.exists(csv_path)
    with open(csv_path, "a", newline="") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(
                [
                    "name",
                    "score",
                    "score_mhz",
                    "log_dir",
                    "error",
                    "timestamp",
                    "params",
                ]
            )
        w.writerow(
            [
                name,
                "" if score is None else f"{score:.6f}",
                "" if mhz_score is None else f"{mhz_score:.6f}",
                log_dir,
                error if error else "false",
                datetime.datetime.now().isoformat(timespec="seconds"),
                json.dumps({**DEFAULTS, **overrides}),
            ]
        )


def saved_bitstream(name: str) -> str:
    """Return the path where the bitstream for a given run name is saved."""
    return os.path.join(BITSTREAM_DIR, f"{name}.bit")


def save_bitstream(name: str) -> None:
    """Copy the freshly built bitstream from the build dir to the per-run archive."""
    os.makedirs(BITSTREAM_DIR, exist_ok=True)
    dest = saved_bitstream(name)
    shutil.copy2(BITSTREAM_DEFAULT, dest)
    _fpga.log("OK", f"Bitstream saved: {dest}")


def restore_bitstream(name: str) -> None:
    """Copy a previously saved bitstream back into the build dir before programming."""
    src = saved_bitstream(name)
    if not os.path.exists(src):
        raise FileNotFoundError(f"No saved bitstream for '{name}': {src}")
    shutil.copy2(src, BITSTREAM_DEFAULT)
    _fpga.log("OK", f"Bitstream restored from: {src}")


def run_one(
    name: str,
    overrides: dict,
    gpt: str,
    do_synth: bool,
    bitstream_name: str | None = None,
) -> tuple[float | None, float | None, str]:
    """Synthesise (if requested), flash GPT, program bitstream, and collect CoreMark score."""
    _fpga._open_log()
    log_dir = _fpga._run_dir
    _fpga.log("INFO", f"=== Sweep point: {name}  overrides={overrides} ===")

    board = uart_proc = None
    score = mhz_score = None
    try:
        if do_synth:
            if os.path.exists(BITSTREAM_DEFAULT):
                os.remove(BITSTREAM_DEFAULT)
            with patched_pkg(overrides):
                _fpga.synth()
            save_bitstream(name)
        elif bitstream_name:
            restore_bitstream(bitstream_name)
        else:
            _fpga.log(
                "WARN", "--no-synth: reusing whatever bitstream is in the build dir"
            )

        _fpga.release_existing(_fpga.SSH_HOST)
        board = _fpga.book(_fpga.SSH_HOST, _fpga.FPGA_CLASS, _fpga.FPGA_LEASE)
        tcp_port, jtag_sn, _ = _fpga.start_hwserver(_fpga.SSH_HOST, board)
        uart = _fpga.find_uart(_fpga.SSH_HOST, board)
        uart_proc, uart_log = _fpga.start_uart_log(_fpga.SSH_HOST, uart)

        _fpga.flash(board, tcp_port, jtag_sn, gpt)
        _fpga.program(board, tcp_port, jtag_sn)
        _fpga.watch_uart(uart_log)

        score, mhz_score = parse_score(uart_log)
        if score is None:
            _fpga.log("WARN", "Could not parse CoreMark score from UART log")
        else:
            _fpga.log(
                "OK",
                (
                    f"CoreMark score: {score:.6f} iter/s  |  {mhz_score:.6f} /MHz"
                    if mhz_score is not None
                    else f"CoreMark score: {score:.6f} iter/s"
                ),
            )

    finally:
        if uart_proc:
            uart_proc.terminate()
        if board:
            _fpga.release(_fpga.SSH_HOST, board)

    return score, mhz_score, log_dir


def main():
    parser = argparse.ArgumentParser(description="CoreMark parameter sweep")
    parser.add_argument(
        "--results",
        default=RESULTS_CSV,
        metavar="CSV",
        help="Output CSV file (appended)",
    )
    parser.add_argument(
        "--only", metavar="NAME", help="Run only this single sweep point"
    )
    parser.add_argument(
        "--from",
        dest="from_name",
        metavar="NAME",
        help="Start from this sweep point (skip earlier ones)",
    )
    parser.add_argument(
        "--no-synth",
        action="store_true",
        help="Skip synthesis, reuse existing bitstream",
    )
    parser.add_argument(
        "--bitstream",
        metavar="NAME",
        help="With --no-synth: restore this saved bitstream before programming",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print sweep plan and exit"
    )
    args = parser.parse_args()

    if args.dry_run:
        print(f"{'Name':<25}  Overrides")
        print("-" * 60)
        for name, overrides in SWEEPS:
            cfg = {**DEFAULTS, **overrides}
            print(f"{name:<25}  {overrides or '(baseline)'}")
        return

    done = load_done(args.results)
    started = args.from_name is None

    for name, overrides in SWEEPS:
        if not started:
            if name == args.from_name:
                started = True
            else:
                continue
        if args.only and name != args.only:
            continue
        if name in done:
            _fpga.log("INFO", f"Skipping {name} - already in {args.results}")
            continue

        try:
            score, mhz_score, log_dir = run_one(
                name, overrides, _fpga.COREMARK_GPT, not args.no_synth, args.bitstream
            )
            append_result(args.results, name, overrides, score, mhz_score, log_dir)
        except Exception as e:
            append_result(args.results, name, overrides, None, None, "", str(e))
            _fpga.log("ERROR", f"{name}: {e}")


if __name__ == "__main__":
    main()
