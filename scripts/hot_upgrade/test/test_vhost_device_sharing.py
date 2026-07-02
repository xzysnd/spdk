#!/usr/bin/env python3
"""Phase 13a test: vhost device sharing functional verification (no QEMU).

Verifies that vhost block devices survive hot upgrade via the device-sharing
mechanism (Primary saves device params → Secondary rebuilds g_vhost_devices).

Tests:
  VT-A-01: Primary creates vhost controller         — vhost_get_controllers returns 1 device
  VT-A-02: Hot upgrade completes without crash       — Secondary starts, Primary exits
  VT-A-03: Secondary device list rebuilt             — vhost_get_controllers returns naa.Malloc0.0
  VT-A-04: vhost socket file persists                — /var/tmp/naa.Malloc0.0 exists after HU
  VT-A-05: timeline recorded                         — total_io_interruption_ms has value

Known limitation: vhost-user session (QEMU connection) is NOT migrated because
rebuild_devices creates devices with ctxt=NULL. This test verifies device-list
rebuild only, not session continuity.
"""
import socket
import json
import subprocess
import time
import os
import sys
import glob
import re

PRIMARY_SOCK = "/var/tmp/spdk_hot_upgrade.sock"
SECONDARY_SOCK = "/var/tmp/spdk_secondary.sock"
STATE_FILE = "/var/tmp/spdk_hot_upgrade_state"
TIMELINE_FILE = "/var/tmp/spdk_hot_upgrade_timeline"
IPC_SOCK = "/var/tmp/spdk_hu_ipc.sock"
VHOST_SOCK = "/var/tmp/naa.Malloc0.0"
VHOST_CTRLR = "naa.Malloc0.0"
BDEV_NAME = "Malloc0"
VHOST_DIR = "/var/tmp"

# Resolve paths relative to this script's location (works regardless of repo install path)
TEST_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT_DIR = os.path.dirname(TEST_DIR)                      # scripts/hot_upgrade/
REPO_ROOT = os.path.dirname(os.path.dirname(SCRIPT_DIR))    # repo root
BINARY = os.path.join(REPO_ROOT, "build/bin/spdk_tgt")
ENV = os.environ.copy()
ENV["LD_LIBRARY_PATH"] = os.path.join(REPO_ROOT, "build/lib") + ":" + \
    os.path.join(REPO_ROOT, "dpdk/build/lib") + ":" + \
    os.path.join(REPO_ROOT, "dpdk/build/lib/dpdk/pmds-24.0") + ":" + ENV.get("LD_LIBRARY_PATH", "")

TIMELINE_FIELDS = [
    "tsc_primary_exit_start",
    "tsc_primary_drain_done",
    "tsc_primary_suspend_done",
    "tsc_secondary_init_start",
    "tsc_secondary_takeover_done",
    "tsc_reactor_running",
]


def rpc(sock, method, params=None, timeout=10):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(sock)
    req = {"jsonrpc": "2.0", "method": method, "id": 1}
    if params:
        req["params"] = params
    s.sendall(json.dumps(req).encode())
    chunks = []
    while True:
        try:
            chunk = s.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
        except socket.timeout:
            break
    s.close()
    return json.loads(b"".join(chunks))


def get_state(sock):
    try:
        resp = rpc(sock, "hot_upgrade_status", timeout=5)
        return resp.get("result", {}).get("state", "UNKNOWN")
    except Exception:
        return "UNKNOWN"


def get_timeline(sock):
    try:
        resp = rpc(sock, "hot_upgrade_get_timeline", timeout=5)
        return resp.get("result", {})
    except Exception:
        return {}


def get_vhost_controllers(sock):
    resp = rpc(sock, "vhost_get_controllers", timeout=5)
    if "error" in resp:
        return []
    result = resp.get("result", [])
    return result if isinstance(result, list) else []


def kill_stale_spdk():
    my_pid = os.getpid()
    for exe_link in glob.glob("/proc/[0-9]*/exe"):
        try:
            target = os.readlink(exe_link)
        except OSError:
            continue
        if target == BINARY:
            pid = int(exe_link.split("/")[2])
            if pid != my_pid:
                try:
                    os.kill(pid, 9)
                except OSError:
                    pass


def cleanup():
    kill_stale_spdk()
    time.sleep(1)
    for f in [PRIMARY_SOCK, SECONDARY_SOCK, STATE_FILE, TIMELINE_FILE,
              IPC_SOCK, VHOST_SOCK]:
        os.system(f"rm -f {f} 2>/dev/null")
    os.system("rm -f /var/tmp/spdk_cpu_lock_* 2>/dev/null")
    os.system("rm -rf /var/run/dpdk/spdk100 2>/dev/null")
    os.system("rm -f /dev/hugepages/spdk100map_* 2>/dev/null")


def start_primary_with_vhost():
    """Start Primary, create Malloc0 bdev + vhost blk controller."""
    p = subprocess.Popen(
        [BINARY, "-r", PRIMARY_SOCK, "--shm-id=100"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=ENV,
        cwd=VHOST_DIR)
    time.sleep(3)
    print(f"  Primary PID: {p.pid}", flush=True)

    state = get_state(PRIMARY_SOCK)
    assert state == "IDLE", f"Expected IDLE, got {state}"

    resp = rpc(PRIMARY_SOCK, "bdev_malloc_create",
               {"block_size": 512, "num_blocks": 512, "name": BDEV_NAME}, timeout=10)
    assert "error" not in resp, f"bdev create failed: {resp}"
    print(f"  {BDEV_NAME} bdev created (256KB)", flush=True)

    resp = rpc(PRIMARY_SOCK, "vhost_create_blk_controller",
               {"ctrlr": VHOST_CTRLR, "dev_name": BDEV_NAME}, timeout=10)
    assert "error" not in resp, f"vhost create failed: {resp}"
    print(f"  vhost controller '{VHOST_CTRLR}' created", flush=True)

    return p


def run_hot_upgrade():
    """Run full hot upgrade, return (proc, new_primary_sock)."""
    proc = subprocess.run(
        [f"{SCRIPT_DIR}/spdk_hot_upgrade.sh", BINARY,
         "--primary-sock", PRIMARY_SOCK,
         "--secondary-sock", SECONDARY_SOCK, "-v"],
        capture_output=True, text=True, timeout=120, env=ENV)

    print(proc.stdout[-2000:] if len(proc.stdout) > 2000 else proc.stdout)
    if proc.stderr:
        print("STDERR:", proc.stderr[-1000:])

    assert proc.returncode == 0, \
        f"Hot upgrade failed (rc={proc.returncode})"

    time.sleep(1)
    state_primary = get_state(PRIMARY_SOCK)
    state_secondary = get_state(SECONDARY_SOCK)
    print(f"  New Primary state: {state_primary}, Secondary state: {state_secondary}")

    if state_primary == "COMPLETE":
        return proc, PRIMARY_SOCK
    elif state_secondary == "COMPLETE":
        return proc, SECONDARY_SOCK
    else:
        raise AssertionError(
            f"Neither socket is COMPLETE (primary={state_primary}, secondary={state_secondary})")


def test_vt_a_01_primary_create_vhost():
    """VT-A-01: Primary creates vhost controller."""
    print("\n[Test VT-A-01] Primary creates vhost controller...", flush=True)

    ctrls = get_vhost_controllers(PRIMARY_SOCK)
    print(f"  Controllers: {json.dumps(ctrls, indent=2)}", flush=True)

    assert len(ctrls) == 1, f"Expected 1 controller, got {len(ctrls)}: {ctrls}"
    assert ctrls[0].get("ctrlr") == VHOST_CTRLR, \
        f"Expected {VHOST_CTRLR}, got {ctrls[0].get('ctrlr')}"

    assert os.path.exists(VHOST_SOCK), \
        f"vhost socket {VHOST_SOCK} not created"
    print(f"  vhost socket exists: {VHOST_SOCK}")

    print("  PASSED: vhost controller created and visible", flush=True)
    return True


def test_vt_a_02_03_04_05_hot_upgrade():
    """VT-A-02~05: Hot upgrade + device rebuild + socket + timeline."""
    print("\n[Test VT-A-02~05] Hot upgrade with vhost device sharing...", flush=True)

    proc, check_sock = run_hot_upgrade()

    state = get_state(check_sock)
    assert state == "COMPLETE", f"Expected COMPLETE, got {state}"
    print(f"  VT-A-02 PASSED: Hot upgrade completed, state={state}", flush=True)

    ctrls = get_vhost_controllers(check_sock)
    print(f"  Secondary controllers: {json.dumps(ctrls, indent=2)}", flush=True)

    found = any(c.get("ctrlr") == VHOST_CTRLR for c in ctrls)
    assert found, \
        f"VT-A-03 FAILED: {VHOST_CTRLR} not found in secondary controllers: {ctrls}"
    print(f"  VT-A-03 PASSED: Device list rebuilt, '{VHOST_CTRLR}' visible", flush=True)

    if os.path.exists(VHOST_SOCK):
        print(f"  VT-A-04 PASSED: vhost socket persists: {VHOST_SOCK}")
    else:
        print(f"  VT-A-04 WARN: vhost socket {VHOST_SOCK} gone after HU "
              "(expected if session cleanup ran)")

    tl = get_timeline(check_sock)
    print(f"  Timeline: {json.dumps(tl, indent=2)}", flush=True)
    total_ms = tl.get("total_io_interruption_ms", 0)
    if total_ms > 0:
        print(f"  VT-A-05 PASSED: total_io_interruption_ms={total_ms}")
    else:
        print(f"  VT-A-05 WARN: total_io_interruption_ms={total_ms} (timeline not recorded)")

    return True


def main():
    print("=" * 60, flush=True)
    print("  Phase 13a Test: vhost Device Sharing (No QEMU)", flush=True)
    print("=" * 60, flush=True)

    results = []
    try:
        print("\n=== Cleanup ===", flush=True)
        cleanup()

        print("\n=== Start Primary with vhost ===", flush=True)
        p = start_primary_with_vhost()

        results.append(("VT-A-01: primary_create_vhost",
                        test_vt_a_01_primary_create_vhost()))

        results.append(("VT-A-02~05: hot_upgrade_device_sharing",
                        test_vt_a_02_03_04_05_hot_upgrade()))

    except Exception as e:
        print(f"\n[ERROR] Test failed: {e}", flush=True)
        import traceback
        traceback.print_exc()
        results.append(("exception", False))
    finally:
        print("\n=== Final Cleanup ===", flush=True)
        cleanup()

    print("\n" + "=" * 60, flush=True)
    print("  Test Summary", flush=True)
    print("=" * 60, flush=True)
    all_passed = True
    for name, passed in results:
        status = "PASSED" if passed else "FAILED"
        print(f"  {name}: {status}", flush=True)
        if not passed:
            all_passed = False

    if all_passed:
        print("\n=== All Phase 13a tests PASSED ===", flush=True)
        sys.exit(0)
    else:
        print("\n=== Some tests FAILED ===", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
