#!/usr/bin/env python3
"""
SW-211457 Bug Reproduction Test
Race condition: Internal error when rollback+commit from 2 CLI sessions.

The bug: When Session X commits a rollback that deletes IRB+EVPN config,
and Session Y simultaneously enters configure mode, does rollback 1 to
re-create the same config, and commits - the commit fails with:
  Internal error in callback handler: ...TypeError: 'NoneType' object
  cannot be interpreted as an integer
"""

import paramiko
import threading
import time
import sys
import re

DEVICE_IP = "100.64.3.239"
USER = "dnroot"
PASS = "dnroot"

class DNOSSession:
    def __init__(self, name):
        self.name = name
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.shell = None
        self.log = []

    def connect(self):
        self.log.append(f"[{self.name}] Connecting to {DEVICE_IP}...")
        self.client.connect(DEVICE_IP, username=USER, password=PASS,
                          look_for_keys=False, allow_agent=False, timeout=30)
        self.shell = self.client.invoke_shell(width=200, height=50)
        self.shell.settimeout(60)
        time.sleep(2)
        initial = self._read_all()
        self.log.append(f"[{self.name}] Connected. Initial: {initial[-200:]}")

    def send_cmd(self, cmd, wait=3):
        self.log.append(f"[{self.name}] >>> {cmd}")
        self.shell.send(cmd + "\n")
        time.sleep(wait)
        output = self._read_all()
        self.log.append(f"[{self.name}] <<< {output}")
        return output

    def _read_all(self):
        output = ""
        while self.shell.recv_ready():
            chunk = self.shell.recv(65536).decode('utf-8', errors='replace')
            output += chunk
            time.sleep(0.2)
        return output

    def close(self):
        try:
            self.shell.close()
            self.client.close()
        except:
            pass

    def print_log(self):
        for line in self.log:
            print(line)


def run_test(attempt_num):
    print(f"\n{'='*60}")
    print(f"  ATTEMPT {attempt_num}")
    print(f"{'='*60}")

    session_x = DNOSSession("Session-X")
    session_y = DNOSSession("Session-Y")
    sync_event = threading.Event()
    results = {"x": None, "y": None, "error": None}

    def session_x_flow():
        try:
            session_x.connect()

            # Step 1: Enter configure mode
            session_x.send_cmd("configure", wait=2)

            # Step 2: Configure IRB + EVPN + VRF
            session_x.send_cmd("interfaces irb66 admin-state enabled ipv4-address 101.1.0.254/24", wait=1)
            session_x.send_cmd("network-services evpn instance kfkfkf protocols bgp 1 export-l2vpn-evpn route-target 100:1", wait=1)
            session_x.send_cmd("network-services evpn instance kfkfkf protocols bgp 1 import-l2vpn-evpn route-target 100:1", wait=1)
            session_x.send_cmd("network-services evpn instance kfkfkf protocols bgp 1 route-distinguisher 65145:1", wait=1)
            session_x.send_cmd("network-services evpn instance kfkfkf counters service-counters enabled", wait=1)
            session_x.send_cmd("network-services evpn instance kfkfkf router-interface irb66", wait=1)
            session_x.send_cmd("network-services vrf instance alpha interface irb66", wait=1)

            # Step 3: Show config compare
            out = session_x.send_cmd("show config compare | no-more", wait=3)
            session_x.log.append(f"[Session-X] Config compare shows changes ready")

            # Step 4: Commit (creates IRB+EVPN)
            out = session_x.send_cmd("commit", wait=15)
            session_x.log.append(f"[Session-X] First commit result: {out}")

            # Step 5: Rollback 1 (deletes IRB+EVPN)
            session_x.send_cmd("rollback 1", wait=2)
            session_x.send_cmd("show config compare | no-more", wait=3)

            # Step 6: Signal Session Y to get ready
            sync_event.set()

            # Step 7: Commit the rollback (this is the commit Session Y races against)
            out = session_x.send_cmd("commit", wait=20)
            session_x.log.append(f"[Session-X] Rollback commit result: {out}")

            results["x"] = out

            session_x.send_cmd("exit", wait=1)
            session_x.send_cmd("exit", wait=1)
        except Exception as e:
            session_x.log.append(f"[Session-X] ERROR: {e}")
            results["error"] = str(e)
        finally:
            session_x.close()

    def session_y_flow():
        try:
            session_y.connect()
            session_y.log.append(f"[Session-Y] Waiting for Session X to signal...")

            # Wait for Session X to be ready to commit rollback
            sync_event.wait(timeout=120)
            session_y.log.append(f"[Session-Y] Signal received, entering configure mode")

            # Slight delay to let Session X's commit start but not finish
            time.sleep(0.5)

            # Step 1: Enter configure mode (during Session X's commit)
            session_y.send_cmd("configure", wait=2)

            # Step 2: Rollback 1 (re-create the IRB+EVPN that Session X is deleting)
            out = session_y.send_cmd("rollback 1", wait=2)
            session_y.log.append(f"[Session-Y] Rollback result: {out}")

            # Step 3: Show config compare
            out = session_y.send_cmd("show config compare | no-more", wait=3)
            session_y.log.append(f"[Session-Y] Config compare: {out}")

            # Step 4: Commit - THIS IS WHERE THE BUG MANIFESTS
            out = session_y.send_cmd("commit", wait=20)
            session_y.log.append(f"[Session-Y] COMMIT RESULT: {out}")

            results["y"] = out

            session_y.send_cmd("exit", wait=1)
            session_y.send_cmd("exit", wait=1)
        except Exception as e:
            session_y.log.append(f"[Session-Y] ERROR: {e}")
            results["error"] = str(e)
        finally:
            session_y.close()

    thread_x = threading.Thread(target=session_x_flow, name="SessionX")
    thread_y = threading.Thread(target=session_y_flow, name="SessionY")

    thread_x.start()
    thread_y.start()

    thread_x.join(timeout=180)
    thread_y.join(timeout=180)

    print("\n--- Session X Log ---")
    session_x.print_log()
    print("\n--- Session Y Log ---")
    session_y.print_log()

    y_output = results.get("y", "") or ""
    x_output = results.get("x", "") or ""
    all_output = x_output + y_output

    if "Internal error" in all_output:
        print("\n*** RESULT: BUG REPRODUCED - Internal error detected ***")
        return "REPRODUCED"
    elif "RECOVERY" in all_output:
        print("\n*** RESULT: BUG REPRODUCED - Device entered RECOVERY ***")
        return "REPRODUCED"
    elif "NoneType" in all_output:
        print("\n*** RESULT: BUG REPRODUCED - NoneType error detected ***")
        return "REPRODUCED"
    elif "another commit is in progress" in all_output:
        print("\n*** RESULT: TIMING - Concurrent commit guard triggered (need to retry) ***")
        return "RETRY"
    elif "Commit succeeded" in y_output:
        print("\n*** RESULT: BUG NOT REPRODUCED - Session Y commit succeeded ***")
        return "NOT_REPRODUCED"
    elif "Commit failed" in y_output:
        print(f"\n*** RESULT: COMMIT FAILED with different error ***")
        return "DIFFERENT_ERROR"
    else:
        print(f"\n*** RESULT: INCONCLUSIVE ***")
        return "INCONCLUSIVE"


def cleanup_config():
    """Clean up any test config left on the device."""
    print("\n--- Cleanup: removing test configuration ---")
    s = DNOSSession("Cleanup")
    try:
        s.connect()
        s.send_cmd("configure", wait=2)
        s.send_cmd("no interfaces irb66", wait=1)
        s.send_cmd("no network-services evpn instance kfkfkf", wait=1)
        s.send_cmd("no network-services vrf instance alpha", wait=1)
        out = s.send_cmd("show config compare | no-more", wait=3)
        if "config-start" in out and ("irb66" in out or "kfkfkf" in out or "alpha" in out):
            s.send_cmd("commit", wait=15)
            print("  Cleanup commit done")
        else:
            s.send_cmd("rollback 0", wait=2)
            print("  No cleanup needed")
        s.send_cmd("exit", wait=1)
        s.send_cmd("exit", wait=1)
    except Exception as e:
        print(f"  Cleanup error: {e}")
    finally:
        s.close()


if __name__ == "__main__":
    MAX_ATTEMPTS = 3
    final_results = []

    for attempt in range(1, MAX_ATTEMPTS + 1):
        result = run_test(attempt)
        final_results.append(result)

        if result == "REPRODUCED":
            print(f"\n{'='*60}")
            print("  BUG SW-211457 IS NOT FIXED")
            print(f"{'='*60}")
            break

        # Cleanup between attempts
        cleanup_config()
        time.sleep(5)

    if "REPRODUCED" not in final_results:
        not_repro = final_results.count("NOT_REPRODUCED")
        print(f"\n{'='*60}")
        if not_repro >= 2:
            print(f"  BUG SW-211457 APPEARS FIXED ({not_repro}/{len(final_results)} clean passes)")
        else:
            print(f"  Results: {final_results}")
        print(f"{'='*60}")

    # Final cleanup
    cleanup_config()
