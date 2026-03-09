#!/usr/bin/env python3
"""DNOS CLI test runner for Y.1731 proactive PM bug verification."""
import pexpect
import sys
import re
import time

HOST = "WKY1C7VD00008P2"
USER = "dnroot"
PASS = "dnroot"
PROMPT_RE = r'[\w\-]+[#>]\s*$'
MORE_RE = r'-- More --'
COMMIT_RE = r'Commit complete|commit confirmed|ERROR|error|Aborted|aborted'
TIMEOUT = 30

class DnosSession:
    def __init__(self):
        cmd = (
            f"sshpass -p '{PASS}' ssh -tt "
            f"-o StrictHostKeyChecking=no "
            f"-o PreferredAuthentications=password,keyboard-interactive "
            f"-o PubkeyAuthentication=no "
            f"{USER}@{HOST}"
        )
        self.child = pexpect.spawn(cmd, encoding='utf-8', timeout=TIMEOUT,
                                   maxread=200000)
        self.child.expect(PROMPT_RE)
        self.in_config = False

    def clean(self, text):
        text = re.sub(r'\x1b\[[0-9;]*[mKHJrl]', '', text)
        text = re.sub(r'\x1b\[\?[0-9;]*[hl]', '', text)
        text = re.sub(r'\r\s+\r', '\n', text)
        text = re.sub(r'\r\n', '\n', text)
        text = re.sub(r'\r', '', text)
        return text.strip()

    def cmd(self, command, timeout=TIMEOUT):
        self.child.sendline(command)
        output = ""
        while True:
            idx = self.child.expect([PROMPT_RE, MORE_RE, pexpect.TIMEOUT],
                                   timeout=timeout)
            output += self.child.before
            if idx == 0:
                break
            elif idx == 1:
                self.child.send(" ")
            else:
                break
        return self.clean(output)

    def config_mode(self):
        if not self.in_config:
            out = self.cmd("configure")
            self.in_config = True
            return out

    def commit(self, timeout=60):
        out = self.cmd("commit", timeout=timeout)
        return out

    def exit_config(self):
        if self.in_config:
            self.cmd("top")
            self.cmd("exit")
            self.in_config = False

    def close(self):
        try:
            if self.in_config:
                self.cmd("top")
                self.cmd("exit discard")
            self.child.sendline("exit")
            self.child.expect(pexpect.EOF, timeout=5)
        except:
            self.child.close()

def test_header(name):
    print(f"\n{'='*70}")
    print(f"TEST: {name}")
    print(f"{'='*70}")

def run_all_tests():
    sess = DnosSession()

    # =========================================================
    # PHASE 0: Baseline - check existing state
    # =========================================================
    test_header("PHASE 0 - Baseline: show existing proactive sessions")
    out = sess.cmd("show services performance-monitoring cfm tests proactive")
    print(out)

    # Check existing profiles
    out = sess.cmd("show services performance-monitoring cfm tests on-demand")
    print(out)

    # =========================================================
    # PHASE 1: Happy path - Create profile + proactive DMM session
    # =========================================================
    test_header("HP-01: Create DM profile and proactive DMM session")

    sess.config_mode()

    # Create DM profile
    print("\n--- Creating DM profile 'dm-profile-test1' ---")
    cmds = [
        "services performance-monitoring profiles cfm two-way-delay-measurement profile dm-profile-test1",
        "config-items test-duration probe-count count 5 interval 1",
        "config-items thresholds delay-rtt-avg 500000",
        "config-items thresholds jitter-rtt-avg 200000",
        "config-items thresholds success-rate 50",
        "top",
    ]
    for c in cmds:
        out = sess.cmd(c)
        print(f"  {c}: {out[-80:] if len(out) > 80 else out}")

    # Create proactive DM session targeting RMEP 1 from MEP 2
    print("\n--- Creating proactive DM session 'test-dm-1' ---")
    cmds = [
        "services performance-monitoring cfm-tests proactive-monitoring two-way-delay-measurements test-session test-dm-1",
        "config-items source-md-name MD-CUST",
        "config-items source-ma-name MA-CUST",
        "config-items source-mep-id 2",
        "config-items target mep-id 1",
        "config-items profile dm-profile-test1",
        "config-items admin-state enabled",
        "top",
    ]
    for c in cmds:
        out = sess.cmd(c)
        print(f"  {c}: {out[-100:] if len(out) > 100 else out}")

    print("\n--- Committing ---")
    out = sess.commit(timeout=60)
    print(f"COMMIT RESULT: {out[-200:]}")

    sess.exit_config()
    time.sleep(5)

    print("\n--- Verifying proactive DM session ---")
    out = sess.cmd("show services performance-monitoring cfm tests proactive")
    print(out)

    # =========================================================
    # PHASE 2: Happy path - Create SLM profile + proactive SLM session
    # =========================================================
    test_header("HP-02: Create SLM profile and proactive SLM session")

    sess.config_mode()

    # Create SLM profile
    print("\n--- Creating SLM profile 'slm-profile-test1' ---")
    cmds = [
        "services performance-monitoring profiles cfm two-way-synthetic-loss-measurement profile slm-profile-test1",
        "config-items test-duration probe-count count 5 interval 1",
        "config-items thresholds near-end-loss 10",
        "config-items thresholds far-end-loss 10",
        "top",
    ]
    for c in cmds:
        out = sess.cmd(c)
        print(f"  {c}: {out[-80:] if len(out) > 80 else out}")

    # Create proactive SLM session
    print("\n--- Creating proactive SLM session 'test-slm-1' ---")
    cmds = [
        "services performance-monitoring cfm-tests proactive-monitoring two-way-synthetic-loss-measurements test-session test-slm-1",
        "config-items source-md-name MD-CUST",
        "config-items source-ma-name MA-CUST",
        "config-items source-mep-id 2",
        "config-items target mep-id 1",
        "config-items profile slm-profile-test1",
        "config-items admin-state enabled",
        "top",
    ]
    for c in cmds:
        out = sess.cmd(c)
        print(f"  {c}: {out[-100:] if len(out) > 100 else out}")

    print("\n--- Committing ---")
    out = sess.commit(timeout=60)
    print(f"COMMIT RESULT: {out[-200:]}")

    sess.exit_config()
    time.sleep(10)

    print("\n--- Verifying both proactive sessions ---")
    out = sess.cmd("show services performance-monitoring cfm tests proactive")
    print(out)

    # =========================================================
    # BUG-08 TEST: Modify running session params (insert vs insert_or_assign)
    # =========================================================
    test_header("BUG-08: Modify proactive DM session target (should restart with new params)")

    print("\n--- Before modification ---")
    out = sess.cmd("show services performance-monitoring cfm tests proactive")
    print(out)

    sess.config_mode()

    # Modify the DM session - change target from RMEP 1 to RMEP by MAC
    print("\n--- Changing test-dm-1 profile to see if update takes effect ---")
    cmds = [
        "services performance-monitoring cfm-tests proactive-monitoring two-way-delay-measurements test-session test-dm-1",
        "config-items profile dm-profile-test1",
        "top",
    ]
    for c in cmds:
        out = sess.cmd(c)
        print(f"  {c}: {out[-100:] if len(out) > 100 else out}")

    out = sess.commit(timeout=60)
    print(f"COMMIT RESULT: {out[-200:]}")
    sess.exit_config()

    time.sleep(10)

    print("\n--- After modification (should show updated results) ---")
    out = sess.cmd("show services performance-monitoring cfm tests proactive")
    print(out)

    # =========================================================
    # CO-01 TEST: On-demand + proactive for same MEP
    # =========================================================
    test_header("CO-01: On-demand DM while proactive DM is running on same MEP")

    print("\n--- Starting on-demand DM for same MEP 2 ---")
    out = sess.cmd("request ethernet-oam cfm on-demand two-way-delay-measurement md-name MD-CUST ma-name MA-CUST mep-id 2 target mep-id 1")
    print(f"ON-DEMAND RESULT: {out}")

    time.sleep(5)

    out = sess.cmd("show services performance-monitoring cfm tests on-demand")
    print(out)

    # =========================================================
    # NI-06 TEST: Delete MD while proactive session active
    # =========================================================
    test_header("NI-06: Try to delete MD-CUST while proactive session is configured")

    sess.config_mode()
    print("\n--- Attempting to delete MD-CUST ---")
    out = sess.cmd("delete services ethernet-oam connectivity-fault-management maintenance-domains MD-CUST")
    print(f"DELETE CMD: {out}")
    out = sess.commit(timeout=30)
    print(f"COMMIT (should reject): {out[-300:]}")

    # Discard changes
    out = sess.cmd("exit discard")
    sess.in_config = False
    print(f"EXIT DISCARD: {out}")

    # =========================================================
    # Show detailed results with history
    # =========================================================
    test_header("RESULT VERIFICATION: Detailed proactive session results")

    time.sleep(10)

    out = sess.cmd("show services performance-monitoring cfm tests proactive test-session test-dm-1 ?")
    print(f"SUBCOMMANDS: {out}")

    out = sess.cmd("show services performance-monitoring cfm tests proactive")
    print(out)

    # =========================================================
    # CLEANUP: Delete test sessions
    # =========================================================
    test_header("CLEANUP: Delete test proactive sessions and profiles")

    sess.config_mode()

    cmds = [
        "delete services performance-monitoring cfm-tests proactive-monitoring two-way-delay-measurements test-session test-dm-1",
        "delete services performance-monitoring cfm-tests proactive-monitoring two-way-synthetic-loss-measurements test-session test-slm-1",
        "delete services performance-monitoring profiles cfm two-way-delay-measurement profile dm-profile-test1",
        "delete services performance-monitoring profiles cfm two-way-synthetic-loss-measurement profile slm-profile-test1",
        "top",
    ]
    for c in cmds:
        out = sess.cmd(c)
        print(f"  {c}: {out[-100:] if len(out) > 100 else out}")

    out = sess.commit(timeout=60)
    print(f"CLEANUP COMMIT: {out[-200:]}")
    sess.exit_config()

    time.sleep(3)
    out = sess.cmd("show services performance-monitoring cfm tests proactive")
    print(f"AFTER CLEANUP: {out}")

    sess.close()
    print("\n\nDONE - All tests completed.")

if __name__ == "__main__":
    run_all_tests()
