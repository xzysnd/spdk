#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
# Test external process visibility of hot upgrade state.
# Runs a background poller while executing the hot upgrade test.

import os
import struct
import subprocess
import sys
import threading
import time

STATE_FILE = "/var/tmp/spdk_hot_upgrade_state"
SPDK_DIR = "/root/xzy/spdk"
TEST_SCRIPT = f"{SPDK_DIR}/scripts/hot_upgrade/test/test_tsc_timeline_and_io_interruption.py"

states_seen = []
lock = threading.Lock()


def poll_state(stop_event):
    """Poll the state file every 10ms."""
    while not stop_event.is_set():
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "rb") as f:
                    f.seek(0, 2)
                    sz = f.tell()
                    if sz >= 40:
                        f.seek(sz - 40)
                        data = f.read(40)
                        state, pid = struct.unpack_from("<II", data, 0)
                        name = data[8:40].split(b'\x00')[0].decode('ascii', errors='replace')
                        entry = (time.time(), name, pid)
                        with lock:
                            # Only add if different from last
                            if not states_seen or states_seen[-1][1] != name:
                                states_seen.append(entry)
                                print(f"  [{time.strftime('%H:%M:%S')}] State: {name} (PID: {pid})")
            except Exception:
                pass
        time.sleep(0.01)


def main():
    print("=== External Status Visibility Test ===")
    print(f"Polling {STATE_FILE} every 10ms while running hot upgrade test...")
    print()

    stop_event = threading.Event()
    poller = threading.Thread(target=poll_state, args=(stop_event,))
    poller.start()

    # Run the hot upgrade test
    result = subprocess.run(
        ["python3", TEST_SCRIPT],
        capture_output=True, text=True, timeout=120
    )

    # Let poller run a bit more to catch final state
    time.sleep(2)
    stop_event.set()
    poller.join(timeout=5)

    print()
    print("=== States captured by external poller ===")
    with lock:
        for ts, name, pid in states_seen:
            print(f"  {time.strftime('%H:%M:%S', time.localtime(ts))} — {name} (PID: {pid})")

    print(f"\nTotal unique state transitions: {len(states_seen)}")

    # Check test results
    print("\n=== Hot upgrade test result ===")
    for line in result.stdout.splitlines():
        if "PASSED" in line or "FAILED" in line:
            print(f"  {line}")

    # Verify we saw hot upgrade states
    state_names = [s[1] for s in states_seen]
    saw_hot_upgrade = any(s in ("PRIMARY_SUSPENDED", "SECONDARY_TAKEOVER", "COMPLETE") for s in state_names)

    print(f"\n=== Summary ===")
    print(f"  External process saw hot upgrade states: {'YES' if saw_hot_upgrade else 'NO'}")
    if saw_hot_upgrade:
        print("  PASSED: External process can detect hot upgrade via state file")
        sys.exit(0)
    else:
        print("  FAILED: External process did not see hot upgrade states")
        sys.exit(1)


if __name__ == "__main__":
    main()
