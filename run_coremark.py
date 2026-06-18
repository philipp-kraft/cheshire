#!/usr/bin/env python3
"""CoreMark FPGA automation"""

import re
import subprocess
import sys

SSH_HOST   = "weissenstein"
FPGA_CLASS = "genesys2"
FPGA_LEASE = "1h"

# ANSI colors
_C = {
    "reset":   "\x1b[0m",
    "bold":    "\x1b[1m",
    "cyan":    "\x1b[36m",
    "green":   "\x1b[32m",
    "yellow":  "\x1b[33m",
    "red":     "\x1b[31m",
}


def log(level, msg):
    colors = {"INFO": _C["cyan"], "OK": _C["green"], "WARN": _C["yellow"], "ERROR": _C["red"]}
    c = colors.get(level, "")
    print(f"{c}{_C['bold']}[{level}]{_C['reset']} {msg}", file=sys.stderr if level == "ERROR" else sys.stdout)


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
    return re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)


def book(host, fpga_class, lease):
    result = ssh(host, f"fpga book --class {fpga_class} --time {lease}")
    board = strip_ansi(result.stdout).strip().split()[2]
    log("OK", f"Booked {board}")
    return board


def release(host, board):
    ssh(host, f"fpga release -b {board}")
    log("OK", f"Released {board}")


def main():
    board = None
    try:
        board = book(SSH_HOST, FPGA_CLASS, FPGA_LEASE)

    except Exception as e:
        log("ERROR", str(e))
        sys.exit(1)
    finally:
        if board:
            release(SSH_HOST, board)


if __name__ == "__main__":
    main()
