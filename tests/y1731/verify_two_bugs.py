#!/usr/bin/env python3
"""
Verify SW-242034 and SW-238673 on two machines with multiple attempts.

SW-242034: repeat-interval < test duration should be rejected by commit check.
SW-238673: Decimal percentage values should be accepted in threshold config.

Devices:
  NCPL (XEC1E3VR00008): 100.64.5.225
  NCP3 (WKY1C7VD00008P2): 100.64.8.59
"""

import paramiko
import re
import time
import json
from datetime import datetime

DEVICES = {
    "NCPL": {"host": "100.64.5.225", "serial": "XEC1E3VR00008"},
    "NCP3": {"host": "100.64.8.59", "serial": "WKY1C7VD00008P2"},
}

USER = "dnroot"
PASSWORD = "dnroot"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
ATTEMPTS = 3


class Session:
    def __init__(self, name, host):
        self.name = name
        self.host = host
        self.shell = None
        self.client = None

    def connect(self):
        print(f"  [{self.name}] Connecting to {self.host}...")
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(
            self.host, username=USER, password=PASSWORD, timeout=30,
            look_for_keys=False, allow_agent=False,
        )
        t = self.client.get_transport()
        if t:
            t.set_keepalive(30)
        self.shell = self.client.invoke_shell(width=400, height=1000)
        time.sleep(4)
        self._drain()
        self.shell.send("no-paging\n")
        time.sleep(2)
        self._drain()
        print(f"  [{self.name}] Connected.")

    def _drain(self):
        out = b""
        while self.shell.recv_ready():
            out += self.shell.recv(65535)
            time.sleep(0.1)
        return out.decode(errors="replace")

    def run(self, cmd, wait=5, timeout=30):
        self._drain()
        self.shell.send(cmd + "\n")
        output = ""
        start = time.time()
        last_data = time.time()
        while True:
            if time.time() - start > timeout:
                break
            if self.shell.recv_ready():
                chunk = self.shell.recv(65535).decode(errors="replace")
                output += chunk
                last_data = time.time()
                clean = ANSI_RE.sub("", output).strip()
                if clean.endswith("#") or clean.endswith(">"):
                    if time.time() - last_data > 0.3:
                        break
            else:
                if time.time() - last_data > wait:
                    break
                time.sleep(0.2)
        return ANSI_RE.sub("", output)

    def close(self):
        if self.shell:
            try:
                self.shell.send("exit\n")
            except Exception:
                pass
        if self.client:
            self.client.close()
        print(f"  [{self.name}] Disconnected.")


results = []


def record(bug, device, attempt, verified, detail):
    tag = "\033[92m[VERIFIED]\033[0m" if verified else "\033[91m[NOT FIXED]\033[0m"
    status = "VERIFIED" if verified else "NOT_FIXED"
    results.append({"bug": bug, "device": device, "attempt": attempt, "status": status, "detail": detail})
    print(f"    {tag} {bug} on {device} (attempt {attempt}): {detail}")


def test_sw242034(sess, device_name):
    """
    BUG: No validation that repeat-interval >= test duration (probe-count * probe-interval).
    FIX: commit check should reject configs where repeat-interval < probe-count * probe-interval.
    TEST: Create profile with probe-count=10, probe-interval=2 (=20s), repeat-interval=5 (< 20s).
          commit check should FAIL.
    """
    prof = "VERIFY_242034"
    for attempt in range(1, ATTEMPTS + 1):
        sess.run("configure", wait=2)
        sess.run(f"services performance-monitoring profiles cfm two-way-delay-measurement {prof}", wait=2)
        sess.run("inform-test-results enabled", wait=2)
        sess.run("test-duration probes probe-count 10 probe-interval 2 repeat-interval 5", wait=2)
        sess.run("thresholds delay-rtt-min 100", wait=2)
        sess.run("top", wait=1)

        out = sess.run("commit check", wait=8, timeout=20)
        has_error = bool(re.search(r"ERROR|failed|Failed", out, re.IGNORECASE))

        sess.run("rollback 0", wait=3)
        sess.run("exit", wait=2)

        if has_error:
            record("SW-242034", device_name, attempt, True,
                   "commit check correctly rejected overlapping timers (repeat=5 < count*interval=20)")
        else:
            record("SW-242034", device_name, attempt, False,
                   "commit check PASSED for repeat-interval(5) < probe-count(10)*probe-interval(2)=20")

        # Also test other invalid combos on last attempt
        if attempt == ATTEMPTS:
            for pc, pi, ri, label in [
                (100, 1, 10, "100s test, 10s repeat"),
                (5, 10, 10, "50s test, 10s repeat"),
            ]:
                sess.run("configure", wait=2)
                sess.run(f"services performance-monitoring profiles cfm two-way-delay-measurement {prof}_extra", wait=2)
                sess.run("inform-test-results enabled", wait=2)
                sess.run(f"test-duration probes probe-count {pc} probe-interval {pi} repeat-interval {ri}", wait=2)
                sess.run("thresholds delay-rtt-min 100", wait=2)
                sess.run("top", wait=1)
                out = sess.run("commit check", wait=8, timeout=20)
                has_err = bool(re.search(r"ERROR|failed|Failed", out, re.IGNORECASE))
                sess.run("rollback 0", wait=3)
                sess.run("exit", wait=2)
                tag = "VERIFIED" if has_err else "NOT_FIXED"
                print(f"      Extra check ({label}): {tag}")


def test_sw238673(sess, device_name):
    """
    BUG: Decimal percentage values (e.g. 0.1) rejected with 'Unknown word' error.
    FIX: Decimal percentages should be accepted.
    TEST: Try setting threshold near-end-loss to 0.1, 25.5, 99.99 - all should succeed.
    """
    prof = "VERIFY_238673"
    test_values = ["0.1", "25.5", "50.0", "99.99"]

    for attempt in range(1, ATTEMPTS + 1):
        issues = []
        for val in test_values:
            sess.run("configure", wait=2)
            out = sess.run(
                f"services performance-monitoring profiles cfm two-way-synthetic-loss-measurement {prof} "
                f"thresholds near-end-loss {val}",
                wait=3,
            )
            if "ERROR" in out or "Unknown word" in out or "Invalid" in out:
                issues.append(f"Rejected {val}: {out.strip().splitlines()[-1] if out.strip() else out[:80]}")
            sess.run("rollback 0", wait=3)
            sess.run("exit", wait=2)

        # Also test success-rate decimal in DM profile
        sess.run("configure", wait=2)
        out_sr = sess.run(
            f"services performance-monitoring profiles cfm two-way-delay-measurement {prof}_dm "
            f"thresholds success-rate 33.3",
            wait=3,
        )
        if "ERROR" in out_sr or "Unknown word" in out_sr or "Invalid" in out_sr:
            issues.append(f"Rejected success-rate 33.3")
        sess.run("rollback 0", wait=3)
        sess.run("exit", wait=2)

        if issues:
            record("SW-238673", device_name, attempt, False, "; ".join(issues))
        else:
            record("SW-238673", device_name, attempt, True,
                   f"All decimal values accepted: {', '.join(test_values)} + success-rate 33.3")


def main():
    print("=" * 80)
    print("Bug Verification: SW-242034 & SW-238673")
    print(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Attempts per bug per device: {ATTEMPTS}")
    print("=" * 80)

    for device_name, info in DEVICES.items():
        print(f"\n{'='*80}")
        print(f"DEVICE: {device_name} ({info['serial']}) @ {info['host']}")
        print(f"{'='*80}")

        sess = Session(device_name, info["host"])
        try:
            sess.connect()
        except Exception as e:
            print(f"  FAILED to connect: {e}")
            for bug in ["SW-242034", "SW-238673"]:
                for a in range(1, ATTEMPTS + 1):
                    record(bug, device_name, a, False, f"Connection failed: {e}")
            continue

        print(f"\n  --- SW-242034: repeat-interval vs test duration validation ---")
        try:
            test_sw242034(sess, device_name)
        except Exception as e:
            print(f"    [ERROR] {e}")

        print(f"\n  --- SW-238673: decimal percentage values ---")
        try:
            test_sw238673(sess, device_name)
        except Exception as e:
            print(f"    [ERROR] {e}")

        sess.close()

    # Final report
    print(f"\n{'='*80}")
    print("FINAL REPORT")
    print(f"{'='*80}")

    for bug in ["SW-242034", "SW-238673"]:
        bug_results = [r for r in results if r["bug"] == bug]
        v = sum(1 for r in bug_results if r["status"] == "VERIFIED")
        f = sum(1 for r in bug_results if r["status"] == "NOT_FIXED")
        total = len(bug_results)
        if f == 0:
            verdict = "\033[92mVERIFIED\033[0m"
        else:
            verdict = f"\033[91mNOT FIXED ({f}/{total} failures)\033[0m"
        print(f"\n{bug}: {verdict}")
        for r in bug_results:
            print(f"  {r['device']} attempt {r['attempt']}: {r['status']} - {r['detail']}")

    fname = f"/home/dn/verify_242034_238673_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(fname, "w") as fp:
        json.dump({"date": datetime.now().isoformat(), "results": results}, fp, indent=2)
    print(f"\nResults saved to: {fname}")


if __name__ == "__main__":
    main()
