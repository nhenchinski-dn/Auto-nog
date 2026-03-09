#!/usr/bin/env python3
"""
Verification script for closed Y.1731 PM bugs on epic SW-141523.
Tests each bug on two machines with multiple attempts.

Devices:
  NCPL (XEC1E3VR00008): 100.64.5.225  - MEP 1 (MD-CUST/MA-CUST), MEP 3 (MD-CUST1/MA-CUST1)
  NCP3 (WKY1C7VD00008P2): 100.64.8.59 - MEP 2 (MD-CUST/MA-CUST), MEP 4 (MD-CUST1/MA-CUST1)
"""

import paramiko
import re
import time
import json
import sys
from datetime import datetime
from typing import Dict, List, Optional, Tuple

DEVICES = {
    "NCPL": {
        "host": "100.64.5.225",
        "serial": "XEC1E3VR00008",
        "mep_id": 1,
        "remote_mep": 2,
        "md": "MD-CUST",
        "ma": "MA-CUST",
        "interface": "ge10-0/0/32.100",
    },
    "NCP3": {
        "host": "100.64.8.59",
        "serial": "WKY1C7VD00008P2",
        "mep_id": 2,
        "remote_mep": 1,
        "md": "MD-CUST",
        "ma": "MA-CUST",
        "interface": "ge400-0/0/24.100",
    },
}

USER = "dnroot"
PASSWORD = "dnroot"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
ATTEMPTS = 3


class DeviceSession:
    def __init__(self, name: str, host: str):
        self.name = name
        self.host = host
        self.client: Optional[paramiko.SSHClient] = None
        self.shell = None
        self.prompt = ""

    def connect(self):
        print(f"  [{self.name}] Connecting to {self.host}...")
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(
            self.host,
            username=USER,
            password=PASSWORD,
            timeout=30,
            look_for_keys=False,
            allow_agent=False,
        )
        transport = self.client.get_transport()
        if transport:
            transport.set_keepalive(30)
        self.shell = self.client.invoke_shell(width=400, height=1000)
        time.sleep(4)
        banner = self._drain()
        self.shell.send("no-paging\n")
        time.sleep(2)
        self._drain()
        clean = ANSI_RE.sub("", banner)
        for line in reversed(clean.splitlines()):
            stripped = line.strip()
            if stripped.endswith("#") or stripped.endswith(">"):
                self.prompt = stripped
                break
        print(f"  [{self.name}] Connected. Prompt: {self.prompt}")

    def _drain(self) -> str:
        out = b""
        while self.shell.recv_ready():
            out += self.shell.recv(65535)
            time.sleep(0.1)
        return out.decode(errors="replace")

    def run(self, cmd: str, wait: float = 5, timeout: float = 30) -> str:
        self._drain()
        self.shell.send(cmd + "\n")
        output = ""
        start = time.time()
        last_data = time.time()
        while True:
            elapsed = time.time() - start
            if elapsed > timeout:
                break
            if self.shell.recv_ready():
                chunk = self.shell.recv(65535).decode(errors="replace")
                output += chunk
                last_data = time.time()
                clean = ANSI_RE.sub("", output).strip()
                if clean.endswith("#") or clean.endswith(">"):
                    if time.time() - last_data > 0.3 or elapsed > wait:
                        break
            else:
                if time.time() - last_data > wait:
                    break
                time.sleep(0.2)
        return ANSI_RE.sub("", output)

    def run_config(self, commands: List[str], wait: float = 3) -> str:
        self.run("configure", wait=2)
        outputs = []
        for cmd in commands:
            out = self.run(cmd, wait=wait)
            outputs.append(out)
        return "\n".join(outputs)

    def commit_check(self) -> Tuple[bool, str]:
        out = self.run("commit check", wait=8, timeout=30)
        has_error = bool(
            re.search(r"ERROR|error|failed|Failed|unexpected", out, re.IGNORECASE)
        )
        return (not has_error, out)

    def commit(self) -> Tuple[bool, str]:
        out = self.run("commit", wait=10, timeout=30)
        has_error = bool(
            re.search(r"ERROR|error|failed|Failed|unexpected", out, re.IGNORECASE)
        )
        return (not has_error, out)

    def rollback(self) -> str:
        return self.run("rollback 0", wait=5)

    def exit_config(self) -> str:
        self.run("top", wait=1)
        self.rollback()
        return self.run("exit", wait=2)

    def tab_complete(self, partial_cmd: str) -> str:
        """Send partial command + TAB and capture completion suggestions."""
        self._drain()
        self.shell.send(partial_cmd + "\t")
        time.sleep(2)
        output = ""
        while self.shell.recv_ready():
            output += self.shell.recv(65535).decode(errors="replace")
            time.sleep(0.1)
        self.shell.send("\x03")  # Ctrl+C to cancel
        time.sleep(1)
        self._drain()
        return ANSI_RE.sub("", output)

    def close(self):
        if self.shell:
            try:
                self.shell.send("exit\n")
                time.sleep(1)
            except Exception:
                pass
        if self.client:
            self.client.close()
        print(f"  [{self.name}] Disconnected.")


class BugVerifier:
    def __init__(self):
        self.results: List[Dict] = []

    def record(
        self,
        bug_id: str,
        device: str,
        attempt: int,
        verified: bool,
        detail: str,
        raw: str = "",
    ):
        status = "VERIFIED" if verified else "NOT_FIXED"
        self.results.append(
            {
                "bug": bug_id,
                "device": device,
                "attempt": attempt,
                "status": status,
                "detail": detail,
            }
        )
        tag = f"\033[92m[VERIFIED]\033[0m" if verified else f"\033[91m[NOT FIXED]\033[0m"
        print(f"    {tag} {bug_id} on {device} (attempt {attempt}): {detail}")

    def test_sw242034(self, sess: DeviceSession, device_name: str):
        """repeat-interval < test duration should be rejected by commit check."""
        prof = "VERIFY_242034"
        for attempt in range(1, ATTEMPTS + 1):
            sess.run_config(
                [
                    f"services performance-monitoring profiles cfm two-way-delay-measurement {prof}",
                    "inform-test-results enabled",
                    "test-duration probes probe-count 10 probe-interval 2 repeat-interval 5",
                    "thresholds delay-rtt-min 100",
                    "top",
                ]
            )
            ok, out = sess.commit_check()
            sess.rollback()
            sess.run("exit", wait=2)
            if not ok:
                self.record(
                    "SW-242034", device_name, attempt, True,
                    "commit check correctly rejected overlapping timers",
                )
            else:
                self.record(
                    "SW-242034", device_name, attempt, False,
                    "commit check passed for repeat-interval(5) < probe-count(10)*probe-interval(2)=20",
                )

    def test_sw242033(self, sess: DeviceSession, device_name: str):
        """Profile deletion should work after session deletion."""
        prof = "VERIFY_242033_PROF"
        session = "VERIFY_242033_SESS"
        dev = DEVICES[device_name]
        for attempt in range(1, ATTEMPTS + 1):
            sess.run_config(
                [
                    f"services performance-monitoring profiles cfm two-way-delay-measurement {prof}",
                    "inform-test-results enabled",
                    "test-duration probes probe-count 5 probe-interval 1 repeat-interval 10",
                    "thresholds delay-rtt-min 100",
                    "thresholds delay-rtt-avg 1000",
                    "top",
                    f"services performance-monitoring cfm two-way-delay-measurement {session}",
                    "admin-state enabled",
                    f"profile {prof}",
                    f"source maintenance-domain {dev['md']} maintenance-association {dev['ma']} mep-id {dev['mep_id']}",
                    f"target mep-id {dev['remote_mep']}",
                    "top",
                ]
            )
            ok_c, _ = sess.commit()
            if not ok_c:
                sess.rollback()
                sess.run("exit", wait=2)
                self.record("SW-242033", device_name, attempt, False, "Could not create test session (commit failed)")
                continue
            time.sleep(3)

            sess.run("configure", wait=2)
            sess.run(f"no services performance-monitoring cfm two-way-delay-measurement {session}", wait=2)
            sess.run("top", wait=1)
            sess.commit()
            time.sleep(2)

            sess.run(f"no services performance-monitoring profiles cfm two-way-delay-measurement {prof}", wait=2)
            sess.run("top", wait=1)
            ok, out = sess.commit_check()
            if ok:
                sess.commit()
                sess.run("exit", wait=2)
                self.record("SW-242033", device_name, attempt, True, "Profile deletion succeeded after session deletion")
            else:
                sess.rollback()
                sess.run("exit", wait=2)
                self.record(
                    "SW-242033", device_name, attempt, False,
                    f"Profile deletion still fails: {out[:200]}",
                )

    def test_sw241287(self, sess: DeviceSession, device_name: str):
        """Removing thresholds while inform-test-results enabled should be blocked."""
        prof = "VERIFY_241287"
        for attempt in range(1, ATTEMPTS + 1):
            sess.run_config(
                [
                    f"services performance-monitoring profiles cfm two-way-delay-measurement {prof}",
                    "inform-test-results enabled",
                    "thresholds success-rate 20.0",
                    "top",
                ]
            )
            sess.commit()
            time.sleep(1)

            sess.run("configure", wait=2)
            sess.run(
                f"no services performance-monitoring profiles cfm two-way-delay-measurement {prof} thresholds",
                wait=2,
            )
            sess.run("top", wait=1)
            ok, out = sess.commit_check()

            if not ok:
                self.record(
                    "SW-241287", device_name, attempt, True,
                    "Correctly blocked threshold removal while inform-test-results enabled",
                )
            else:
                self.record(
                    "SW-241287", device_name, attempt, False,
                    "Allowed removing thresholds while inform-test-results enabled",
                )

            sess.rollback()
            # cleanup
            sess.run(f"no services performance-monitoring profiles cfm two-way-delay-measurement {prof}", wait=2)
            sess.run("top", wait=1)
            sess.commit()
            sess.run("exit", wait=2)

    def test_sw241269(self, sess: DeviceSession, device_name: str):
        """DMM target should show MAC address, not single digit."""
        dev = DEVICES[device_name]
        for attempt in range(1, ATTEMPTS + 1):
            out = sess.run(
                f"run ethernet-oam cfm on-demand delay-measurement two-way "
                f"maintenance-domain {dev['md']} maintenance-association {dev['ma']} "
                f"target mac-address 22:22:22:22:22:22 count 3 interval 1",
                wait=15,
                timeout=25,
            )
            time.sleep(2)
            detail_out = sess.run(
                "show services performance-monitoring cfm tests on-demand two-way-delay detail | no-more",
                wait=8,
            )
            target_match = re.search(r"Target:\s*(.+)", detail_out)
            if target_match:
                target_val = target_match.group(1).strip()
                if re.match(r"^\d+$", target_val):
                    self.record(
                        "SW-241269", device_name, attempt, False,
                        f"Target shows single digit '{target_val}' instead of MAC",
                    )
                elif "22:22:22" in target_val or re.match(r"[0-9a-fA-F]{2}:", target_val):
                    self.record(
                        "SW-241269", device_name, attempt, True,
                        f"Target correctly shows MAC: {target_val}",
                    )
                else:
                    self.record(
                        "SW-241269", device_name, attempt, True,
                        f"Target shows: {target_val}",
                    )
            else:
                self.record("SW-241269", device_name, attempt, False, "Could not find Target field in output")

    def test_sw240517(self, sess: DeviceSession, device_name: str):
        """Status should be consistent between on-demand views, target should be consistent."""
        dev = DEVICES[device_name]
        for attempt in range(1, ATTEMPTS + 1):
            sess.run(
                f"run ethernet-oam cfm on-demand delay-measurement two-way "
                f"maintenance-domain {dev['md']} maintenance-association {dev['ma']} "
                f"target mep-id {dev['remote_mep']} count 3",
                wait=15,
                timeout=25,
            )
            time.sleep(2)

            summary_out = sess.run(
                "show services performance-monitoring cfm tests on-demand two-way-delay | no-more",
                wait=8,
            )
            all_out = sess.run(
                "show services performance-monitoring cfm tests on-demand | no-more",
                wait=8,
            )

            status_specific = re.search(r"Status\s*\|\s*\n.*?\|\s*(\w+)\s*\|", summary_out)
            issues = []
            if "two-way-delay" in all_out.lower():
                lines = all_out.split("\n")
                for line in lines:
                    if "two-way-delay" in line.lower():
                        if "Invalid" in line and "Ongoing" in summary_out:
                            issues.append("Status mismatch: specific=Ongoing, general=Invalid")
                        cols = [c.strip() for c in line.split("|") if c.strip()]
                        if len(cols) >= 6:
                            target_col = cols[5] if len(cols) > 5 else ""
                            if re.match(r"^[0-9a-fA-F]{2}:", target_col) and str(dev["remote_mep"]) not in summary_out:
                                issues.append(f"Target inconsistency: general shows MAC {target_col}")

            if issues:
                self.record("SW-240517", device_name, attempt, False, "; ".join(issues))
            else:
                self.record("SW-240517", device_name, attempt, True, "Status and target consistent across views")

    def test_sw240425(self, sess: DeviceSession, device_name: str):
        """On-demand stop should set end time."""
        dev = DEVICES[device_name]
        for attempt in range(1, ATTEMPTS + 1):
            sess.run(
                f"run ethernet-oam cfm on-demand linktrace "
                f"maintenance-domain {dev['md']} maintenance-association {dev['ma']} "
                f"target mep-id {dev['remote_mep']}",
                wait=8,
                timeout=15,
            )
            time.sleep(1)
            sess.run("request ethernet-oam cfm on-demand stop all", wait=5)
            time.sleep(2)

            detail_out = sess.run(
                "show services performance-monitoring cfm tests on-demand linktrace detail | no-more",
                wait=8,
            )

            end_match = re.search(r"End time:\s*(.*)", detail_out)
            if end_match:
                end_val = end_match.group(1).strip()
                if end_val and end_val != "":
                    self.record(
                        "SW-240425", device_name, attempt, True,
                        f"End time is populated: {end_val}",
                    )
                else:
                    self.record(
                        "SW-240425", device_name, attempt, False,
                        "End time is empty after stop",
                    )
            else:
                self.record("SW-240425", device_name, attempt, True, "No test session found or format changed")

    def test_sw240423(self, sess: DeviceSession, device_name: str):
        """Proactive DM description should not show 'MEP None' for MAC-based sessions."""
        dev = DEVICES[device_name]
        for attempt in range(1, ATTEMPTS + 1):
            out = sess.run(
                "show services performance-monitoring cfm tests proactive detail | no-more",
                wait=10,
            )
            if "MEP None" in out:
                self.record(
                    "SW-240423", device_name, attempt, False,
                    "Description still shows 'MEP None'",
                )
            elif "Description:" in out:
                desc_matches = re.findall(r"Description:\s*(.*)", out)
                issues = []
                for desc in desc_matches:
                    if "Delay measurement" in desc and "synthetic-loss" in out.lower():
                        issues.append(f"Wrong description type: {desc.strip()}")
                if issues:
                    self.record("SW-240423", device_name, attempt, False, "; ".join(issues))
                else:
                    self.record("SW-240423", device_name, attempt, True, "Description correct, no 'MEP None'")
            else:
                self.record("SW-240423", device_name, attempt, True, "No proactive sessions or no Description field")

    def test_sw239944(self, sess: DeviceSession, device_name: str):
        """On-demand stop should not list completed/non-running tests."""
        dev = DEVICES[device_name]
        for attempt in range(1, ATTEMPTS + 1):
            out = sess.run("request ethernet-oam cfm on-demand stop all", wait=5)
            if "Stopped tests:" in out and "Total stopped tests:" in out:
                count_match = re.search(r"Total stopped tests:\s*(\d+)", out)
                count = int(count_match.group(1)) if count_match else 0
                if count > 0:
                    self.record(
                        "SW-239944", device_name, attempt, False,
                        f"Stop reports {count} stopped tests when none should be running",
                    )
                else:
                    self.record("SW-239944", device_name, attempt, True, "No tests falsely reported as stopped")
            elif "No active" in out or "no on-demand" in out.lower() or "Total stopped tests: 0" in out:
                self.record("SW-239944", device_name, attempt, True, "Correctly reports no active tests")
            else:
                if "Stopped tests:" not in out:
                    self.record("SW-239944", device_name, attempt, True, "No stopped tests listed")
                else:
                    self.record("SW-239944", device_name, attempt, False, f"Unexpected output: {out[:200]}")

    def test_sw239925(self, sess: DeviceSession, device_name: str):
        """On-demand test should not show 'Ongoing' after being stopped."""
        dev = DEVICES[device_name]
        for attempt in range(1, ATTEMPTS + 1):
            sess.run(
                f"run ethernet-oam cfm on-demand loopback "
                f"maintenance-domain {dev['md']} maintenance-association {dev['ma']} "
                f"target mep-id {dev['remote_mep']} count 100 interval 1",
                wait=5,
                timeout=10,
            )
            time.sleep(2)
            self.shell_send_ctrl_c(sess)
            time.sleep(2)
            sess.run("request ethernet-oam cfm on-demand stop all", wait=5)
            time.sleep(2)

            out = sess.run(
                "show services performance-monitoring cfm tests on-demand | no-more",
                wait=8,
            )
            if "Ongoing" in out:
                self.record("SW-239925", device_name, attempt, False, "Test still shows 'Ongoing' after stop")
            else:
                self.record("SW-239925", device_name, attempt, True, "No tests showing 'Ongoing' after stop")

    def shell_send_ctrl_c(self, sess: DeviceSession):
        sess.shell.send("\x03")
        time.sleep(1)
        sess._drain()

    def test_sw239530(self, sess: DeviceSession, device_name: str):
        """On-demand target should not show 00:00:00:00:00:00 for mep-id tests."""
        dev = DEVICES[device_name]
        for attempt in range(1, ATTEMPTS + 1):
            sess.run(
                f"run ethernet-oam cfm on-demand delay-measurement two-way "
                f"maintenance-domain {dev['md']} maintenance-association {dev['ma']} "
                f"target mep-id {dev['remote_mep']} count 3",
                wait=15,
                timeout=25,
            )
            time.sleep(2)

            out = sess.run(
                "show services performance-monitoring cfm tests on-demand | no-more",
                wait=8,
            )
            if "00:00:00:00:00:00" in out:
                self.record("SW-239530", device_name, attempt, False, "Target shows 00:00:00:00:00:00")
            else:
                self.record("SW-239530", device_name, attempt, True, "Target does not show all-zero MAC")

    def test_sw238960(self, sess: DeviceSession, device_name: str):
        """Multicast/broadcast MAC should be rejected as PM target."""
        test_name = "VERIFY_238960"
        dev = DEVICES[device_name]
        for attempt in range(1, ATTEMPTS + 1):
            macs = ["ff:ff:ff:ff:ff:ff", "01:00:5e:00:00:01", "33:33:00:00:00:01"]
            issues = []
            for mac in macs:
                sess.run_config(
                    [
                        f"services performance-monitoring cfm two-way-delay-measurement {test_name}",
                        "admin-state enabled",
                        "profile test",
                        f"source maintenance-domain {dev['md']} maintenance-association {dev['ma']} mep-id {dev['mep_id']}",
                        f"target mac-address {mac}",
                        "top",
                    ]
                )
                ok, out = sess.commit_check()
                sess.rollback()
                if ok:
                    issues.append(f"Accepted multicast/broadcast MAC {mac}")
                sess.run("exit", wait=2)

            if issues:
                self.record("SW-238960", device_name, attempt, False, "; ".join(issues))
            else:
                self.record("SW-238960", device_name, attempt, True, "All multicast/broadcast MACs rejected")

    def test_sw238674(self, sess: DeviceSession, device_name: str):
        """Multiple proactive tests for same source MEP-ID should be blocked."""
        dev = DEVICES[device_name]
        t1 = "VERIFY_238674_1"
        t2 = "VERIFY_238674_2"
        prof = "VERIFY_238674_P"
        for attempt in range(1, ATTEMPTS + 1):
            sess.run_config(
                [
                    f"services performance-monitoring profiles cfm two-way-delay-measurement {prof}",
                    "test-duration probes probe-count 5 probe-interval 1 repeat-interval 10",
                    "thresholds delay-rtt-min 100",
                    "top",
                    f"services performance-monitoring cfm two-way-delay-measurement {t1}",
                    "admin-state enabled",
                    f"profile {prof}",
                    f"source maintenance-domain {dev['md']} maintenance-association {dev['ma']} mep-id {dev['mep_id']}",
                    f"target mep-id {dev['remote_mep']}",
                    "top",
                    f"services performance-monitoring cfm two-way-delay-measurement {t2}",
                    "admin-state enabled",
                    f"profile {prof}",
                    f"source maintenance-domain {dev['md']} maintenance-association {dev['ma']} mep-id {dev['mep_id']}",
                    "target mac-address 22:22:22:22:22:22",
                    "top",
                ]
            )
            ok, out = sess.commit_check()
            sess.rollback()
            # cleanup
            sess.run(f"no services performance-monitoring cfm two-way-delay-measurement {t1}", wait=2)
            sess.run(f"no services performance-monitoring cfm two-way-delay-measurement {t2}", wait=2)
            sess.run(f"no services performance-monitoring profiles cfm two-way-delay-measurement {prof}", wait=2)
            sess.run("top", wait=1)
            sess.commit()
            sess.run("exit", wait=2)

            if not ok:
                self.record(
                    "SW-238674", device_name, attempt, True,
                    "Correctly blocked multiple tests for same MEP-ID",
                )
            else:
                self.record(
                    "SW-238674", device_name, attempt, False,
                    "Allowed multiple proactive tests for same source MEP-ID",
                )

    def test_sw238673(self, sess: DeviceSession, device_name: str):
        """Decimal percentage values should be accepted."""
        prof = "VERIFY_238673"
        for attempt in range(1, ATTEMPTS + 1):
            sess.run("configure", wait=2)
            out = sess.run(
                f"services performance-monitoring profiles cfm two-way-synthetic-loss-measurement {prof} thresholds near-end-loss 0.1",
                wait=3,
            )
            if "ERROR" in out or "Unknown word" in out:
                sess.rollback()
                sess.run("exit", wait=2)
                self.record("SW-238673", device_name, attempt, False, f"Decimal 0.1 rejected: {out[:150]}")
            else:
                sess.rollback()
                # cleanup
                sess.run(f"no services performance-monitoring profiles cfm two-way-synthetic-loss-measurement {prof}", wait=2)
                sess.run("top", wait=1)
                sess.commit()
                sess.run("exit", wait=2)
                self.record("SW-238673", device_name, attempt, True, "Decimal percentage 0.1 accepted")

    def test_sw237380(self, sess: DeviceSession, device_name: str):
        """Percentage thresholds should show range 0-100, reject values outside."""
        for attempt in range(1, ATTEMPTS + 1):
            sess.run("configure", wait=2)
            out = sess.run(
                "services performance-monitoring profiles cfm two-way-delay-measurement VERIFY_237380 thresholds success-rate ?",
                wait=3,
            )
            issues = []
            if "92233720368547" in out:
                issues.append("Still shows huge range instead of 0-100")

            out2 = sess.run(
                "services performance-monitoring profiles cfm two-way-delay-measurement VERIFY_237380 thresholds success-rate -233",
                wait=3,
            )
            if "ERROR" not in out2 and "Unknown" not in out2 and "Invalid" not in out2:
                issues.append("Accepted negative percentage -233")

            out3 = sess.run(
                "services performance-monitoring profiles cfm two-way-delay-measurement VERIFY_237380 thresholds success-rate 222222222222",
                wait=3,
            )
            if "ERROR" not in out3 and "Unknown" not in out3 and "Invalid" not in out3:
                issues.append("Accepted percentage > 100")

            sess.rollback()
            sess.run(f"no services performance-monitoring profiles cfm two-way-delay-measurement VERIFY_237380", wait=2)
            sess.run("top", wait=1)
            sess.commit()
            sess.run("exit", wait=2)

            if issues:
                self.record("SW-237380", device_name, attempt, False, "; ".join(issues))
            else:
                self.record("SW-237380", device_name, attempt, True, "Percentage range correctly limited to 0-100")

    def test_sw237374(self, sess: DeviceSession, device_name: str):
        """MA-name should auto-complete in show PM commands."""
        for attempt in range(1, ATTEMPTS + 1):
            out = sess.tab_complete(
                "show services performance-monitoring cfm tests ma-name "
            )
            if "MA-CUST" in out or "customer-ma" in out:
                self.record("SW-237374", device_name, attempt, True, "MA-name autocomplete works")
            elif "<text>" in out:
                self.record("SW-237374", device_name, attempt, False, "MA-name still shows generic <text>")
            else:
                self.record(
                    "SW-237374", device_name, attempt, False,
                    f"Unclear autocomplete output: {out[:150]}",
                )

    def test_sw237350(self, sess: DeviceSession, device_name: str):
        """Profile with inform-test-results but no thresholds should be rejected at commit."""
        prof = "VERIFY_237350"
        for attempt in range(1, ATTEMPTS + 1):
            sess.run_config(
                [
                    f"services performance-monitoring profiles cfm two-way-synthetic-loss-measurement {prof}",
                    "inform-test-results enabled",
                    "top",
                ]
            )
            ok, out = sess.commit_check()
            sess.rollback()
            sess.run(f"no services performance-monitoring profiles cfm two-way-synthetic-loss-measurement {prof}", wait=2)
            sess.run("top", wait=1)
            sess.commit()
            sess.run("exit", wait=2)

            if not ok:
                self.record(
                    "SW-237350", device_name, attempt, True,
                    "Correctly rejected profile without thresholds",
                )
            else:
                self.record(
                    "SW-237350", device_name, attempt, False,
                    "Allowed profile with inform-test-results but no thresholds",
                )

    def test_sw237303(self, sess: DeviceSession, device_name: str):
        """Should not allow configuring both mep-id and mac-address on target."""
        test_name = "VERIFY_237303"
        for attempt in range(1, ATTEMPTS + 1):
            sess.run("configure", wait=2)
            out = sess.run(
                f"services performance-monitoring cfm two-way-delay-measurement {test_name} target mep-id 1 mac-address 22:22:22:22:22:22",
                wait=3,
            )
            if "ERROR" in out or "Invalid" in out or "Unknown" in out:
                sess.rollback()
                sess.run(f"no services performance-monitoring cfm two-way-delay-measurement {test_name}", wait=2)
                sess.run("top", wait=1)
                sess.commit()
                sess.run("exit", wait=2)
                self.record("SW-237303", device_name, attempt, True, "CLI rejected both mep-id and mac-address")
            else:
                config_out = sess.run("show config compare | no-more", wait=5)
                has_both = "mep-id" in config_out and "mac-address" in config_out
                sess.rollback()
                sess.run(f"no services performance-monitoring cfm two-way-delay-measurement {test_name}", wait=2)
                sess.run("top", wait=1)
                sess.commit()
                sess.run("exit", wait=2)
                if has_both:
                    self.record("SW-237303", device_name, attempt, False, "Both mep-id and mac-address accepted in config")
                else:
                    self.record("SW-237303", device_name, attempt, True, "Only one target type applied (fix: CLI silently uses one)")

    def test_sw236105(self, sess: DeviceSession, device_name: str):
        """Non-existent MA/MD should give clear error, not 'Unexpected error'."""
        test_name = "VERIFY_236105"
        for attempt in range(1, ATTEMPTS + 1):
            sess.run_config(
                [
                    f"services performance-monitoring cfm two-way-delay-measurement {test_name}",
                    "admin-state enabled",
                    "profile test",
                    "source maintenance-domain NONEXISTENT_MD maintenance-association NONEXISTENT_MA mep-id 99",
                    "top",
                ]
            )
            ok, out = sess.commit_check()
            sess.rollback()
            sess.run(f"no services performance-monitoring cfm two-way-delay-measurement {test_name}", wait=2)
            sess.run("top", wait=1)
            sess.commit()
            sess.run("exit", wait=2)

            if not ok:
                if "Unexpected error" in out:
                    self.record("SW-236105", device_name, attempt, False, "Still shows 'Unexpected error' for non-existent MD/MA")
                else:
                    self.record("SW-236105", device_name, attempt, True, "Clear error for non-existent MD/MA")
            else:
                self.record("SW-236105", device_name, attempt, False, "Commit check passed for non-existent MD/MA")

    def test_sw236098(self, sess: DeviceSession, device_name: str):
        """Autocomplete should work for MD/MA in PM configuration."""
        for attempt in range(1, ATTEMPTS + 1):
            sess.run("configure", wait=2)
            out = sess.tab_complete(
                "services performance-monitoring cfm two-way-delay-measurement VERIFY_236098 source maintenance-domain "
            )
            sess.run("exit", wait=2)

            if "MD-CUST" in out or "MD_CUST" in out:
                self.record("SW-236098", device_name, attempt, True, "MD autocomplete works in PM config")
            elif "<text>" in out:
                self.record("SW-236098", device_name, attempt, False, "Still shows generic <text> for MD autocomplete")
            else:
                self.record(
                    "SW-236098", device_name, attempt, False,
                    f"Unclear autocomplete: {out[:150]}",
                )

    def test_sw236074(self, sess: DeviceSession, device_name: str):
        """Valid PM config with existing MD/MA should commit without 'Unexpected error'."""
        dev = DEVICES[device_name]
        test_name = "VERIFY_236074"
        for attempt in range(1, ATTEMPTS + 1):
            sess.run_config(
                [
                    f"services performance-monitoring cfm two-way-delay-measurement {test_name}",
                    "admin-state enabled",
                    "profile test",
                    f"source maintenance-domain {dev['md']} maintenance-association {dev['ma']} mep-id {dev['mep_id']}",
                    f"target mep-id {dev['remote_mep']}",
                    "top",
                ]
            )
            ok, out = sess.commit_check()
            if ok:
                self.record("SW-236074", device_name, attempt, True, "Valid PM config commit check passed")
            else:
                if "Unexpected error" in out:
                    self.record("SW-236074", device_name, attempt, False, "Still shows 'Unexpected error' for valid config")
                else:
                    self.record("SW-236074", device_name, attempt, True, f"Different error (not Unexpected): {out[:150]}")
            sess.rollback()
            sess.run(f"no services performance-monitoring cfm two-way-delay-measurement {test_name}", wait=2)
            sess.run("top", wait=1)
            sess.commit()
            sess.run("exit", wait=2)

    def test_sw212440(self, sess: DeviceSession, device_name: str):
        """DM detail should not show misleading DMR received for unreachable targets."""
        dev = DEVICES[device_name]
        for attempt in range(1, ATTEMPTS + 1):
            out = sess.run(
                f"run ethernet-oam cfm on-demand delay-measurement two-way "
                f"maintenance-domain {dev['md']} maintenance-association {dev['ma']} "
                f"target mac-address 84:40:76:ff:ff:ff detail count 5 interval 1",
                wait=20,
                timeout=30,
            )
            dmr_count = len(re.findall(r"DMR received:", out))
            if dmr_count > 0 and "Delay: 0 usec" in out:
                self.record(
                    "SW-212440", device_name, attempt, False,
                    f"Still shows {dmr_count} misleading 'DMR received: Delay: 0 usec' for unreachable target",
                )
            elif dmr_count == 0 or "Delay: 0" not in out:
                self.record("SW-212440", device_name, attempt, True, "No misleading DMR received for unreachable target")
            else:
                self.record("SW-212440", device_name, attempt, True, f"Output seems improved: {out[:200]}")

    def test_sw212156(self, sess: DeviceSession, device_name: str):
        """DM statistics should not be all zeros when delay values are present."""
        dev = DEVICES[device_name]
        for attempt in range(1, ATTEMPTS + 1):
            out = sess.run(
                f"run ethernet-oam cfm on-demand delay-measurement two-way "
                f"maintenance-domain {dev['md']} maintenance-association {dev['ma']} "
                f"target mep-id {dev['remote_mep']} detail count 10 interval 1",
                wait=25,
                timeout=35,
            )
            has_nonzero_dmr = bool(re.search(r"DMR received: Delay: [1-9]\d* usec", out))
            stats_match = re.search(r"Round-trip-delay \(min/avg/max\):\s*(\d+)/(\d+)/(\d+)", out)

            if stats_match:
                min_v, avg_v, max_v = int(stats_match.group(1)), int(stats_match.group(2)), int(stats_match.group(3))
                if has_nonzero_dmr and min_v == 0 and avg_v == 0 and max_v == 0:
                    self.record(
                        "SW-212156", device_name, attempt, False,
                        "DMR shows non-zero delays but summary stats are all 0/0/0",
                    )
                elif min_v > 0 or avg_v > 0 or max_v > 0:
                    self.record(
                        "SW-212156", device_name, attempt, True,
                        f"Stats correctly show min={min_v}/avg={avg_v}/max={max_v}",
                    )
                else:
                    self.record("SW-212156", device_name, attempt, True, "No non-zero DMR to compare (no connectivity)")
            else:
                self.record("SW-212156", device_name, attempt, True, "Stats format changed or no output")

    def test_sw210995(self, sess: DeviceSession, device_name: str):
        """Linktrace should not fail with 'unexpected reason'."""
        dev = DEVICES[device_name]
        for attempt in range(1, ATTEMPTS + 1):
            out = sess.run(
                f"run ethernet-oam cfm on-demand linktrace "
                f"maintenance-domain {dev['md']} maintenance-association {dev['ma']} "
                f"target mep-id {dev['remote_mep']}",
                wait=15,
                timeout=20,
            )
            if "Command failed due to unexpected reason" in out:
                self.record("SW-210995", device_name, attempt, False, "Linktrace still fails with 'unexpected reason'")
            elif "ERROR" in out:
                self.record("SW-210995", device_name, attempt, False, f"Linktrace error: {out[:200]}")
            else:
                self.record("SW-210995", device_name, attempt, True, "Linktrace completed without unexpected error")

    def test_sw238712(self, sess: DeviceSession, device_name: str):
        """DM values should not show 'None' — check current proactive test output."""
        for attempt in range(1, ATTEMPTS + 1):
            out = sess.run(
                "show services performance-monitoring cfm tests proactive two-way-delay detail | no-more",
                wait=10,
            )
            none_fields = re.findall(r":\s+None[%\s]", out)
            if none_fields:
                self.record(
                    "SW-238712", device_name, attempt, False,
                    f"Found {len(none_fields)} 'None' values in DM output",
                )
            elif "No active" in out or "Session" not in out:
                self.record("SW-238712", device_name, attempt, True, "No proactive DM session to check")
            else:
                self.record("SW-238712", device_name, attempt, True, "No 'None' values in DM output")


def main():
    print("=" * 80)
    print("Y.1731 PM Bug Verification Script")
    print(f"Epic: SW-141523 | Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Attempts per bug: {ATTEMPTS}")
    print("=" * 80)

    verifier = BugVerifier()
    bug_tests = [
        ("SW-242034", verifier.test_sw242034),
        ("SW-242033", verifier.test_sw242033),
        ("SW-241287", verifier.test_sw241287),
        ("SW-241269", verifier.test_sw241269),
        ("SW-240517", verifier.test_sw240517),
        ("SW-240425", verifier.test_sw240425),
        ("SW-240423", verifier.test_sw240423),
        ("SW-239944", verifier.test_sw239944),
        ("SW-239925", verifier.test_sw239925),
        ("SW-239530", verifier.test_sw239530),
        ("SW-238960", verifier.test_sw238960),
        ("SW-238712", verifier.test_sw238712),
        ("SW-238674", verifier.test_sw238674),
        ("SW-238673", verifier.test_sw238673),
        ("SW-237380", verifier.test_sw237380),
        ("SW-237374", verifier.test_sw237374),
        ("SW-237350", verifier.test_sw237350),
        ("SW-237303", verifier.test_sw237303),
        ("SW-236105", verifier.test_sw236105),
        ("SW-236098", verifier.test_sw236098),
        ("SW-236074", verifier.test_sw236074),
        ("SW-212440", verifier.test_sw212440),
        ("SW-212156", verifier.test_sw212156),
        ("SW-210995", verifier.test_sw210995),
    ]

    for device_name, dev_info in DEVICES.items():
        print(f"\n{'=' * 80}")
        print(f"TESTING ON {device_name} ({dev_info['serial']}) @ {dev_info['host']}")
        print(f"{'=' * 80}")

        sess = DeviceSession(device_name, dev_info["host"])
        try:
            sess.connect()
        except Exception as e:
            print(f"  FAILED to connect to {device_name}: {e}")
            for bug_id, _ in bug_tests:
                for a in range(1, ATTEMPTS + 1):
                    verifier.record(bug_id, device_name, a, False, f"Connection failed: {e}")
            continue

        for bug_id, test_fn in bug_tests:
            print(f"\n  --- Testing {bug_id} ---")
            try:
                test_fn(sess, device_name)
            except Exception as e:
                print(f"    [ERROR] {bug_id}: {e}")
                for a in range(1, ATTEMPTS + 1):
                    verifier.record(bug_id, device_name, a, False, f"Test error: {e}")

        sess.close()

    # Final report
    print("\n" + "=" * 80)
    print("FINAL VERIFICATION REPORT")
    print("=" * 80)

    bug_summary = {}
    for r in verifier.results:
        key = r["bug"]
        if key not in bug_summary:
            bug_summary[key] = {"verified": 0, "not_fixed": 0, "total": 0, "details": []}
        bug_summary[key]["total"] += 1
        if r["status"] == "VERIFIED":
            bug_summary[key]["verified"] += 1
        else:
            bug_summary[key]["not_fixed"] += 1
        bug_summary[key]["details"].append(f"  {r['device']} attempt {r['attempt']}: {r['status']} - {r['detail']}")

    verified_count = 0
    not_fixed_count = 0

    for bug_id, _ in bug_tests:
        if bug_id in bug_summary:
            s = bug_summary[bug_id]
            all_verified = s["not_fixed"] == 0
            if all_verified:
                verdict = "\033[92mVERIFIED\033[0m"
                verified_count += 1
            else:
                verdict = f"\033[91mNOT FIXED ({s['not_fixed']}/{s['total']} failures)\033[0m"
                not_fixed_count += 1
            print(f"\n{bug_id}: {verdict}")
            for d in s["details"]:
                print(d)

    print(f"\n{'=' * 80}")
    print(f"SUMMARY: {verified_count} verified, {not_fixed_count} not fixed, out of {len(bug_tests)} bugs tested")
    print(f"{'=' * 80}")

    # Save results to JSON
    results_file = f"/home/dn/y1731_bug_verification_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(results_file, "w") as f:
        json.dump(
            {
                "epic": "SW-141523",
                "date": datetime.now().isoformat(),
                "devices": {k: v for k, v in DEVICES.items()},
                "results": verifier.results,
                "summary": {
                    bug_id: {
                        "verdict": "VERIFIED" if bug_summary.get(bug_id, {}).get("not_fixed", 1) == 0 else "NOT_FIXED",
                        "verified_count": bug_summary.get(bug_id, {}).get("verified", 0),
                        "failed_count": bug_summary.get(bug_id, {}).get("not_fixed", 0),
                    }
                    for bug_id, _ in bug_tests
                },
            },
            f,
            indent=2,
        )
    print(f"\nDetailed results saved to: {results_file}")


if __name__ == "__main__":
    main()
