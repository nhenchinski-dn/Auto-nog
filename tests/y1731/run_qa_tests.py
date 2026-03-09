#!/usr/bin/env python3
"""
QA Test Runner for Y.1731 Proactive Initiator PM (SW-141523)
Connects to DNOS device and exercises proactive DM/SLM sessions.
"""
import pexpect
import sys
import re
import time

HOST = "WKY1C7VD00008P2"
USER = "dnroot"
PASS = "dnroot"
PROMPT = r'[\w\-\(\)]+[#>]\s*\Z'
MORE = r'-- More --'
CONFIRM = r'\[cancel\]\?|\[yes\]|\[no\]'
TIMEOUT = 30

class DnosCLI:
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
        self.child.expect(PROMPT)

    def cmd(self, c, timeout=TIMEOUT):
        self.child.sendline(c)
        output = ""
        while True:
            idx = self.child.expect([PROMPT, MORE, CONFIRM, pexpect.TIMEOUT],
                                   timeout=timeout)
            output += self.child.before
            if idx == 0:
                break
            elif idx == 1:
                self.child.send(" ")
            elif idx == 2:
                output += self.child.after
                break
            else:
                break
        return self._clean(output)

    def confirm(self, answer="no"):
        self.child.sendline(answer)
        output = ""
        idx = self.child.expect([PROMPT, pexpect.TIMEOUT], timeout=15)
        output += self.child.before
        return self._clean(output)

    def _clean(self, text):
        text = re.sub(r'\x1b\[[0-9;]*[mKHJrl]', '', text)
        text = re.sub(r'\x1b\[\?[0-9;]*[hl]', '', text)
        text = re.sub(r'\r\n', '\n', text)
        text = re.sub(r'\r', '', text)
        return text.strip()

    def close(self):
        try:
            self.child.sendline("exit")
            self.child.expect(pexpect.EOF, timeout=5)
        except:
            self.child.close()

def header(name):
    print(f"\n{'='*70}")
    print(f"  TEST: {name}")
    print(f"{'='*70}")

def result(name, passed, detail=""):
    status = "PASS" if passed else "FAIL"
    print(f"\n  [{status}] {name}")
    if detail:
        print(f"         {detail}")
    return passed

def main():
    cli = DnosCLI()
    results = []

    # ===========================================================
    # PHASE 0: Baseline
    # ===========================================================
    header("PHASE 0: Baseline check")
    out = cli.cmd("show services performance-monitoring cfm tests proactive")
    print(out)
    baseline_ok = "Total displayed tests: 0" in out
    results.append(result("Baseline: no proactive tests", baseline_ok))

    # ===========================================================
    # HP-01: Create DM profile + proactive DMM session
    # ===========================================================
    header("HP-01: Create DM profile and proactive DMM session")

    cli.cmd("configure")

    # Create DM profile
    print("\n--- Creating DM profile ---")
    cli.cmd("services performance-monitoring")
    cli.cmd("profiles cfm")
    cli.cmd("two-way-delay-measurement dm-prof1")
    out = cli.cmd("test-duration probes count 5 interval 1")
    print(f"  test-duration: {out}")
    out = cli.cmd("thresholds delay-rtt-avg 500000")
    print(f"  thresh delay-rtt-avg: {out}")
    out = cli.cmd("thresholds jitter-rtt-avg 200000")
    print(f"  thresh jitter-rtt-avg: {out}")
    out = cli.cmd("thresholds success-rate 50")
    print(f"  thresh success-rate: {out}")
    out = cli.cmd("inform-test-results enabled")
    print(f"  inform-test-results: {out}")
    cli.cmd("top")

    # Create proactive DM session
    print("\n--- Creating proactive DM session 'dm-test-1' ---")
    cli.cmd("services performance-monitoring")
    cli.cmd("cfm")
    out = cli.cmd("?")
    print(f"  CFM sub-commands: {out}")

    # Navigate to proactive
    out = cli.cmd("proactive-monitoring ?")
    print(f"  proactive sub: {out}")

    out = cli.cmd("two-way-delay-measurements ?")
    print(f"  dm sub: {out}")

    out = cli.cmd("test-session dm-test-1")
    print(f"  session created: {out}")

    out = cli.cmd("?")
    print(f"  session sub-commands: {out[-400:]}")

    # Set config items
    out = cli.cmd("config-items source-md-name MD-CUST")
    print(f"  source-md: {out}")
    out = cli.cmd("source-ma-name MA-CUST")
    print(f"  source-ma: {out}")
    out = cli.cmd("source-mep-id 2")
    print(f"  source-mep: {out}")
    out = cli.cmd("target mep-id 1")
    print(f"  target: {out}")
    out = cli.cmd("profile dm-prof1")
    print(f"  profile: {out}")
    out = cli.cmd("admin-state enabled")
    print(f"  admin-state: {out}")

    cli.cmd("top")

    print("\n--- Committing ---")
    out = cli.cmd("commit", timeout=60)
    print(f"  COMMIT: {out[-300:]}")
    commit_ok = "Commit complete" in out or "no configuration changes" in out
    if "ERROR" in out:
        commit_ok = False
    results.append(result("HP-01: DM profile+session commit", commit_ok, out[-200:]))

    cli.cmd("exit")

    # Wait for session to run
    print("\n--- Waiting 15s for proactive session to run ---")
    time.sleep(15)

    out = cli.cmd("show services performance-monitoring cfm tests proactive")
    print(out)
    dm_running = "dm-test-1" in out and "two-way-delay" in out.lower()
    results.append(result("HP-01: DM session appears in show output", dm_running, out[:200]))

    # ===========================================================
    # HP-02: Create SLM profile + session
    # ===========================================================
    header("HP-02: Create SLM profile and proactive SLM session")

    cli.cmd("configure")
    cli.cmd("services performance-monitoring")
    cli.cmd("profiles cfm")
    cli.cmd("two-way-synthetic-loss-measurement slm-prof1")
    cli.cmd("test-duration probes count 5 interval 1")
    cli.cmd("thresholds near-end-loss 10")
    cli.cmd("thresholds far-end-loss 10")
    cli.cmd("inform-test-results enabled")
    cli.cmd("top")

    cli.cmd("services performance-monitoring")
    cli.cmd("cfm")
    cli.cmd("proactive-monitoring")
    cli.cmd("two-way-synthetic-loss-measurements")
    cli.cmd("test-session slm-test-1")
    out = cli.cmd("?")
    print(f"  SLM session sub: {out[-300:]}")

    cli.cmd("config-items source-md-name MD-CUST")
    cli.cmd("source-ma-name MA-CUST")
    cli.cmd("source-mep-id 2")
    cli.cmd("target mep-id 1")
    cli.cmd("profile slm-prof1")
    cli.cmd("admin-state enabled")
    cli.cmd("top")

    out = cli.cmd("commit", timeout=60)
    print(f"  COMMIT: {out[-300:]}")
    commit_ok = "Commit complete" in out
    results.append(result("HP-02: SLM profile+session commit", commit_ok, out[-200:]))

    cli.cmd("exit")
    time.sleep(15)

    out = cli.cmd("show services performance-monitoring cfm tests proactive")
    print(out)
    slm_running = "slm-test-1" in out
    results.append(result("HP-02: SLM session appears", slm_running))

    # ===========================================================
    # CO-01: On-demand + proactive same MEP
    # ===========================================================
    header("CO-01: On-demand DM while proactive DM running on same MEP")
    out = cli.cmd("request ethernet-oam cfm on-demand two-way-delay-measurement md-name MD-CUST ma-name MA-CUST mep-id 2 target mep-id 1")
    print(f"  ON-DEMAND: {out}")
    od_result = "ERROR" not in out.upper()
    results.append(result("CO-01: On-demand DM with same MEP", od_result, out[:200]))

    time.sleep(8)
    out = cli.cmd("show services performance-monitoring cfm tests on-demand")
    print(out)

    # ===========================================================
    # BUG-08 TEST: Modify running proactive session
    # ===========================================================
    header("BUG-08: Modify proactive DM session target (insert vs insert_or_assign)")

    print("\n--- Before modification: check current state ---")
    out = cli.cmd("show services performance-monitoring cfm tests proactive")
    print(out)
    before_state = out

    cli.cmd("configure")
    cli.cmd("services performance-monitoring")
    cli.cmd("cfm")
    cli.cmd("proactive-monitoring")
    cli.cmd("two-way-delay-measurements")
    cli.cmd("test-session dm-test-1")

    # Change to target by MAC address instead of MEP ID
    print("\n--- Changing target from mep-id 1 to mac 84:40:76:bd:58:fb ---")
    out = cli.cmd("config-items target mac-address 84:40:76:bd:58:fb")
    print(f"  target change: {out}")
    cli.cmd("top")
    out = cli.cmd("commit", timeout=60)
    print(f"  COMMIT: {out[-300:]}")
    cli.cmd("exit")

    time.sleep(15)

    print("\n--- After modification ---")
    out = cli.cmd("show services performance-monitoring cfm tests proactive")
    print(out)

    # Check if target changed (BUG-08: if insert() doesn't overwrite, target stays old)
    after_state = out
    target_changed = "84:40:76:bd:58:fb" in after_state or after_state != before_state
    results.append(result("BUG-08: Session params updated after modification", target_changed,
                         "If target is still mep-id 1, BUG-08 confirmed"))

    # ===========================================================
    # NI-06: Delete MD while proactive session active
    # ===========================================================
    header("NI-06: Delete MD-CUST while proactive sessions are configured")

    cli.cmd("configure")
    print("\n--- Attempting to delete MD-CUST ---")
    out = cli.cmd("delete services ethernet-oam connectivity-fault-management maintenance-domains MD-CUST")
    print(f"  DELETE: {out}")
    out = cli.cmd("commit", timeout=30)
    print(f"  COMMIT (expect reject): {out[-400:]}")

    should_reject = "ERROR" in out or "Aborted" in out or "rejected" in out.lower() or "failed" in out.lower()
    results.append(result("NI-06: Commit rejected when deleting MD with active proactive",
                         should_reject, out[-200:]))

    # Cancel changes
    out = cli.cmd("exit")
    if "cancel" in out.lower() or "uncommitted" in out.lower():
        cli.confirm("no")

    # ===========================================================
    # HP-08: Historic results (N-list cycling)
    # ===========================================================
    header("HP-08: Check historic results (N-list)")

    # Let sessions run more cycles
    time.sleep(20)

    out = cli.cmd("show services performance-monitoring cfm tests proactive")
    print(out)

    # ===========================================================
    # CLEANUP
    # ===========================================================
    header("CLEANUP: Remove test sessions and profiles")

    cli.cmd("configure")
    cli.cmd("delete services performance-monitoring cfm-tests proactive-monitoring two-way-delay-measurements test-session dm-test-1")
    cli.cmd("delete services performance-monitoring cfm-tests proactive-monitoring two-way-synthetic-loss-measurements test-session slm-test-1")
    cli.cmd("delete services performance-monitoring profiles cfm two-way-delay-measurement dm-prof1")
    cli.cmd("delete services performance-monitoring profiles cfm two-way-synthetic-loss-measurement slm-prof1")
    cli.cmd("top")
    out = cli.cmd("commit", timeout=60)
    print(f"  CLEANUP COMMIT: {out[-300:]}")
    cli.cmd("exit")

    time.sleep(5)
    out = cli.cmd("show services performance-monitoring cfm tests proactive")
    print(f"  AFTER CLEANUP: {out}")
    cleanup_ok = "Total displayed tests: 0" in out
    results.append(result("Cleanup: all test sessions removed", cleanup_ok))

    cli.close()

    # ===========================================================
    # SUMMARY
    # ===========================================================
    print(f"\n{'='*70}")
    print(f"  TEST RESULTS SUMMARY")
    print(f"{'='*70}")
    passed = sum(1 for _, p, *_ in [(r, r) if isinstance(r, bool) else r for r in results])
    total = len(results)
    for r in results:
        pass  # already printed
    print(f"\n  Total: {total} tests")
    print(f"  NOTE: See detailed output above for evidence of each finding.")

if __name__ == "__main__":
    main()
