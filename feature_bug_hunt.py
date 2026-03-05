#!/usr/bin/env python3
"""
Feature-level bug hunting for Y.1731 Proactive PM on DNOS.
Tests actual product behavior, not test script issues.
"""
import sys, time, re, paramiko, json

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

class DeviceFeatureTester:
    def __init__(self, host, user="dnroot", password="dnroot"):
        self.host = host
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(host, username=user, password=password,
                           timeout=15, banner_timeout=15, auth_timeout=15)
        self.findings = []

    def close(self):
        self.client.close()

    def run_seq(self, commands, timeout=30):
        ch = self.client.invoke_shell()
        ch.settimeout(timeout)
        time.sleep(1.5)
        while ch.recv_ready():
            ch.recv(65536)
        results = []
        for cmd in commands:
            ch.send(cmd + "\n")
            out = ""
            end_t = time.time() + timeout
            last_data = time.time()
            while time.time() < end_t:
                if ch.recv_ready():
                    out += ch.recv(65536).decode(errors="ignore")
                    last_data = time.time()
                else:
                    if time.time() - last_data > 3:
                        break
                    time.sleep(0.2)
            results.append((cmd, ANSI.sub("", out)))
        ch.close()
        return results

    def run_single(self, cmd, timeout=20):
        return self.run_seq([cmd], timeout=timeout)[0][1]

    def has_error(self, text):
        for p in ["ERROR:", "Error:", "Commit check failed",
                   "commit check has failed", "Commit failed",
                   "Command failed", "TRANSACTION_COMMIT_CHECK_FAILED",
                   "rpc-error"]:
            if p.lower() in text.lower():
                return True
        return False

    def finding(self, severity, title, detail):
        self.findings.append((severity, title, detail))
        tag = {"BUG": "!!!", "OBSERVATION": "???", "OK": "   "}.get(severity, "   ")
        print(f"  [{tag}] [{severity}] {title}")
        print(f"        {detail[:200]}")

    def cleanup(self, sessions=None, profiles=None):
        cmds = ["configure"]
        for s in (sessions or []):
            cmds.append(f"no services performance-monitoring cfm two-way-delay-measurement {s}")
        for s in (sessions or []):
            cmds.append(f"no services performance-monitoring cfm two-way-synthetic-loss-measurement {s}")
        for p in (profiles or []):
            cmds.append(f"no services performance-monitoring profiles cfm two-way-delay-measurement {p}")
        for p in (profiles or []):
            cmds.append(f"no services performance-monitoring profiles cfm two-way-synthetic-loss-measurement {p}")
        cmds.extend(["commit", "exit"])
        self.run_seq(cmds, timeout=30)

    # ================================================================
    # TEST 1: Threshold violation -> CFM_PROACTIVE_TEST_FAILURE event
    # ================================================================
    def test_threshold_event(self, md, ma, mep, target):
        print(f"\n{'='*70}")
        print(f"TEST 1: Threshold violation event (CFM_PROACTIVE_TEST_FAILURE)")
        print(f"{'='*70}")

        prof = "FEAT_LOW_THRESH_P"
        sess = "FEAT_LOW_THRESH_S"

        # Step 1: Open logging channel for real-time event capture
        log_ch = self.client.invoke_shell()
        log_ch.settimeout(5)
        time.sleep(1.5)
        while log_ch.recv_ready():
            log_ch.recv(65536)
        log_ch.send("set logging terminal\n")
        time.sleep(2)
        while log_ch.recv_ready():
            log_ch.recv(65536)
        print("  Opened logging terminal channel.")

        # Step 2: Create profile with impossibly low thresholds
        cmds = [
            "configure",
            f"services performance-monitoring profiles cfm two-way-delay-measurement {prof}",
            "inform-test-results enabled",
            "test-duration probes probe-count 3 probe-interval 1 repeat-interval 5",
            "thresholds delay-rtt-max 1",
            "exit", "exit", "exit", "exit", "exit",
            f"services performance-monitoring cfm two-way-delay-measurement {sess}",
            "admin-state enabled",
            f"profile {prof}",
            f"source maintenance-domain {md} maintenance-association {ma} mep-id {mep}",
            f"target mep-id {target}",
            "exit", "exit", "exit", "exit",
            "commit",
            "exit",
        ]
        outs = self.run_seq(cmds, timeout=45)

        commit_err = False
        for c, o in outs:
            if c == "commit" and self.has_error(o):
                commit_err = True
                # Check if MEP is in use
                if "in use" in o.lower():
                    print(f"  MEP in use, need to free it first.")
                    # Try on MD-CUST1 instead
                    self.run_seq(["configure", "rollback 0", "exit"], timeout=10)
                    log_ch.close()
                    return "MEP_IN_USE"
                self.finding("OBSERVATION", "Low-threshold session commit failed",
                           f"Could not create test session: {o[:150]}")
                self.run_seq(["configure", "rollback 0", "exit"], timeout=10)
                log_ch.close()
                return

        if commit_err:
            log_ch.close()
            return

        print(f"  Created low-threshold DM session ({sess}), waiting for probes...")

        # Step 3: Wait for probes to run (5+5+buffer = 15s)
        time.sleep(18)

        # Step 4: Read logging channel for events
        event_output = ""
        try:
            log_ch.settimeout(2)
            while log_ch.recv_ready():
                event_output += log_ch.recv(65536).decode(errors="ignore")
        except:
            pass
        event_output = ANSI.sub("", event_output)

        print(f"  Logging channel captured {len(event_output)} bytes.")

        has_event = "CFM_PROACTIVE_TEST_FAILURE" in event_output
        has_any_event = "CFM" in event_output or "PROACTIVE" in event_output or "THRESHOLD" in event_output

        if has_event:
            self.finding("OK", "Threshold event generated",
                        "CFM_PROACTIVE_TEST_FAILURE event detected in logging terminal.")
            # Check event content for required fields
            required_fields = ["test-name", "test-type", "md-name", "ma-name", "mep-id"]
            missing = [f for f in required_fields if f not in event_output.lower()]
            if missing:
                self.finding("BUG", "Threshold event missing required fields",
                           f"CFM_PROACTIVE_TEST_FAILURE event is missing: {missing}")
        elif has_any_event:
            self.finding("OBSERVATION", "CFM event detected but not expected name",
                        f"Event output: {event_output[:300]}")
        else:
            self.finding("BUG", "No threshold violation event generated",
                        f"delay-rtt-max=1 usec should be violated (actual ~13 usec) but no CFM_PROACTIVE_TEST_FAILURE event seen. "
                        f"Logging output ({len(event_output)} bytes): {event_output[:200]}")

        # Step 5: Check proactive test detail for threshold status
        detail = self.run_single(
            f"show services performance-monitoring cfm tests proactive two-way-delay session-name {sess} detail | no-more",
            timeout=15)
        print(f"\n  Session detail:")
        for ln in detail.splitlines():
            s = ln.strip()
            if s and not s.startswith("show "):
                print(f"    {s}")

        # Cleanup
        log_ch.close()
        self.cleanup(sessions=[sess], profiles=[prof])

    # ================================================================
    # TEST 2: Profile modification while session is active
    # ================================================================
    def test_profile_mod_while_active(self, md, ma, mep, target):
        print(f"\n{'='*70}")
        print(f"TEST 2: Profile modification while session is active")
        print(f"{'='*70}")

        prof = "FEAT_MOD_PROF"
        sess = "FEAT_MOD_SESS"

        # Create profile + session
        cmds = [
            "configure",
            f"services performance-monitoring profiles cfm two-way-delay-measurement {prof}",
            "inform-test-results enabled",
            "test-duration probes probe-count 5 probe-interval 1 repeat-interval 10",
            "thresholds delay-rtt-max 5000",
            "exit", "exit", "exit", "exit", "exit",
            f"services performance-monitoring cfm two-way-delay-measurement {sess}",
            "admin-state enabled",
            f"profile {prof}",
            f"source maintenance-domain {md} maintenance-association {ma} mep-id {mep}",
            f"target mep-id {target}",
            "exit", "exit", "exit", "exit",
            "commit",
            "exit",
        ]
        outs = self.run_seq(cmds, timeout=45)
        for c, o in outs:
            if c == "commit" and self.has_error(o):
                if "in use" in o.lower():
                    self.run_seq(["configure", "rollback 0", "exit"], timeout=10)
                    return "MEP_IN_USE"
                self.finding("OBSERVATION", "Cannot create test session for profile mod test", o[:150])
                self.run_seq(["configure", "rollback 0", "exit"], timeout=10)
                return

        print("  Session active. Waiting 5s for initial probes...")
        time.sleep(5)

        # Get baseline detail
        detail1 = self.run_single(
            f"show services performance-monitoring cfm tests proactive two-way-delay session-name {sess} detail | no-more")
        idx1 = re.findall(r"Index\s+(\d+)", detail1)
        print(f"  Baseline indices: {idx1[-3:] if idx1 else 'none'}")

        # NOW modify the profile threshold while session is active
        print("  Modifying profile threshold from 5000 -> 1 (should trigger violation)...")
        mod_cmds = [
            "configure",
            f"services performance-monitoring profiles cfm two-way-delay-measurement {prof}",
            "thresholds delay-rtt-max 1",
            "exit", "exit", "exit", "exit", "exit",
            "commit",
            "exit",
        ]
        mod_outs = self.run_seq(mod_cmds, timeout=30)
        mod_err = False
        for c, o in mod_outs:
            if c == "commit":
                if self.has_error(o):
                    mod_err = True
                    self.finding("BUG", "Cannot modify profile while session active",
                               f"Commit failed when changing threshold on in-use profile: {o[:150]}")
                else:
                    print("  Profile modification committed successfully.")

        if not mod_err:
            # Wait for new probes with new threshold
            time.sleep(12)
            detail2 = self.run_single(
                f"show services performance-monitoring cfm tests proactive two-way-delay session-name {sess} detail | no-more")
            idx2 = re.findall(r"Index\s+(\d+)", detail2)
            print(f"  Post-mod indices: {idx2[-3:] if idx2 else 'none'}")

            # Check if threshold is reflected
            if "delay-rtt-max" in detail2 or "Threshold" in detail2 or "1 usec" in detail2:
                self.finding("OK", "Modified threshold reflected in detail",
                           "Profile change propagated to active session.")
            else:
                # Check if the profile values are actually applied
                prof_check = self.run_single(
                    f"show config services performance-monitoring profiles cfm two-way-delay-measurement {prof} | no-more")
                if "delay-rtt-max 1" in prof_check:
                    self.finding("OK", "Profile modification persisted in config",
                               "Threshold changed to 1 in config.")
                else:
                    self.finding("BUG", "Profile modification not persisted",
                               f"Expected delay-rtt-max 1 but config shows: {prof_check[:200]}")

        # Cleanup
        self.cleanup(sessions=[sess], profiles=[prof])

    # ================================================================
    # TEST 3: SLM to non-existent MAC - check loss stats
    # ================================================================
    def test_slm_nonexistent_mac(self):
        print(f"\n{'='*70}")
        print(f"TEST 3: SLM session targeting non-existent MAC")
        print(f"{'='*70}")

        # Check xec1e3vr00008's SLM_CLI_TAB_mep3 which targets 22:22:22:22:22:22
        if self.host == "xec1e3vr00008":
            detail = self.run_single(
                "show services performance-monitoring cfm tests proactive two-way-synthetic-loss session-name SLM_CLI_TAB_mep3 detail | no-more")
            print("  SLM to MAC 22:22:22:22:22:22 detail:")
            for ln in detail.splitlines():
                s = ln.strip()
                if s and not s.startswith("show "):
                    print(f"    {s}")

            # Check loss stats
            near_loss = re.search(r"Near-end loss.*?:\s*(\S+)", detail)
            far_loss = re.search(r"Far-end loss.*?:\s*(\S+)", detail)
            success = re.search(r"Success rate.*?:\s*(\S+)", detail)
            slr_received = re.search(r"SLR PDUs received.*?:\s*(\d+)", detail)
            slm_transmitted = re.search(r"SLM PDUs transmitted.*?:\s*(\d+)", detail)

            if slr_received and slm_transmitted:
                rx = int(slr_received.group(1))
                tx = int(slm_transmitted.group(1))
                if rx == 0 and tx > 0:
                    self.finding("OBSERVATION", "SLM to fake MAC: 0 responses received",
                               f"TX={tx}, RX={rx}. Session shows ongoing even with 0% success against non-existent MAC.")
                    # Check if status is still 'valid'
                    if "valid" in detail.lower() and "invalid" not in detail.lower():
                        self.finding("BUG", "SLM session reports 'valid' with 0% success to fake MAC",
                                   "Session targeting non-existent MAC 22:22:22:22:22:22 should not report valid status.")
                elif rx > 0:
                    self.finding("OBSERVATION", "SLM to fake MAC getting responses",
                               f"TX={tx}, RX={rx}. Something is responding to 22:22:22:22:22:22.")
            elif "ERROR" in detail or "Unknown" in detail:
                self.finding("OBSERVATION", "Cannot get SLM detail", detail[:200])
            else:
                self.finding("OBSERVATION", "SLM detail output format unexpected", detail[:300])
        else:
            # On WKY1C7VD00008P2, check the regular SLM session
            detail = self.run_single(
                "show services performance-monitoring cfm tests proactive two-way-synthetic-loss session-name SLM_CLI_TAB detail | no-more")
            print("  SLM_CLI_TAB detail:")
            for ln in detail.splitlines():
                s = ln.strip()
                if s and not s.startswith("show "):
                    print(f"    {s}")

            # Check for SLM far-end loss > 0 (unexpected if both sides are connected)
            far_loss = re.search(r"Far-end loss percentage:\s*(\S+)", detail)
            if far_loss:
                loss_val = far_loss.group(1).replace("%", "")
                try:
                    if float(loss_val) > 0:
                        self.finding("BUG", f"SLM far-end loss {loss_val}% between connected MEPs",
                                   "Far-end loss should be 0% between directly connected MEPs with healthy link.")
                    else:
                        self.finding("OK", "SLM far-end loss is 0%", "Expected for connected MEPs.")
                except ValueError:
                    pass

    # ================================================================
    # TEST 4: On-demand interferes with proactive
    # ================================================================
    def test_od_proactive_interference(self, md, ma, mep, target):
        print(f"\n{'='*70}")
        print(f"TEST 4: On-demand test interference with proactive session")
        print(f"{'='*70}")

        # Get baseline proactive state
        detail_before = self.run_single(
            "show services performance-monitoring cfm tests proactive two-way-delay session-name DM_CLI_TAB detail | no-more")
        idx_before = re.findall(r"\|\s*(\d+)\s*\|.*?\|\s*(valid|invalid|incomplete)\s*\|", detail_before)
        print(f"  Before on-demand: last 3 indices = {idx_before[-3:] if idx_before else 'none'}")

        # Run an on-demand DM
        print("  Running on-demand DM (10 probes)...")
        od_out = self.run_single(
            f"run ethernet-oam cfm on-demand delay-measurement two-way maintenance-domain {md} maintenance-association {ma} target mep-id {target} count 10",
            timeout=30)
        for ln in od_out.splitlines():
            s = ln.strip()
            if s and ("PDU" in s or "delay" in s or "Success" in s or "Jitter" in s):
                print(f"    {s}")

        # Wait for proactive to cycle
        time.sleep(5)

        # Check proactive state after
        detail_after = self.run_single(
            "show services performance-monitoring cfm tests proactive two-way-delay session-name DM_CLI_TAB detail | no-more")
        idx_after = re.findall(r"\|\s*(\d+)\s*\|.*?\|\s*(valid|invalid|incomplete)\s*\|", detail_after)
        print(f"  After on-demand: last 3 indices = {idx_after[-3:] if idx_after else 'none'}")

        # Check for any 'invalid' entries that appeared during/after on-demand
        new_invalids = []
        before_idxs = set(i[0] for i in idx_before)
        for idx_num, status in idx_after:
            if idx_num not in before_idxs and status == "invalid":
                new_invalids.append(idx_num)

        if new_invalids:
            self.finding("BUG", "On-demand test causes proactive test entries to go invalid",
                       f"Proactive DM indices {new_invalids} became 'invalid' during on-demand DM test. "
                       "On-demand and proactive should not interfere with each other.")
        else:
            self.finding("OK", "On-demand test did not interfere with proactive",
                       "No new invalid entries in proactive session during on-demand test.")

    # ================================================================
    # TEST 5: Admin-state disable/enable cycle
    # ================================================================
    def test_admin_toggle(self, session_name="DM_CLI_TAB"):
        print(f"\n{'='*70}")
        print(f"TEST 5: Admin-state disable/enable on {session_name}")
        print(f"{'='*70}")

        # Get current state
        detail1 = self.run_single(
            f"show services performance-monitoring cfm tests proactive two-way-delay session-name {session_name} detail | no-more")
        status_before = "Ongoing" if "Ongoing" in detail1 else ("valid" if "valid" in detail1 else "unknown")
        uptime_match = re.search(r"Uptime:\s*(.+)", detail1)
        uptime_before = uptime_match.group(1).strip() if uptime_match else "unknown"
        print(f"  Before: status={status_before}, uptime={uptime_before}")

        # Disable
        print("  Disabling session...")
        self.run_seq([
            "configure",
            f"services performance-monitoring cfm two-way-delay-measurement {session_name}",
            "admin-state disabled",
            "exit", "exit", "exit", "exit",
            "commit", "exit"
        ], timeout=30)
        time.sleep(3)

        # Check disabled state
        detail_dis = self.run_single(
            f"show services performance-monitoring cfm tests proactive two-way-delay session-name {session_name} detail | no-more")
        if "disabled" in detail_dis.lower() or self.has_error(detail_dis):
            print("  Session disabled (or not visible when disabled).")
        else:
            print(f"  Disabled state detail: ...")
            for ln in detail_dis.splitlines()[-10:]:
                s = ln.strip()
                if s: print(f"    {s}")

        # Re-enable
        print("  Re-enabling session...")
        self.run_seq([
            "configure",
            f"services performance-monitoring cfm two-way-delay-measurement {session_name}",
            "admin-state enabled",
            "exit", "exit", "exit", "exit",
            "commit", "exit"
        ], timeout=30)
        time.sleep(8)

        # Check re-enabled state
        detail2 = self.run_single(
            f"show services performance-monitoring cfm tests proactive two-way-delay session-name {session_name} detail | no-more")

        # Check uptime reset
        uptime_match2 = re.search(r"Uptime:\s*(.+)", detail2)
        uptime_after = uptime_match2.group(1).strip() if uptime_match2 else "unknown"
        print(f"  After re-enable: uptime={uptime_after}")

        # Check if session resumed properly
        if "Ongoing" in detail2 or "incomplete" in detail2 or "valid" in detail2:
            self.finding("OK", "Session resumed after admin-state toggle",
                       f"Session back to active. Uptime before: {uptime_before}, after: {uptime_after}")
        else:
            self.finding("BUG", "Session did not resume after admin-state re-enable",
                       f"Detail after re-enable: {detail2[:200]}")

        # Check if historic index counter reset or continued
        idx2 = re.findall(r"\|\s*(\d+)\s*\|", detail2)
        if idx2:
            last_idx = int(idx2[-1])
            if last_idx < 10:
                self.finding("OBSERVATION", "Historic index reset after admin-state toggle",
                           f"Index went back to {last_idx}. Previous run had thousands of indices. "
                           "N-list history is lost on disable/enable.")
            else:
                self.finding("OK", "Historic index counter preserved",
                           f"Last index: {last_idx}")

    # ================================================================
    # TEST 6: Invalid references and edge cases
    # ================================================================
    def test_invalid_refs(self, md, ma, mep, target):
        print(f"\n{'='*70}")
        print(f"TEST 6: Invalid references and edge cases")
        print(f"{'='*70}")

        # 6a: Session pointing to non-existent profile
        print("\n  6a: Session with non-existent profile reference")
        cmds = [
            "configure",
            "services performance-monitoring cfm two-way-delay-measurement FEAT_BAD_REF",
            "admin-state enabled",
            "profile THIS_PROFILE_DOES_NOT_EXIST",
            f"source maintenance-domain {md} maintenance-association {ma} mep-id {mep}",
            f"target mep-id {target}",
            "exit", "exit", "exit", "exit",
            "commit check",
        ]
        outs = self.run_seq(cmds, timeout=20)
        cc = ""
        for c, o in outs:
            if c == "commit check": cc = o
        if self.has_error(cc):
            self.finding("OK", "Non-existent profile reference rejected at commit",
                       f"Device correctly rejects invalid profile reference.")
        else:
            self.finding("BUG", "Non-existent profile reference accepted",
                       "Session with profile=THIS_PROFILE_DOES_NOT_EXIST should fail commit check.")
        self.run_seq(["rollback 0", "exit"], timeout=10)

        # 6b: Session with non-existent MD
        print("\n  6b: Session with non-existent MD")
        cmds = [
            "configure",
            "services performance-monitoring cfm two-way-delay-measurement FEAT_BAD_MD",
            "admin-state enabled",
            "profile test",
            "source maintenance-domain FAKE_MD maintenance-association FAKE_MA mep-id 99",
            "target mep-id 1",
            "exit", "exit", "exit", "exit",
            "commit check",
        ]
        outs = self.run_seq(cmds, timeout=20)
        cc = ""
        for c, o in outs:
            if c == "commit check": cc = o
        if self.has_error(cc):
            self.finding("OK", "Non-existent MD/MA/MEP rejected at commit",
                       "Device correctly rejects invalid CFM context.")
        else:
            self.finding("BUG", "Non-existent MD/MA/MEP accepted at commit",
                       "Session with FAKE_MD/FAKE_MA/mep-99 should fail commit check.")
        self.run_seq(["rollback 0", "exit"], timeout=10)

        # 6c: Duplicate session name
        print("\n  6c: Attempt to create session with duplicate name (DM_CLI_TAB)")
        cmds = [
            "configure",
            "services performance-monitoring cfm two-way-delay-measurement DM_CLI_TAB",
            "description DUPLICATE_TEST",
            "commit check",
        ]
        outs = self.run_seq(cmds, timeout=20)
        cc = ""
        for c, o in outs:
            if c == "commit check": cc = o
        # This actually modifies the existing session!
        if "DUPLICATE_TEST" in self.run_single(
            "show config services performance-monitoring cfm two-way-delay-measurement DM_CLI_TAB | no-more"):
            self.finding("OBSERVATION", "Modifying existing session via same name overwrites config",
                       "Re-entering session name 'DM_CLI_TAB' modifies existing session (expected CLI behavior).")
            # Revert
            self.run_seq(["configure", "rollback 0", "exit"], timeout=10)
        else:
            self.run_seq(["rollback 0", "exit"], timeout=10)

        # 6d: Session name with special characters
        print("\n  6d: Session name with special characters")
        special_names = ["test@session", "test session", "test/session", "test;drop"]
        for sname in special_names:
            cmds = [
                "configure",
                f"services performance-monitoring cfm two-way-delay-measurement {sname}",
            ]
            outs = self.run_seq(cmds, timeout=10)
            last_out = outs[-1][1] if outs else ""
            if self.has_error(last_out) or "Unknown" in last_out:
                print(f"    '{sname}': Rejected (good)")
            else:
                print(f"    '{sname}': ACCEPTED!")
                self.finding("OBSERVATION", f"Special char session name '{sname}' accepted",
                           "May cause issues in RESTCONF/YANG path encoding.")
            self.run_seq(["exit", "exit", "exit", "exit", "exit"], timeout=5)

    # ================================================================
    # TEST 7: Rapid session create/delete cycles
    # ================================================================
    def test_rapid_create_delete(self, md, ma, mep, target):
        print(f"\n{'='*70}")
        print(f"TEST 7: Rapid create/delete session cycles")
        print(f"{'='*70}")

        prof = "FEAT_RAPID_PROF"
        sess = "FEAT_RAPID_SESS"

        # Create profile first
        self.run_seq([
            "configure",
            f"services performance-monitoring profiles cfm two-way-delay-measurement {prof}",
            "inform-test-results enabled",
            "test-duration probes probe-count 3 probe-interval 1 repeat-interval 5",
            "thresholds delay-rtt-max 5000",
            "exit", "exit", "exit", "exit", "exit",
            "commit", "exit",
        ], timeout=30)

        errors = []
        for i in range(3):
            print(f"  Cycle {i+1}/3: create -> commit -> delete -> commit")
            # Create session
            self.run_seq([
                "configure",
                f"services performance-monitoring cfm two-way-delay-measurement {sess}",
                "admin-state enabled",
                f"profile {prof}",
                f"source maintenance-domain {md} maintenance-association {ma} mep-id {mep}",
                f"target mep-id {target}",
                "exit", "exit", "exit", "exit",
                "commit", "exit",
            ], timeout=30)
            time.sleep(1)

            # Delete session
            del_outs = self.run_seq([
                "configure",
                f"no services performance-monitoring cfm two-way-delay-measurement {sess}",
                "commit", "exit",
            ], timeout=30)
            for c, o in del_outs:
                if c == "commit" and self.has_error(o):
                    errors.append(f"Cycle {i+1} delete: {o[:100]}")
            time.sleep(1)

        if errors:
            self.finding("BUG", "Rapid create/delete cycle causes errors",
                       f"Errors during rapid cycling: {'; '.join(errors)}")
        else:
            self.finding("OK", "Rapid create/delete cycles completed cleanly",
                       "3 create/delete cycles with no errors.")

        # Cleanup profile
        self.cleanup(sessions=[sess], profiles=[prof])

    def print_summary(self):
        print(f"\n{'#'*70}")
        print(f"# FEATURE BUG HUNT SUMMARY: {self.host}")
        print(f"{'#'*70}")
        bugs = [f for f in self.findings if f[0] == "BUG"]
        observations = [f for f in self.findings if f[0] == "OBSERVATION"]
        ok = [f for f in self.findings if f[0] == "OK"]
        print(f"\n  BUGS FOUND: {len(bugs)}")
        for sev, title, detail in bugs:
            print(f"    !!! {title}")
            print(f"        {detail[:200]}")
        print(f"\n  OBSERVATIONS: {len(observations)}")
        for sev, title, detail in observations:
            print(f"    ??? {title}")
        print(f"\n  PASSED: {len(ok)}")
        for sev, title, detail in ok:
            print(f"    OK  {title}")


def main():
    devices = {
        "WKY1C7VD00008P2": {"md": "MD-CUST1", "ma": "MA-CUST1", "mep": "4", "target": "3"},
        "xec1e3vr00008": {"md": "MD-CUST1", "ma": "MA-CUST1", "mep": "3", "target": "4"},
    }

    for host, ctx in devices.items():
        print(f"\n{'#'*70}")
        print(f"# DEVICE: {host}")
        print(f"# Using: {ctx['md']}/{ctx['ma']} MEP {ctx['mep']} -> target {ctx['target']}")
        print(f"{'#'*70}")

        tester = DeviceFeatureTester(host)
        try:
            result = tester.test_threshold_event(ctx["md"], ctx["ma"], ctx["mep"], ctx["target"])
            if result == "MEP_IN_USE":
                print("  MEP in use, trying with MD-CUST MEP for threshold test...")
                # Skip threshold test on this MEP

            tester.test_profile_mod_while_active(ctx["md"], ctx["ma"], ctx["mep"], ctx["target"])
            tester.test_slm_nonexistent_mac()
            tester.test_od_proactive_interference(
                "MD-CUST", "MA-CUST",
                "2" if host == "WKY1C7VD00008P2" else "1",
                "1" if host == "WKY1C7VD00008P2" else "2")
            tester.test_admin_toggle("DM_CLI_TAB")
            tester.test_invalid_refs(ctx["md"], ctx["ma"], ctx["mep"], ctx["target"])
            tester.test_rapid_create_delete(ctx["md"], ctx["ma"], ctx["mep"], ctx["target"])
            tester.print_summary()
        except Exception as e:
            print(f"\n[FATAL] {host}: {e}")
            import traceback
            traceback.print_exc()
        finally:
            tester.close()


if __name__ == "__main__":
    main()
