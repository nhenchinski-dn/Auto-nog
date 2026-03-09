#!/usr/bin/env python3
"""
SW-211457 Bug Reproduction Test v2
Fixes: Use correct BGP AS (65001), handle CLI context, proper VRF config.

Race condition: Internal error when rollback+commit from 2 CLI sessions
involving IRB interface creation/deletion with EVPN.
"""

import paramiko
import threading
import time
import sys

DEVICE_IP = "100.64.3.239"
USER = "dnroot"
PASS = "dnroot"
BGP_AS = "65001"


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
        self.shell.settimeout(90)
        time.sleep(3)
        initial = self._read_all()
        self.log.append(f"[{self.name}] Connected. Prompt: {initial.strip()[-60:]}")

    def send_cmd(self, cmd, wait=3):
        self.log.append(f"[{self.name}] >>> {cmd}")
        self.shell.send(cmd + "\n")
        time.sleep(wait)
        output = self._read_all()
        clean = self._strip_ansi(output)
        self.log.append(f"[{self.name}] <<< {clean}")
        return clean

    def _read_all(self):
        output = ""
        while self.shell.recv_ready():
            chunk = self.shell.recv(65536).decode('utf-8', errors='replace')
            output += chunk
            time.sleep(0.3)
        return output

    @staticmethod
    def _strip_ansi(text):
        import re
        return re.sub(r'\x1b\[[0-9;]*m', '', text)

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
    print(f"\n{'='*70}")
    print(f"  ATTEMPT {attempt_num}")
    print(f"{'='*70}")

    session_x = DNOSSession("X")
    session_y = DNOSSession("Y")
    x_commit_started = threading.Event()
    results = {"x_output": "", "y_output": "", "error": None}

    def session_x_flow():
        try:
            session_x.connect()
            session_x.send_cmd("configure", wait=2)

            # Step 1: Configure IRB + EVPN (using correct BGP AS 65001)
            session_x.send_cmd(f"interfaces irb66 admin-state enabled ipv4-address 101.1.0.254/24", wait=1)
            session_x.send_cmd(f"network-services evpn instance kfkfkf protocols bgp {BGP_AS} export-l2vpn-evpn route-target 100:1", wait=1)
            session_x.send_cmd(f"network-services evpn instance kfkfkf protocols bgp {BGP_AS} import-l2vpn-evpn route-target 100:1", wait=1)
            session_x.send_cmd(f"network-services evpn instance kfkfkf protocols bgp {BGP_AS} route-distinguisher 65145:1", wait=1)
            session_x.send_cmd(f"network-services evpn instance kfkfkf counters service-counters enabled", wait=1)

            # Use 'top' to return to top config context before setting router-interface
            session_x.send_cmd("top", wait=1)
            session_x.send_cmd(f"network-services evpn instance kfkfkf router-interface irb66", wait=1)
            session_x.send_cmd("top", wait=1)
            session_x.send_cmd(f"network-services vrf instance alpha interface irb66", wait=1)
            session_x.send_cmd("top", wait=1)

            # Show what will be committed
            out = session_x.send_cmd("show config compare | no-more", wait=3)
            session_x.log.append(f"[X] === Config compare before first commit ===")

            # Step 2: Commit the IRB+EVPN config
            out = session_x.send_cmd("commit", wait=20)
            if "ERROR" in out or "error" in out.lower():
                session_x.log.append(f"[X] !!! FIRST COMMIT FAILED: {out}")
                results["error"] = f"Session X first commit failed: {out}"
                return
            session_x.log.append(f"[X] First commit succeeded")

            # Step 3: Rollback 1 (delete the IRB+EVPN config)
            session_x.send_cmd("rollback 1", wait=2)
            out = session_x.send_cmd("show config compare | no-more", wait=3)
            session_x.log.append(f"[X] === Rollback compare (should show deletions) ===")

            # Step 4: Signal Session Y, then commit the rollback
            # Session Y should enter configure + rollback 1 during this commit
            x_commit_started.set()
            session_x.log.append(f"[X] === Signaling Y and committing rollback ===")
            out = session_x.send_cmd("commit", wait=25)
            results["x_output"] = out
            session_x.log.append(f"[X] Rollback commit output: {out[-300:]}")

            session_x.send_cmd("exit", wait=1)
            session_x.send_cmd("exit", wait=1)
        except Exception as e:
            session_x.log.append(f"[X] EXCEPTION: {e}")
            results["error"] = str(e)
            x_commit_started.set()
        finally:
            session_x.close()

    def session_y_flow():
        try:
            session_y.connect()
            session_y.log.append(f"[Y] Waiting for X to signal...")

            x_commit_started.wait(timeout=180)
            if not x_commit_started.is_set():
                session_y.log.append(f"[Y] Timeout waiting for X")
                return

            # Critical timing: enter configure mode immediately after X starts commit
            # The race window is between point-of-no-return and cached_running_config update
            time.sleep(0.3)

            session_y.send_cmd("configure", wait=2)
            session_y.log.append(f"[Y] Entered configure mode during X's commit window")

            # Rollback 1 - re-creates the IRB+EVPN that X is deleting
            out = session_y.send_cmd("rollback 1", wait=2)
            session_y.log.append(f"[Y] Rollback 1 done")

            out = session_y.send_cmd("show config compare | no-more", wait=3)
            session_y.log.append(f"[Y] === Config compare after rollback ===")

            # THIS IS THE BUG TRIGGER: commit with re-created IRB
            out = session_y.send_cmd("commit", wait=30)
            results["y_output"] = out
            session_y.log.append(f"[Y] *** COMMIT RESULT: {out[-500:]} ***")

            session_y.send_cmd("exit", wait=1)
            session_y.send_cmd("exit", wait=1)
        except Exception as e:
            session_y.log.append(f"[Y] EXCEPTION: {e}")
            results["error"] = str(e)
        finally:
            session_y.close()

    # Run both sessions in parallel
    thread_x = threading.Thread(target=session_x_flow, name="SessionX")
    thread_y = threading.Thread(target=session_y_flow, name="SessionY")

    thread_x.start()
    thread_y.start()

    thread_x.join(timeout=240)
    thread_y.join(timeout=240)

    print("\n--- Session X Log ---")
    session_x.print_log()
    print("\n--- Session Y Log ---")
    session_y.print_log()

    # Analyze results
    y_out = results.get("y_output", "") or ""
    x_out = results.get("x_output", "") or ""
    all_out = x_out + y_out
    err = results.get("error", "") or ""

    if "Internal error" in all_out or "NoneType" in all_out:
        verdict = "REPRODUCED"
        print(f"\n*** RESULT: BUG REPRODUCED ***")
    elif "RECOVERY" in all_out:
        verdict = "REPRODUCED"
        print(f"\n*** RESULT: BUG REPRODUCED - Device in RECOVERY ***")
    elif "another commit is in progress" in y_out:
        verdict = "RETRY"
        print(f"\n*** RESULT: TIMING - concurrent commit guard (will retry) ***")
    elif "Commit succeeded" in y_out:
        verdict = "NOT_REPRODUCED"
        print(f"\n*** RESULT: BUG NOT REPRODUCED - Y commit succeeded ***")
    elif "Commit failed" in y_out:
        verdict = "DIFFERENT_ERROR"
        print(f"\n*** RESULT: Y COMMIT FAILED with different error ***")
    elif err:
        verdict = "ERROR"
        print(f"\n*** RESULT: TEST ERROR - {err[:200]} ***")
    else:
        verdict = "INCONCLUSIVE"
        print(f"\n*** RESULT: INCONCLUSIVE ***")

    return verdict


def cleanup_config():
    """Remove test config so device is clean for next attempt."""
    print("\n  [Cleanup] Removing test configuration...")
    s = DNOSSession("Cleanup")
    try:
        s.connect()
        s.send_cmd("configure", wait=2)
        s.send_cmd("no interfaces irb66", wait=1)
        s.send_cmd("no network-services evpn instance kfkfkf", wait=1)
        s.send_cmd("no network-services vrf instance alpha interface irb66", wait=1)
        s.send_cmd("top", wait=1)
        out = s.send_cmd("show config compare | no-more", wait=3)
        if "irb66" in out or "kfkfkf" in out or "alpha" in out:
            commit_out = s.send_cmd("commit", wait=20)
            print(f"  [Cleanup] Committed: {commit_out[-100:]}")
        else:
            s.send_cmd("rollback 0", wait=2)
            print(f"  [Cleanup] Nothing to clean")
        s.send_cmd("exit", wait=1)
        s.send_cmd("exit", wait=1)
    except Exception as e:
        print(f"  [Cleanup] Error: {e}")
    finally:
        s.close()


if __name__ == "__main__":
    MAX_ATTEMPTS = 3
    all_results = []

    for attempt in range(1, MAX_ATTEMPTS + 1):
        result = run_test(attempt)
        all_results.append(result)

        if result == "REPRODUCED":
            print(f"\n{'='*70}")
            print(f"  ** SW-211457: BUG IS NOT FIXED **")
            print(f"{'='*70}")
            break

        if result in ("RETRY", "ERROR", "DIFFERENT_ERROR", "INCONCLUSIVE"):
            print(f"  Attempt {attempt} inconclusive, cleaning up and retrying...")

        cleanup_config()
        time.sleep(5)

    if "REPRODUCED" not in all_results:
        passes = all_results.count("NOT_REPRODUCED")
        print(f"\n{'='*70}")
        print(f"  SW-211457 RESULTS: {all_results}")
        if passes >= 2:
            print(f"  CONCLUSION: Bug appears FIXED ({passes}/{len(all_results)} clean passes)")
        else:
            print(f"  CONCLUSION: Inconclusive - more testing may be needed")
        print(f"{'='*70}")

    cleanup_config()
    print("\nDone.")
