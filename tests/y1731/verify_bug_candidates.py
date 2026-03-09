#!/usr/bin/env python3
"""
Targeted verification of bug candidates on live DNOS devices.
Tests BC-5, BC-7, BC-9, BC-10 directly on hardware.
"""
import sys, time, re
import paramiko

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

class DeviceTester:
    def __init__(self, host, user="dnroot", password="dnroot", timeout=20):
        self.host = host
        self.user = user
        self.password = password
        self.timeout = timeout
        self.client = None
        self.results = []

    def connect(self):
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(self.host, username=self.user, password=self.password,
                           timeout=self.timeout, banner_timeout=self.timeout,
                           auth_timeout=self.timeout)
        transport = self.client.get_transport()
        if transport:
            transport.set_keepalive(30)

    def disconnect(self):
        if self.client:
            self.client.close()

    def run_sequence(self, commands, timeout=30):
        ch = self.client.invoke_shell()
        ch.settimeout(timeout)
        time.sleep(1.5)
        while ch.recv_ready():
            ch.recv(65536)
        
        outputs = []
        for cmd in commands:
            ch.send(cmd + "\n")
            out = ""
            end = time.time() + timeout
            last_data = time.time()
            while time.time() < end:
                if ch.recv_ready():
                    out += ch.recv(65536).decode(errors="ignore")
                    last_data = time.time()
                else:
                    if time.time() - last_data > 2.5:
                        break
                    time.sleep(0.2)
            clean = ANSI.sub("", out)
            outputs.append((cmd, clean))
        ch.close()
        return outputs

    def run_single(self, cmd, timeout=20):
        ch = self.client.invoke_shell()
        ch.settimeout(timeout)
        time.sleep(1.5)
        while ch.recv_ready():
            ch.recv(65536)
        ch.send(cmd + "\n")
        out = ""
        end = time.time() + timeout
        last_data = time.time()
        while time.time() < end:
            if ch.recv_ready():
                out += ch.recv(65536).decode(errors="ignore")
                last_data = time.time()
            else:
                if time.time() - last_data > 3:
                    break
                time.sleep(0.2)
        ch.close()
        return ANSI.sub("", out)

    def has_error(self, text):
        for pat in ["ERROR:", "Error:", "Unknown command", "Invalid command",
                     "Commit check failed", "commit check has failed", "Commit failed",
                     "Command failed", "TRANSACTION_COMMIT_CHECK_FAILED",
                     "missing a mandatory leaf", "rpc-error"]:
            if pat.lower() in text.lower():
                return True
        return False

    def record(self, name, ok, detail):
        status = "PASS" if ok else "FAIL"
        self.results.append((name, status, detail))
        print(f"  [{status}] {name}: {detail[:150]}")

    # ────────────────────────────────────────────────────
    # BC-5: repeat-interval < total probe time
    # ────────────────────────────────────────────────────
    def test_bc5_repeat_interval_overlap(self):
        print(f"\n--- BC-5: repeat-interval overlap validation ---")
        prof = "BC5_OVERLAP_TEST"
        cmds = [
            "configure",
            f"services performance-monitoring profiles cfm two-way-delay-measurement {prof}",
            "inform-test-results enabled",
            "test-duration probes probe-count 10 probe-interval 2 repeat-interval 5",
            "thresholds delay-rtt-min 100",
            "commit check",
            f"no services performance-monitoring profiles cfm two-way-delay-measurement {prof}",
            "exit",  # exit profiles context
            "exit",  # exit cfm
            "exit",  # exit performance-monitoring
            "exit",  # exit services
            "exit",  # exit configure
        ]
        outputs = self.run_sequence(cmds, timeout=30)
        
        commit_check_output = ""
        for cmd, out in outputs:
            if cmd == "commit check":
                commit_check_output = out
        
        has_err = self.has_error(commit_check_output)
        if has_err:
            self.record("BC-5: repeat-interval overlap",
                       False,  # From bug perspective: False means "NOT a bug" (device rejects it)
                       "DEVICE REJECTS overlap config (good behavior) - NOT A BUG")
        else:
            self.record("BC-5: repeat-interval overlap",
                       True,  # From bug perspective: True means "BUG CONFIRMED"
                       "BUG CONFIRMED: commit check PASSES for probe-count=10,pi=2,ri=5 (20s cycle, 5s repeat)")

        # Cleanup: also try rollback in case commit check left candidate dirty
        self.run_sequence(["configure", "rollback 0", "exit"], timeout=10)

    # ────────────────────────────────────────────────────
    # BC-7: Profile deletion after session deletion
    # ────────────────────────────────────────────────────
    def test_bc7_profile_deletion(self):
        print(f"\n--- BC-7: Profile deletion after session deletion ---")
        prof = "BC7_DEL_TEST_PROF"
        sess = "BC7_DEL_TEST_SESS"

        # Step 1: Find a free MEP or use a known context
        out = self.run_single("show config services ethernet-oam connectivity-fault-management | no-more")
        
        # Parse to find MD/MA/MEP
        md_match = re.search(r"maintenance-domains\s+(\S+)", out)
        ma_match = re.search(r"maintenance-associations\s+(\S+)", out)
        mep_match = re.search(r"local-mep\s+(\d+)", out)
        remote_match = re.search(r"crosscheck mep-id\s+(\d+)", out)
        
        if not all([md_match, ma_match, mep_match, remote_match]):
            self.record("BC-7: Profile deletion", False, "Cannot find CFM context to test with")
            return
        
        md = md_match.group(1)
        ma = ma_match.group(1)
        mep = mep_match.group(1)
        target = remote_match.group(1)
        
        print(f"  Using context: MD={md}, MA={ma}, MEP={mep}, target={target}")
        
        # Step 1: Create profile
        cmds_create_profile = [
            "configure",
            f"services performance-monitoring profiles cfm two-way-delay-measurement {prof}",
            "inform-test-results enabled",
            "test-duration probes probe-count 5 probe-interval 1 repeat-interval 10",
            "thresholds delay-rtt-min 100",
            "exit",  # exit profile
            "exit",  # exit two-way-delay-measurement
            "exit",  # exit cfm
            "exit",  # exit profiles
            "exit",  # exit performance-monitoring
            "exit",  # exit services
            # Step 2: Create session using that profile
            f"services performance-monitoring cfm two-way-delay-measurement {sess}",
            "admin-state enabled",
            f"profile {prof}",
            f"source maintenance-domain {md} maintenance-association {ma} mep-id {mep}",
            f"target mep-id {target}",
            "exit",  # exit session
            "exit",  # exit two-way-delay-measurement
            "exit",  # exit cfm
            "exit",  # exit performance-monitoring
            "exit",  # exit services
            "commit",
            "exit",  # exit configure
        ]
        out_create = self.run_sequence(cmds_create_profile, timeout=60)
        
        create_err = False
        for cmd, out in out_create:
            if cmd == "commit" and self.has_error(out):
                create_err = True
                self.record("BC-7: Profile deletion (setup)", False, 
                           f"Cannot create test profile+session: commit error in {out[:100]}")
                # Cleanup
                self.run_sequence([
                    "configure",
                    f"no services performance-monitoring cfm two-way-delay-measurement {sess}",
                    f"no services performance-monitoring profiles cfm two-way-delay-measurement {prof}",
                    "commit",
                    "exit",
                ], timeout=30)
                return
        
        print(f"  Created profile '{prof}' and session '{sess}', committed.")
        time.sleep(2)
        
        # Step 3: Delete the session first, commit
        cmds_del_session = [
            "configure",
            f"no services performance-monitoring cfm two-way-delay-measurement {sess}",
            "commit",
            "exit",
        ]
        out_del_sess = self.run_sequence(cmds_del_session, timeout=30)
        sess_del_err = False
        for cmd, out in out_del_sess:
            if cmd == "commit" and self.has_error(out):
                sess_del_err = True
        
        if sess_del_err:
            self.record("BC-7: Profile deletion", False, "Cannot delete session (unexpected)")
            # Try full cleanup
            self.run_sequence([
                "configure",
                f"no services performance-monitoring cfm two-way-delay-measurement {sess}",
                f"no services performance-monitoring profiles cfm two-way-delay-measurement {prof}",
                "commit",
                "exit",
            ], timeout=30)
            return
        
        print(f"  Deleted session '{sess}', committed. Now trying to delete profile...")
        time.sleep(2)
        
        # Step 4: Now try to delete the profile (THIS IS THE BUG)
        cmds_del_profile = [
            "configure",
            f"no services performance-monitoring profiles cfm two-way-delay-measurement {prof}",
            "commit check",
        ]
        out_del_prof = self.run_sequence(cmds_del_profile, timeout=30)
        
        commit_check_output = ""
        for cmd, out in out_del_prof:
            if cmd == "commit check":
                commit_check_output = out
        
        profile_del_err = self.has_error(commit_check_output)
        
        if profile_del_err:
            self.record("BC-7: Profile deletion after session deletion",
                       True,  # BUG CONFIRMED
                       f"BUG CONFIRMED: Profile deletion fails after session removal. Error: {commit_check_output[:200]}")
        else:
            self.record("BC-7: Profile deletion after session deletion",
                       False,  # NOT A BUG (device accepts deletion)
                       "DEVICE ACCEPTS profile deletion after session removal - NOT A BUG (or fixed)")
        
        # Full cleanup
        self.run_sequence([
            "rollback 0",
            "exit",
        ], timeout=10)
        self.run_sequence([
            "configure",
            f"no services performance-monitoring profiles cfm two-way-delay-measurement {prof}",
            "commit",
            "exit",
        ], timeout=30)

    # ────────────────────────────────────────────────────
    # BC-10: Event test missing direction
    # ────────────────────────────────────────────────────
    def test_bc10_event_missing_direction(self):
        print(f"\n--- BC-10: Source line with/without direction ---")
        # First find a MEP that requires direction
        out = self.run_single("show config services ethernet-oam connectivity-fault-management | no-more")
        
        # Parse ALL local MEPs with their directions
        meps_found = []
        current_md = None
        current_ma = None
        for line in out.splitlines():
            md_m = re.search(r"maintenance-domains\s+(\S+)", line)
            if md_m:
                current_md = md_m.group(1)
                current_ma = None
            ma_m = re.search(r"maintenance-associations\s+(\S+)", line)
            if ma_m:
                current_ma = ma_m.group(1)
            mep_m = re.search(r"local-mep\s+(\d+)", line)
            if mep_m and current_md and current_ma:
                meps_found.append((current_md, current_ma, mep_m.group(1)))
            dir_m = re.search(r"direction\s+(up|down)", line)
            if dir_m and meps_found:
                last = meps_found[-1]
                meps_found[-1] = (*last, dir_m.group(1))

        print(f"  Found MEPs: {meps_found}")
        
        # Find a MEP with direction that has a remote crosscheck
        remote_match = re.search(r"crosscheck mep-id\s+(\d+)", out)
        target = remote_match.group(1) if remote_match else "1"
        
        for mep_info in meps_found:
            if len(mep_info) < 4:
                continue
            md, ma, mep, direction = mep_info
            prof = "BC10_DIR_TEST"
            sess = "BC10_DIR_SESS"
            
            # Test WITHOUT direction (the bug in event test at line 3005)
            print(f"\n  Testing MEP {mep} ({direction}) WITHOUT direction keyword...")
            cmds_no_dir = [
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
                "commit check",
            ]
            out_no_dir = self.run_sequence(cmds_no_dir, timeout=30)
            
            no_dir_commit = ""
            for cmd, out_val in out_no_dir:
                if cmd == "commit check":
                    no_dir_commit = out_val
            
            no_dir_err = self.has_error(no_dir_commit)
            
            # Cleanup
            self.run_sequence(["rollback 0", "exit"], timeout=10)
            self.run_sequence([
                "configure",
                f"no services performance-monitoring cfm two-way-delay-measurement {sess}",
                f"no services performance-monitoring profiles cfm two-way-delay-measurement {prof}",
                "commit",
                "exit",
            ], timeout=30)
            
            # Test WITH direction
            print(f"  Testing MEP {mep} ({direction}) WITH direction {direction}...")
            cmds_with_dir = [
                "configure",
                f"services performance-monitoring profiles cfm two-way-delay-measurement {prof}",
                "inform-test-results enabled",
                "test-duration probes probe-count 3 probe-interval 1 repeat-interval 5",
                "thresholds delay-rtt-max 1",
                "exit", "exit", "exit", "exit", "exit",
                f"services performance-monitoring cfm two-way-delay-measurement {sess}",
                "admin-state enabled",
                f"profile {prof}",
                f"source maintenance-domain {md} maintenance-association {ma} mep-id {mep} direction {direction}",
                f"target mep-id {target}",
                "exit", "exit", "exit", "exit",
                "commit check",
            ]
            out_with_dir = self.run_sequence(cmds_with_dir, timeout=30)
            
            with_dir_commit = ""
            for cmd, out_val in out_with_dir:
                if cmd == "commit check":
                    with_dir_commit = out_val
            
            with_dir_err = self.has_error(with_dir_commit)
            
            # Cleanup
            self.run_sequence(["rollback 0", "exit"], timeout=10)
            self.run_sequence([
                "configure",
                f"no services performance-monitoring cfm two-way-delay-measurement {sess}",
                f"no services performance-monitoring profiles cfm two-way-delay-measurement {prof}",
                "commit",
                "exit",
            ], timeout=30)
            
            # Report results
            if no_dir_err and not with_dir_err:
                self.record(f"BC-10: Direction required for MEP {mep} ({direction})",
                           True,
                           f"BUG CONFIRMED: Without direction=FAIL, with direction=PASS. Event test line 3005 would fail.")
            elif not no_dir_err and not with_dir_err:
                self.record(f"BC-10: Direction for MEP {mep} ({direction})",
                           False,
                           f"Both with/without direction accepted - direction optional for this MEP. NOT A BUG on this device.")
            elif no_dir_err and with_dir_err:
                self.record(f"BC-10: Direction for MEP {mep} ({direction})",
                           False,
                           f"Both fail - MEP may already be in use. No-dir err: {no_dir_commit[:100]}")
            else:
                self.record(f"BC-10: Direction for MEP {mep} ({direction})",
                           False,
                           f"Without direction=PASS, with direction=FAIL. Device does not want direction keyword.")
            break  # Test one MEP

    # ────────────────────────────────────────────────────
    # BC-9: On-demand stop validation permissiveness
    # ────────────────────────────────────────────────────
    def test_bc9_stop_validation(self):
        print(f"\n--- BC-9: On-demand stop with no active tests ---")
        out = self.run_single("request ethernet-oam cfm on-demand stop all")
        
        lower = out.lower()
        has_stopped = "stopped tests" in lower or "total stopped" in lower
        has_no_tests = "no ongoing" in lower or "no on-demand" in lower or "no tests" in lower
        has_error = "unknown command" in lower or "invalid command" in lower
        
        if has_stopped:
            self.record("BC-9: Stop with no active tests",
                       False,
                       f"Device reports 'stopped tests' even with no active on-demand tests running. Output: {out[:200]}")
        elif has_no_tests:
            self.record("BC-9: Stop with no active tests",
                       True,
                       f"Device correctly reports 'no ongoing tests'. Output: {out[:200]}")
        elif has_error:
            self.record("BC-9: Stop with no active tests",
                       False,
                       f"Command not accepted: {out[:200]}")
        else:
            self.record("BC-9: Stop with no active tests",
                       False,
                       f"Ambiguous output (would pass permissive validator): {out[:200]}")

    # ────────────────────────────────────────────────────
    # NEW: Check orphaned profiles from prior test runs (BC-3)
    # ────────────────────────────────────────────────────
    def test_bc3_orphaned_profiles(self):
        print(f"\n--- BC-3: Check for orphaned test profiles ---")
        out = self.run_single("show config services performance-monitoring profiles | no-more")
        
        test_profiles = []
        for line in out.splitlines():
            line = line.strip()
            if re.match(r"two-way-(delay|synthetic).*\s+\S+", line):
                name_match = re.search(r"two-way-(?:delay-measurement|synthetic-loss-measurement)\s+(\S+)", line)
                if name_match:
                    test_profiles.append(name_match.group(1))
        
        orphan_markers = ["_SW235372", "_BAD", "_NONNUM", "_BADPCP", "_BADTIMER", 
                          "BC5_", "BC7_", "BC10_", "VERIFY_", "RESTCONF_"]
        orphaned = [p for p in test_profiles if any(m in p for m in orphan_markers)]
        
        if orphaned:
            self.record("BC-3: Orphaned test profiles",
                       True,
                       f"BUG CONFIRMED: {len(orphaned)} orphaned profiles found: {orphaned[:5]}")
        else:
            self.record("BC-3: Orphaned test profiles",
                       False,
                       f"No orphaned test profiles found. Total profiles: {test_profiles}")

    def print_summary(self):
        print(f"\n{'='*70}")
        print(f"RESULTS SUMMARY: {self.host}")
        print(f"{'='*70}")
        bugs_confirmed = 0
        not_bugs = 0
        for name, status, detail in self.results:
            indicator = "BUG" if "BUG CONFIRMED" in detail else ("OK" if status == "PASS" else "CHECK")
            print(f"  [{indicator}] {name}")
            if "BUG CONFIRMED" in detail:
                bugs_confirmed += 1
            elif "NOT A BUG" in detail:
                not_bugs += 1
        print(f"\n  Bugs confirmed: {bugs_confirmed}")
        print(f"  Not bugs: {not_bugs}")
        print(f"  Other: {len(self.results) - bugs_confirmed - not_bugs}")


def main():
    devices = [
        ("WKY1C7VD00008P2", "dnroot", "dnroot"),
        ("xec1e3vr00008", "dnroot", "dnroot"),
    ]
    
    for host, user, pw in devices:
        print(f"\n{'#'*70}")
        print(f"# TESTING DEVICE: {host}")
        print(f"{'#'*70}")
        
        tester = DeviceTester(host, user, pw)
        try:
            tester.connect()
            tester.test_bc5_repeat_interval_overlap()
            tester.test_bc7_profile_deletion()
            tester.test_bc10_event_missing_direction()
            tester.test_bc9_stop_validation()
            tester.test_bc3_orphaned_profiles()
            tester.print_summary()
        except Exception as e:
            print(f"\n[FATAL ERROR] {host}: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
        finally:
            tester.disconnect()


if __name__ == "__main__":
    main()
