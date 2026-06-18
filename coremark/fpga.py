#!/usr/bin/env python3
"""FPGA run automation — book board, optionally program bitstream, load ELF via GDB, capture UART."""

import argparse
import datetime
import os
import re
import subprocess
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_SCRIPT_DIR)

SSH_HOST = "weissenstein"
FPGA_CLASS = "genesys2"
FPGA_LEASE = "1h"
BAUD = 115200
LOG_DIR = os.path.join(_SCRIPT_DIR, "logs")
COREMARK_ELF = os.path.join(_REPO_ROOT, "sw/tests/coremark.spm.elf")
OPENOCD_TCL = "cheshire-target.tcl"

_C = {
    "reset": "\x1b[0m",
    "bold": "\x1b[1m",
    "cyan": "\x1b[36m",
    "green": "\x1b[32m",
    "yellow": "\x1b[33m",
    "red": "\x1b[31m",
}

_logfile = None
_run_ts = None


def _open_log():
    global _logfile, _run_ts
    os.makedirs(LOG_DIR, exist_ok=True)
    _run_ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(LOG_DIR, f"{_run_ts}_fpga.log")
    _logfile = open(log_path, "w")
    log("INFO", f"Log opened: {log_path}")


def log(level, msg):
    colors = {
        "INFO": _C["cyan"],
        "OK": _C["green"],
        "WARN": _C["yellow"],
        "ERROR": _C["red"],
    }
    c = colors.get(level, "")
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(
        f"{c}{_C['bold']}[{level}]{_C['reset']} {msg}",
        file=sys.stderr if level == "ERROR" else sys.stdout,
    )
    if _logfile:
        _logfile.write(f"{ts} [{level}] {msg}\n")
        _logfile.flush()


def ssh(host, cmd):
    log("INFO", f"ssh {host}: {cmd}")
    return subprocess.run(
        ["ssh", "-o", "LogLevel=QUIET", host, cmd],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )


def strip_ansi(text):
    return re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text)


def release_existing(host):
    """Release any FPGA sessions already held by this user before booking a new one."""
    result = ssh(host, "fpga sessions")
    boards = [
        line.strip()
        for line in strip_ansi(result.stdout).splitlines()
        if line
        and not line[0].isspace()
        and " " not in line.strip()
        and ":" not in line
    ]
    if not boards:
        log("INFO", "No existing sessions to release")
        return
    for board in boards:
        board = board.strip()
        log("WARN", f"Releasing existing session: {board}")
        release(host, board)


def start_hwserver(host, board):
    """Start the Xilinx hardware server for the board and return (tcp_port, jtag_sn, openocd_port)."""
    log("INFO", f"Starting hardware server for {board}")
    result = subprocess.run(
        [
            "ssh",
            "-tt",
            "-o",
            "LogLevel=QUIET",
            host,
            f"source /etc/profile; fpga run -b {board}",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    # Lines 2-5 (1-indexed): strip ANSI, split on ':', take 3rd field
    lines = strip_ansi(result.stdout).splitlines()[1:5]
    params = [line.split(":")[2].strip() for line in lines]
    # params[0]=TCP port, params[1]=Vivado GDB, params[2]=JTAG serial, params[3]=OpenOCD GDB
    tcp_port, jtag_sn, openocd_port = params[0], params[2], params[3]
    log(
        "OK",
        f"Hardware server: port={tcp_port}  jtag={jtag_sn}  openocd={openocd_port}",
    )
    return tcp_port, jtag_sn, openocd_port


def find_uart(host, board):
    """Find the /dev/ttyUSB* path assigned to the booked board in fpga sessions output."""
    result = ssh(host, "fpga sessions")
    lines = strip_ansi(result.stdout).splitlines()
    in_our_board = False
    for line in lines:
        stripped = line.strip()
        if stripped == board:
            in_our_board = True
        elif in_our_board and stripped.startswith("/dev/ttyUSB"):
            log("OK", f"UART: {stripped}")
            return stripped
    raise RuntimeError(f"No UART found for {board} in fpga sessions")


def start_uart_log(host, uart):
    """Open a background ssh+cat process that streams UART output into a timestamped log file."""
    path = os.path.join(LOG_DIR, f"{_run_ts}_uart.log")
    # Kill any stale process still holding the device from a previous run
    subprocess.run(
        ["ssh", "-o", "LogLevel=QUIET", host, f"fuser -k {uart} 2>/dev/null; true"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    uart_file = open(path, "w")
    proc = subprocess.Popen(
        [
            "ssh",
            "-o",
            "LogLevel=QUIET",
            host,
            f"stty -F {uart} {BAUD} raw -echo && cat {uart}",
        ],
        stdout=uart_file,
        stderr=uart_file,  # log stty/cat errors into the uart log
    )
    log("OK", f"UART log started: {path}")
    return proc, path


def watch_uart(uart_log, match="CoreMark finish", timeout=300):
    """Tail the UART log file and block until match appears or timeout (seconds) is reached."""
    import time

    log("INFO", f"Waiting for '{match}' (timeout {timeout}s)...")
    deadline = time.time() + timeout
    with open(uart_log, "r") as f:
        while time.time() < deadline:
            line = f.readline()
            if line:
                if match in line:
                    log("OK", f"Matched: {line.strip()}")
                    return
            else:
                time.sleep(0.1)
    raise RuntimeError(f"Timeout waiting for '{match}'")


def start_openocd(host, board):
    """Launch OpenOCD on the remote host in the background using the user-board config."""
    import getpass

    user = getpass.getuser()
    openocd_cfg = f"{user}-{board}-openocd.tcl"
    log("INFO", f"Starting OpenOCD: {openocd_cfg} + {OPENOCD_TCL}")
    proc = subprocess.Popen(
        [
            "ssh",
            "-o",
            "LogLevel=QUIET",
            host,
            f"openocd -f {openocd_cfg} -f {OPENOCD_TCL}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc


def start_gdb(host, openocd_port, elf):
    """Connect GDB to OpenOCD, load the ELF, and continue execution in the background."""
    import time

    time.sleep(2)  # give OpenOCD time to start
    log("INFO", f"GDB: load + continue via {host}:{openocd_port}")
    return subprocess.Popen(
        [
            "riscv64-unknown-elf-gdb",
            "-batch",
            "-ex",
            f"target extended-remote {host}:{openocd_port}",
            "-ex",
            "load",
            "-ex",
            "continue",
            elf,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def synth():
    """Build the genesys2 bitstream locally via make."""
    log("INFO", f"Synthesising bitstream for {FPGA_CLASS} (this takes a while)...")
    subprocess.run(
        ["make", f"chs-xilinx-{FPGA_CLASS}"],
        check=True,
        cwd=_REPO_ROOT,
        stdout=_logfile,
        stderr=_logfile,
    )
    log("OK", "Synthesis complete")


def program(board, tcp_port, jtag_sn):
    """Program the FPGA bitstream via the Xilinx hardware server using make."""
    log("INFO", f"Programming {board} via {SSH_HOST}:{tcp_port}")
    subprocess.run(
        [
            "make",
            f"chs-xilinx-program-{FPGA_CLASS}",
            f"CHS_XILINX_HWS_URL={SSH_HOST}:{tcp_port}",
            f"CHS_XILINX_HWS_PATH_{FPGA_CLASS}={{xilinx_tcf/*/{jtag_sn}*}}",
        ],
        check=True,
        cwd=_REPO_ROOT,
        stdout=_logfile,
        stderr=_logfile,
    )
    log("OK", f"Programming complete")


def book(host, fpga_class, lease):
    result = ssh(host, f"fpga book --class {fpga_class} --time {lease}")
    board = strip_ansi(result.stdout).strip().split()[2]
    log("OK", f"Booked {board}")
    return board


def release(host, board):
    try:
        ssh(host, f"fpga release -b {board}")
        log("OK", f"Released {board}")
    except subprocess.CalledProcessError as e:
        log("WARN", f"Release failed for {board}: {e.stderr.strip()}")


def main():
    """Book an FPGA, optionally program it, load an ELF, and wait for UART output."""
    parser = argparse.ArgumentParser(description="FPGA run automation")
    parser.add_argument(
        "--synth", action="store_true", help="Generate bitstream before flashing"
    )
    parser.add_argument(
        "--program", action="store_true", help="Program the FPGA bitstream"
    )
    parser.add_argument(
        "--binary",
        default=COREMARK_ELF,
        metavar="ELF",
        help=f"ELF to load via GDB (default: {COREMARK_ELF})",
    )
    parser.add_argument(
        "--match",
        default="CoreMark finish",
        metavar="STRING",
        help="String to wait for in UART output (default: 'CoreMark finish')",
    )
    args = parser.parse_args()

    _open_log()
    board = None
    uart_proc = None
    openocd_proc = None
    gdb_proc = None
    try:
        if args.synth:
            synth()
        release_existing(SSH_HOST)
        board = book(SSH_HOST, FPGA_CLASS, FPGA_LEASE)
        tcp_port, jtag_sn, openocd_port = start_hwserver(SSH_HOST, board)
        uart = find_uart(SSH_HOST, board)
        uart_proc, uart_log = start_uart_log(SSH_HOST, uart)
        log("INFO", f"UART output -> {uart_log}")
        if args.program:
            program(board, tcp_port, jtag_sn)
        openocd_proc = start_openocd(SSH_HOST, board)
        gdb_proc = start_gdb(SSH_HOST, openocd_port, args.binary)
        watch_uart(uart_log, match=args.match)

    except Exception as e:
        log("ERROR", str(e))
        sys.exit(1)
    finally:
        if gdb_proc:
            gdb_proc.terminate()
        if openocd_proc:
            openocd_proc.terminate()
        if uart_proc:
            uart_proc.terminate()
        if board:
            release(SSH_HOST, board)


if __name__ == "__main__":
    main()
