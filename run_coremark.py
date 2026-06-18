#!/usr/bin/env python3
"""CoreMark FPGA automation: book → tty → flash → collect → release."""

import re
import subprocess
import sys

SSH_HOST   = "weissenstein"
FPGA_CLASS = "genesys2"
FPGA_LEASE = "1h"


def ssh(host, cmd):
    print(f"[ssh] {host}: {cmd}")
    return subprocess.run(
        ["ssh", "-o", "LogLevel=QUIET", host, cmd],
        check=True, text=True, capture_output=True,
    )


def strip_ansi(text):
    return re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)


def book(host, fpga_class, lease):
    result = ssh(host, f"fpga book --class {fpga_class} --time {lease}")
    # Output: "Booked board genesys-01 <timestamp>" — field 3 (1-indexed) is the board name
    board = strip_ansi(result.stdout).strip().split()[2]
    print(f"[book] Booked {board}")
    return board


def release(host, board):
    ssh(host, f"fpga release -b {board}")
    print(f"[release] Released {board}")


def main():
    board = None
    try:
        board = book(SSH_HOST, FPGA_CLASS, FPGA_LEASE)

    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        if board:
            release(SSH_HOST, board)


if __name__ == "__main__":
    main()
