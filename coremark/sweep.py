#!/usr/bin/env python3
"""CoreMark parameter sweep: patch cheshire_pkg.sv, synth, run, collect score.

Usage:
    sweep.py                         # run all pending (not yet scored) sweep points
    sweep.py --dry-run               # print plan (shows saved/synth per point) and exit
    sweep.py --rerun                 # re-run all points; reuse saved bitstreams, synth the rest
    sweep.py --only baseline         # run a single point
    sweep.py --from lsu_pipe_0_0     # resume from a specific point (skip earlier ones)
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
RESULTS_CSV = os.path.join(_SCRIPT_DIR, "results", "sweep_results.csv")
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
    "Cva6FpgaEn": 0,
    "Cva6NrScoreboardEntries": 8,
    "Cva6BHTHistory": 3,
    "Cva6IcacheByteSize": 16384,
    "Cva6IcacheSetAssoc": 4,
    "Cva6IcacheLineWidth": 128,
    "Cva6DcacheByteSize": 32768,
    "Cva6DcacheSetAssoc": 8,
    "Cva6DcacheLineWidth": 128,
    "Cva6InstrTlbEntries": 16,
    "Cva6DataTlbEntries": 16,
}

# Sweep table: (run_name, {param: override_value, ...})
# Empty dict = baseline (all DEFAULTS).
SWEEPS = [
    # Baseline
    ("baseline", {}),
    # RAS depth (default 2)
    ("ras_4", {"Cva6RASDepth": 4}),
    ("ras_8", {"Cva6RASDepth": 8}),
    ("ras_16", {"Cva6RASDepth": 16}),
    # BTB entries (default 32)
    ("btb_0", {"Cva6BTBEntries": 0}),
    ("btb_8", {"Cva6BTBEntries": 8}),
    ("btb_16", {"Cva6BTBEntries": 16}),
    ("btb_64", {"Cva6BTBEntries": 64}),
    ("btb_128", {"Cva6BTBEntries": 128}),
    ("btb_256", {"Cva6BTBEntries": 256}),
    # BHT entries (default 128)
    ("bht_32", {"Cva6BHTEntries": 32}),
    ("bht_64", {"Cva6BHTEntries": 64}),
    ("bht_256", {"Cva6BHTEntries": 256}),
    ("bht_512", {"Cva6BHTEntries": 512}),
    # BHT history bits (default 3)
    ("bht_hist_1", {"Cva6BHTHistory": 1}),
    ("bht_hist_2", {"Cva6BHTHistory": 2}),
    ("bht_hist_4", {"Cva6BHTHistory": 4}),
    ("bht_hist_5", {"Cva6BHTHistory": 5}),
    ("bht_hist_7", {"Cva6BHTHistory": 7}),
    # LSU pipelining (default load=1, store=0)
    ("lsu_pipe_0_0", {"Cva6NrLoadPipeRegs": 0, "Cva6NrStorePipeRegs": 0}),
    ("lsu_pipe_2_1", {"Cva6NrLoadPipeRegs": 2, "Cva6NrStorePipeRegs": 1}),
    ("store_pipe_1", {"Cva6NrStorePipeRegs": 1}),
    ("store_pipe_2", {"Cva6NrStorePipeRegs": 2}),
    # LSU buffer sizing
    ("lsu_buf_8_12", {"Cva6NrLoadBufEntries": 8, "Cva6MaxOutstandingStores": 12}),
    # Compressed instructions
    # ("rvc_off", {"Cva6RVC": 0}),
    # Bitmanip
    ("rvb", {"Cva6RVB": 1}),
    # Scoreboard / reorder buffer depth (default 8)
    ("sb_2", {"Cva6NrScoreboardEntries": 2}),
    ("sb_4", {"Cva6NrScoreboardEntries": 4}),
    ("sb_16", {"Cva6NrScoreboardEntries": 16}),
    ("sb_32", {"Cva6NrScoreboardEntries": 32}),
    # D-cache size (default 32768)
    ("dcache_256", {"Cva6DcacheByteSize": 256}),
    ("dcache_512", {"Cva6DcacheByteSize": 512}),
    ("dcache_1k", {"Cva6DcacheByteSize": 1024}),
    ("dcache_2k", {"Cva6DcacheByteSize": 2048}),
    ("dcache_4k", {"Cva6DcacheByteSize": 4096}),
    ("dcache_8k", {"Cva6DcacheByteSize": 8192}),
    ("dcache_16k", {"Cva6DcacheByteSize": 16384}),
    # I-cache size (default 16384)
    ("icache_128", {"Cva6IcacheByteSize": 128}),
    ("icache_256", {"Cva6IcacheByteSize": 256}),
    ("icache_512", {"Cva6IcacheByteSize": 512}),
    ("icache_1k", {"Cva6IcacheByteSize": 1024}),
    ("icache_2k", {"Cva6IcacheByteSize": 2048}),
    ("icache_4k", {"Cva6IcacheByteSize": 4096}),
    ("icache_8k", {"Cva6IcacheByteSize": 8192}),
    ("icache_32k", {"Cva6IcacheByteSize": 32768}),
    # Cache line width in bits (default 128)
    ("dcache_line_256", {"Cva6DcacheLineWidth": 256}),
    ("icache_line_64", {"Cva6IcacheLineWidth": 64}),
    ("icache_line_256", {"Cva6IcacheLineWidth": 256}),
    # Cache set-associativity (default icache=4, dcache=8)
    ("icache_assoc_1", {"Cva6IcacheSetAssoc": 1}),
    ("icache_assoc_2", {"Cva6IcacheSetAssoc": 2}),
    ("icache_assoc_8", {"Cva6IcacheSetAssoc": 8}),
    ("dcache_assoc_2", {"Cva6DcacheSetAssoc": 2}),
    ("dcache_assoc_4", {"Cva6DcacheSetAssoc": 4}),
    ("dcache_assoc_16", {"Cva6DcacheSetAssoc": 16}),
    # I$ and D$ at minimum valid size
    ("min_l1_cache", {"Cva6IcacheByteSize": 128, "Cva6DcacheByteSize": 256}),
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
    os.makedirs(os.path.dirname(csv_path), exist_ok=True)
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
        "--rerun",
        action="store_true",
        help="Re-run all points; reuse saved bitstream if available, else synthesize",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print sweep plan and exit"
    )
    args = parser.parse_args()

    if args.dry_run:
        print(f"{'Name':<25}  {'Bitstream':<10}  Overrides")
        print("-" * 70)
        for name, overrides in SWEEPS:
            has_bit = "saved" if os.path.exists(saved_bitstream(name)) else "synth"
            print(f"{name:<25}  {has_bit:<10}  {overrides or '(baseline)'}")
        return

    done = set() if args.rerun else load_done(args.results)
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

        if os.path.exists(saved_bitstream(name)):
            do_synth, bstream = False, name
        else:
            do_synth, bstream = True, None

        try:
            score, mhz_score, log_dir = run_one(
                name, overrides, _fpga.COREMARK_GPT, do_synth, bstream
            )
            append_result(args.results, name, overrides, score, mhz_score, log_dir)
        except Exception as e:
            append_result(args.results, name, overrides, None, None, "", str(e))
            _fpga.log("ERROR", f"{name}: {e}")


if __name__ == "__main__":
    main()
