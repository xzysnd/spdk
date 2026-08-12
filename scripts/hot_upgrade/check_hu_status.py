#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 SPDK Hot Upgrade Contributors.
#
# Check SPDK hot upgrade status by reading the shared state file.
# This script can be run by ANY external process — no SPDK or DPDK
# environment required. It reads /var/tmp/spdk_hot_upgrade_state
# which is mmapped by the SPDK Primary/Secondary process and updated
# in real-time via msync(MS_ASYNC).
#
# Usage:
#   python3 check_hu_status.py          # One-shot check
#   python3 check_hu_status.py --watch  # Poll every 0.5s
#
# Exit codes:
#   0 — SPDK is NOT in hot upgrade (IDLE or no state file)
#   1 — SPDK IS in hot upgrade (any non-IDLE state)
#   2 — Error reading state file

import argparse
import mmap
import os
import struct
import sys
import time

STATE_FILE = "/var/tmp/spdk_hot_upgrade_state"
STATE_MAGIC = 0x5350444B  # "SPDK" in ASCII

# Must match enum spdk_hot_upgrade_state in include/spdk/hot_upgrade.h
STATE_NAMES = {
    0: "IDLE",
    1: "SECONDARY_PRE_INIT",
    2: "SECONDARY_PRE_INIT_DONE",
    3: "PRIMARY_DRAINING",
    4: "PRIMARY_SUSPENDED",
    5: "SECONDARY_TAKEOVER",
    6: "COMPLETE",
    7: "FAILED",
}

# State field offsets in struct spdk_hot_upgrade_shared_state
# We only need the last 3 fields which are at the end of the struct.
# Instead of computing the exact offset (which depends on all preceding
# fields and padding), we read the hu_state_name string from a known
# offset near the end. But the simplest approach: read the entire file
# and search for the state name pattern.
#
# struct layout (tail):
#   ... primary_rpc_addr[108]
#   ... secondary_rpc_addr[108]
#   uint32_t hu_state;           # 4 bytes
#   uint32_t hu_state_pid;       # 4 bytes
#   char     hu_state_name[32];  # 32 bytes
#
# Total tail = 4 + 4 + 32 = 40 bytes


def read_status():
    """Read hot upgrade status from the shared state file.

    Returns (state_name, state_pid) or (None, None) if file doesn't exist.
    """
    if not os.path.exists(STATE_FILE):
        return None, None

    try:
        f = open(STATE_FILE, "rb")
        f.seek(0, 2)
        file_size = f.tell()
        f.seek(0)

        # The last 40 bytes are: hu_state(4) + hu_state_pid(4) + hu_state_name(32)
        tail_size = 40
        if file_size < tail_size:
            f.close()
            return None, None

        f.seek(file_size - tail_size)
        data = f.read(tail_size)
        f.close()

        hu_state, hu_state_pid = struct.unpack_from("<II", data, 0)
        # Read state name (null-terminated string in 32-byte buffer)
        raw_name = data[8:40]
        name_end = raw_name.find(b'\x00')
        if name_end >= 0:
            hu_state_name = raw_name[:name_end].decode('ascii', errors='replace')
        else:
            hu_state_name = raw_name.decode('ascii', errors='replace')

        # Validate against known states
        if hu_state in STATE_NAMES:
            if not hu_state_name:
                hu_state_name = STATE_NAMES[hu_state]
        else:
            hu_state_name = f"UNKNOWN({hu_state})"

        return hu_state_name, hu_state_pid

    except Exception as e:
        print(f"Error reading state file: {e}", file=sys.stderr)
        return None, None


def main():
    parser = argparse.ArgumentParser(description="Check SPDK hot upgrade status")
    parser.add_argument("--watch", action="store_true",
                        help="Poll every 0.5 seconds")
    parser.add_argument("--state-file", default=STATE_FILE,
                        help=f"Path to state file (default: {STATE_FILE})")
    args = parser.parse_args()

    global STATE_FILE
    STATE_FILE = args.state_file

    if args.watch:
        prev_state = None
        try:
            while True:
                state_name, pid = read_status()
                if state_name is None:
                    if prev_state != "NO_FILE":
                        print(f"[{time.strftime('%H:%M:%S')}] No state file — SPDK not running or no hot upgrade")
                        prev_state = "NO_FILE"
                elif state_name != prev_state:
                    print(f"[{time.strftime('%H:%M:%S')}] State: {state_name} (PID: {pid})")
                    prev_state = state_name
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\nStopped.")
    else:
        state_name, pid = read_status()
        if state_name is None:
            print("No hot upgrade in progress (state file not found)")
            sys.exit(0)
        elif state_name == "IDLE":
            print(f"SPDK hot upgrade status: IDLE (PID: {pid})")
            sys.exit(0)
        elif state_name == "COMPLETE":
            print(f"SPDK hot upgrade status: COMPLETE (PID: {pid})")
            sys.exit(0)
        else:
            print(f"SPDK hot upgrade IN PROGRESS: {state_name} (PID: {pid})")
            sys.exit(1)


if __name__ == "__main__":
    main()
