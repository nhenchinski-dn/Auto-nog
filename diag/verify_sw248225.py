#!/usr/bin/env python3
"""Verify SW-248225: PM Profile allows multiple test-duration types to coexist."""

import pexpect
import sys
import time

DEVICE_IP = "100.64.4.93"
USERNAME = "dnroot"
PASSWORD = "dnroot"
PROFILE = "SW248225_TEST"
PROMPT = r"ncpl-cfm-nog[#(]"

def run():
    print(f"Connecting to {DEVICE_IP}...")
    child = pexpect.spawn(
        f"sshpass -p {PASSWORD} ssh -o StrictHostKeyChecking=no -o PubkeyAuthentication=no {USERNAME}@{DEVICE_IP}",
        timeout=30,
        encoding="utf-8",
    )
    child.logfile_read = sys.stdout

    child.expect(PROMPT)
    time.sleep(0.5)

    def send_cmd(cmd, wait=1):
        child.sendline(cmd)
        time.sleep(wait)
        child.expect(PROMPT)

    print("\n\n=== STEP 0: Check version ===")
    send_cmd("show system version | no-more")

    print("\n\n=== STEP 1: Cleanup any prior test profile ===")
    send_cmd("config")
    send_cmd(f"no services performance-monitoring profiles cfm two-way-delay-measurement {PROFILE}")
    send_cmd("commit")
    send_cmd("exit")

    print("\n\n=== STEP 2: Configure test-duration PROBES and commit ===")
    send_cmd("config")
    send_cmd(
        f"services performance-monitoring profiles cfm two-way-delay-measurement {PROFILE} "
        f"test-duration probes probe-count 10 probe-interval 1 repeat-interval 60"
    )
    send_cmd("commit", wait=3)
    send_cmd("exit")

    print("\n\n=== STEP 2 VERIFY: Show config with probes ===")
    send_cmd(f"show config services performance-monitoring profiles cfm two-way-delay-measurement {PROFILE} | no-more", wait=2)

    print("\n\n=== STEP 3: Single transaction - remove probes + add time-frame ===")
    send_cmd("config")
    send_cmd(
        f"no services performance-monitoring profiles cfm two-way-delay-measurement {PROFILE} test-duration probes"
    )
    send_cmd(
        f"services performance-monitoring profiles cfm two-way-delay-measurement {PROFILE} "
        f"test-duration time-frame minutes 5 probe-interval 2 repeat-interval 600"
    )
    send_cmd("commit", wait=3)
    send_cmd("exit")

    print("\n\n" + "=" * 70)
    print("=== STEP 3 VERIFY: Show config after switch to time-frame ===")
    print("=== EXPECTED (FIXED): Only 'test-duration time-frame' should appear ===")
    print("=== BUG (UNFIXED): Both 'test-duration probes' AND 'time-frame' ===")
    print("=" * 70)
    send_cmd(f"show config services performance-monitoring profiles cfm two-way-delay-measurement {PROFILE} | no-more", wait=2)

    print("\n\n=== STEP 4: Cleanup ===")
    send_cmd("config")
    send_cmd(f"no services performance-monitoring profiles cfm two-way-delay-measurement {PROFILE}")
    send_cmd("commit", wait=3)
    send_cmd("exit")

    print("\n\n=== VERIFICATION COMPLETE ===")
    child.sendline("exit")
    child.close()

if __name__ == "__main__":
    run()
