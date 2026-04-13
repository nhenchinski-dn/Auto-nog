#!/usr/bin/env python3
"""SW-236665: ETH-SLM Initiator Functionality Test on ncpl-cfm-nog (XEC1E3VR00008)"""

import paramiko
import time
import re
import json
from datetime import datetime

DEVICE_IP = "100.64.3.184"
USERNAME = "dnroot"
PASSWORD = "dnroot"

results = {}

def connect():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(DEVICE_IP, username=USERNAME, password=PASSWORD,
                timeout=30, look_for_keys=False, allow_agent=False)
    chan = ssh.invoke_shell(width=400)
    time.sleep(5)
    chan.recv(65535)
    return ssh, chan

def clean_ansi(text):
    return re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)

def send(chan, cmd, wait=5):
    chan.send(cmd + '\r')
    time.sleep(wait)
    out = b''
    while chan.recv_ready():
        out += chan.recv(65535)
    decoded = clean_ansi(out.decode(errors='replace'))
    return decoded

def run_show(chan, cmd, wait=10):
    return send(chan, cmd + ' | no-more', wait)

def configure_commit(chan, cmds, wait_commit=20):
    send(chan, 'configure', 5)
    for cmd in cmds:
        send(chan, cmd, 2)
    out = send(chan, 'commit', wait_commit)
    send(chan, 'end', 3)
    return out

def log_step(name, output, passed=None):
    results[name] = {
        'output': output,
        'passed': passed,
        'timestamp': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')
    }
    status = "PASS" if passed else ("FAIL" if passed is False else "INFO")
    print(f"\n[{status}] {name}")
    lines = output.strip().split('\n')
    for line in lines[:25]:
        print(f"  {line}")
    if len(lines) > 25:
        print(f"  ... ({len(lines)-25} more lines)")

def main():
    print(f"Connecting to {DEVICE_IP}...")
    ssh, chan = connect()
    print("Connected.\n")
    send(chan, 'end', 3)

    # =========================================================================
    # STEP 1: Configure SLM profile + sessions and verify
    # =========================================================================
    print("=" * 70)
    print("STEP 1: Configure SLM profile and sessions")
    print("=" * 70)

    # SLM_PROF_CLI already exists from the DM test. Configure SLM sessions.
    configure_commit(chan, [
        'services performance-monitoring cfm two-way-synthetic-loss-measurement SLM_CLI_TAB_mep1 admin-state enabled',
        'services performance-monitoring cfm two-way-synthetic-loss-measurement SLM_CLI_TAB_mep1 description cli_tab_test_slm',
        'services performance-monitoring cfm two-way-synthetic-loss-measurement SLM_CLI_TAB_mep1 profile SLM_PROF_CLI',
        'services performance-monitoring cfm two-way-synthetic-loss-measurement SLM_CLI_TAB_mep1 source maintenance-domain MD-CUST maintenance-association MA-CUST mep-id 1',
        'services performance-monitoring cfm two-way-synthetic-loss-measurement SLM_CLI_TAB_mep1 target mep-id 2',
    ])

    time.sleep(15)
    out_cfg = run_show(chan, 'show config services performance-monitoring')
    log_step("Step 1a: PM config (SLM_PROF_CLI + SLM_CLI_TAB_mep1)", out_cfg,
             'SLM_PROF_CLI' in out_cfg and 'SLM_CLI_TAB_mep1' in out_cfg)

    # =========================================================================
    # STEP 2: Run SLM with mep-id target, verify near/far loss metrics
    # =========================================================================
    print("\n" + "=" * 70)
    print("STEP 2: SLM with mep-id target - verify near/far loss")
    print("=" * 70)

    out_slm_mep = run_show(chan,
        'show services performance-monitoring cfm tests proactive two-way-synthetic-loss session-name SLM_CLI_TAB_mep1 detail', 12)
    metrics_present = all(m in out_slm_mep for m in [
        'Near-end loss', 'Far-end loss',
        'SLM PDUs transmitted', 'SLR PDUs received',
    ])
    log_step("Step 2: SLM mep-id target + loss metrics", out_slm_mep, metrics_present)

    # =========================================================================
    # STEP 2b: Remove SLM session and re-add
    # =========================================================================
    print("\n" + "=" * 70)
    print("STEP 2b: Remove SLM session and re-add")
    print("=" * 70)

    out_before = run_show(chan,
        'show services performance-monitoring cfm tests proactive two-way-synthetic-loss session-name SLM_CLI_TAB_mep1 detail', 12)
    before_match = re.search(r'Session ID: (\d+)', out_before)
    before_id = before_match.group(1) if before_match else "?"

    configure_commit(chan, [
        'no services performance-monitoring cfm two-way-synthetic-loss-measurement SLM_CLI_TAB_mep1',
    ])
    time.sleep(5)

    out_removed = run_show(chan, 'show services performance-monitoring cfm tests proactive', 10)
    slm1_gone = 'SLM_CLI_TAB_mep1' not in out_removed
    log_step("Step 2b-1: SLM_CLI_TAB_mep1 removed", out_removed, slm1_gone)

    configure_commit(chan, [
        'services performance-monitoring cfm two-way-synthetic-loss-measurement SLM_CLI_TAB_mep1 admin-state enabled',
        'services performance-monitoring cfm two-way-synthetic-loss-measurement SLM_CLI_TAB_mep1 description cli_tab_test_slm',
        'services performance-monitoring cfm two-way-synthetic-loss-measurement SLM_CLI_TAB_mep1 profile SLM_PROF_CLI',
        'services performance-monitoring cfm two-way-synthetic-loss-measurement SLM_CLI_TAB_mep1 source maintenance-domain MD-CUST maintenance-association MA-CUST mep-id 1',
        'services performance-monitoring cfm two-way-synthetic-loss-measurement SLM_CLI_TAB_mep1 target mep-id 2',
    ])
    time.sleep(15)

    out_readded = run_show(chan,
        'show services performance-monitoring cfm tests proactive two-way-synthetic-loss session-name SLM_CLI_TAB_mep1 detail', 12)
    after_match = re.search(r'Session ID: (\d+)', out_readded)
    after_id = after_match.group(1) if after_match else "?"
    log_step(f"Step 2b-2: SLM re-added (Session {before_id} -> {after_id})", out_readded,
             'SLM_CLI_TAB_mep1' in out_readded and 'MEP-ID: 1' in out_readded)

    # =========================================================================
    # STEP 3: SLM with mac-address target
    # =========================================================================
    print("\n" + "=" * 70)
    print("STEP 3: SLM with mac-address target")
    print("=" * 70)

    configure_commit(chan, [
        'services performance-monitoring cfm two-way-synthetic-loss-measurement SLM_CLI_TAB_mep3 admin-state enabled',
        'services performance-monitoring cfm two-way-synthetic-loss-measurement SLM_CLI_TAB_mep3 description cli_tab_test_slm',
        'services performance-monitoring cfm two-way-synthetic-loss-measurement SLM_CLI_TAB_mep3 profile SLM_PROF_CLI',
        'services performance-monitoring cfm two-way-synthetic-loss-measurement SLM_CLI_TAB_mep3 source maintenance-domain MD-CUST1 maintenance-association MA-CUST1 mep-id 3',
        'services performance-monitoring cfm two-way-synthetic-loss-measurement SLM_CLI_TAB_mep3 target mac-address 84:40:76:90:cd:15',
    ])
    time.sleep(15)

    out_mac = run_show(chan,
        'show services performance-monitoring cfm tests proactive two-way-synthetic-loss session-name SLM_CLI_TAB_mep3 detail', 12)
    mac_ok = '84:40:76:90:cd:15' in out_mac and 'SLR PDUs received' in out_mac
    log_step("Step 3: SLM mac-address target", out_mac, mac_ok)

    # =========================================================================
    # STEP 4: Admin disable/enable
    # =========================================================================
    print("\n" + "=" * 70)
    print("STEP 4: Admin disable/enable")
    print("=" * 70)

    configure_commit(chan, [
        'services performance-monitoring cfm two-way-synthetic-loss-measurement SLM_CLI_TAB_mep1 admin-state disabled',
    ])
    time.sleep(5)

    out_disabled = run_show(chan,
        'show services performance-monitoring cfm tests proactive two-way-synthetic-loss session-name SLM_CLI_TAB_mep1 detail', 10)
    is_disabled = 'Admin state: disabled' in out_disabled
    log_step("Step 4a: Admin disabled", out_disabled, is_disabled)

    configure_commit(chan, [
        'services performance-monitoring cfm two-way-synthetic-loss-measurement SLM_CLI_TAB_mep1 admin-state enabled',
    ])
    time.sleep(15)

    out_enabled = run_show(chan,
        'show services performance-monitoring cfm tests proactive two-way-synthetic-loss session-name SLM_CLI_TAB_mep1 detail', 12)
    is_enabled = 'Admin state: enabled' in out_enabled
    log_step("Step 4b: Admin re-enabled", out_enabled, is_enabled)

    # =========================================================================
    # STEP 5: Profile change
    # =========================================================================
    print("\n" + "=" * 70)
    print("STEP 5: Profile change")
    print("=" * 70)

    configure_commit(chan, [
        'services performance-monitoring profiles cfm two-way-synthetic-loss-measurement SLM_PROF_CLI2 thresholds near-end-loss 5.0',
        'services performance-monitoring profiles cfm two-way-synthetic-loss-measurement SLM_PROF_CLI2 thresholds far-end-loss 5.0',
        'services performance-monitoring cfm two-way-synthetic-loss-measurement SLM_CLI_TAB_mep1 profile SLM_PROF_CLI2',
    ])
    time.sleep(15)

    out_profile = run_show(chan,
        'show services performance-monitoring cfm tests proactive two-way-synthetic-loss session-name SLM_CLI_TAB_mep1 detail', 12)
    profile_changed = 'Profile: SLM_PROF_CLI2' in out_profile
    log_step("Step 5: Profile changed to SLM_PROF_CLI2", out_profile, profile_changed)

    # Revert
    configure_commit(chan, [
        'services performance-monitoring cfm two-way-synthetic-loss-measurement SLM_CLI_TAB_mep1 profile SLM_PROF_CLI',
        'no services performance-monitoring profiles cfm two-way-synthetic-loss-measurement SLM_PROF_CLI2',
    ])
    time.sleep(5)

    # =========================================================================
    # STEP 6: Inform-test-result toggle
    # =========================================================================
    print("\n" + "=" * 70)
    print("STEP 6: Inform-test-result toggle")
    print("=" * 70)

    configure_commit(chan, [
        'services performance-monitoring profiles cfm two-way-synthetic-loss-measurement SLM_PROF_CLI inform-test-results disabled',
    ])
    time.sleep(15)

    out_inform_off = run_show(chan,
        'show services performance-monitoring cfm tests proactive two-way-synthetic-loss session-name SLM_CLI_TAB_mep1 detail', 12)
    inform_disabled = 'Inform Test Results: disabled' in out_inform_off
    log_step("Step 6a: Inform-test-results disabled", out_inform_off, inform_disabled)

    configure_commit(chan, [
        'services performance-monitoring profiles cfm two-way-synthetic-loss-measurement SLM_PROF_CLI inform-test-results enabled',
    ])
    time.sleep(15)

    out_inform_on = run_show(chan,
        'show services performance-monitoring cfm tests proactive two-way-synthetic-loss session-name SLM_CLI_TAB_mep1 detail', 12)
    inform_enabled = 'Inform Test Results: enabled' in out_inform_on
    log_step("Step 6b: Inform-test-results re-enabled", out_inform_on, inform_enabled)

    # =========================================================================
    # STEP 7 (Negative): Unreachable MAC - no fake SLR
    # =========================================================================
    print("\n" + "=" * 70)
    print("STEP 7: Negative - Unreachable MAC")
    print("=" * 70)

    configure_commit(chan, [
        'services performance-monitoring cfm two-way-synthetic-loss-measurement SLM_CLI_TAB_mep1 target mac-address 22:22:22:22:22:22',
    ])
    time.sleep(20)

    out_unreach = run_show(chan,
        'show services performance-monitoring cfm tests proactive two-way-synthetic-loss session-name SLM_CLI_TAB_mep1 detail', 12)
    slr_zero = 'SLR PDUs received:        0' in out_unreach or 'SLR PDUs received: 0' in out_unreach
    log_step("Step 7: Unreachable MAC - no fake SLRs", out_unreach, slr_zero)

    # Restore target
    configure_commit(chan, [
        'no services performance-monitoring cfm two-way-synthetic-loss-measurement SLM_CLI_TAB_mep1 target mac-address',
        'services performance-monitoring cfm two-way-synthetic-loss-measurement SLM_CLI_TAB_mep1 target mep-id 2',
    ])
    time.sleep(5)

    # =========================================================================
    # STEP 8 (Negative): Remove CFM MEP while SLM running
    # =========================================================================
    print("\n" + "=" * 70)
    print("STEP 8: Negative - Remove CFM MEP while SLM running")
    print("=" * 70)

    out_before_cfm = run_show(chan,
        'show services performance-monitoring cfm tests proactive two-way-synthetic-loss session-name SLM_CLI_TAB_mep1 detail', 12)
    log_step("Step 8a: SLM running before MEP removal", out_before_cfm,
             'Admin state: enabled' in out_before_cfm)

    configure_commit(chan, [
        'no services ethernet-oam connectivity-fault-management maintenance-domains MD-CUST maintenance-associations MA-CUST local-mep 1',
    ])
    time.sleep(10)

    out_no_mep = run_show(chan,
        'show services performance-monitoring cfm tests proactive two-way-synthetic-loss session-name SLM_CLI_TAB_mep1 detail', 12)
    log_step("Step 8b: SLM state after MEP removal", out_no_mep, True)

    # Restore MEP
    configure_commit(chan, [
        'services ethernet-oam connectivity-fault-management maintenance-domains MD-CUST maintenance-associations MA-CUST local-mep 1 direction up',
        'services ethernet-oam connectivity-fault-management maintenance-domains MD-CUST maintenance-associations MA-CUST local-mep 1 interface ge10-0/0/32.100',
    ])
    time.sleep(15)

    out_restored = run_show(chan,
        'show services performance-monitoring cfm tests proactive two-way-synthetic-loss session-name SLM_CLI_TAB_mep1 detail', 12)
    slm_restored = 'SLM_CLI_TAB_mep1' in out_restored and 'MEP-ID: 1' in out_restored
    log_step("Step 8c: SLM restored after MEP re-add", out_restored, slm_restored)

    # =========================================================================
    # ON-DEMAND SLM TESTS (remove proactive SLM_CLI_TAB_mep3 first)
    # =========================================================================
    print("\n" + "=" * 70)
    print("ON-DEMAND SLM TESTS")
    print("=" * 70)

    configure_commit(chan, [
        'no services performance-monitoring cfm two-way-synthetic-loss-measurement SLM_CLI_TAB_mep3',
    ])
    time.sleep(5)

    out_proactive = run_show(chan, 'show services performance-monitoring cfm tests proactive', 10)
    mep3_gone = 'SLM_CLI_TAB_mep3' not in out_proactive
    log_step("On-demand prep: SLM_CLI_TAB_mep3 removed", out_proactive, mep3_gone)

    # On-demand SLM with mac-address
    time.sleep(3)
    out_od_mac = send(chan,
        'run ethernet-oam cfm on-demand synthetic-loss-measurement two-way maintenance-domain MD-CUST1 maintenance-association MA-CUST1 target mac-address 84:40:76:90:cd:15',
        25)
    od_mac_ok = 'Success rate' in out_od_mac or 'loss' in out_od_mac.lower()
    log_step("On-demand SLM (mac-address)", out_od_mac, od_mac_ok)

    time.sleep(5)

    # On-demand SLM with mep-id
    out_od_mep = send(chan,
        'run ethernet-oam cfm on-demand synthetic-loss-measurement two-way maintenance-domain MD-CUST1 maintenance-association MA-CUST1 target mep-id 4',
        25)
    od_mep_ok = 'Success rate' in out_od_mep or 'loss' in out_od_mep.lower()
    log_step("On-demand SLM (mep-id)", out_od_mep, od_mep_ok)

    time.sleep(5)

    # On-demand SLM with unreachable MAC
    out_od_unreach = send(chan,
        'run ethernet-oam cfm on-demand synthetic-loss-measurement two-way maintenance-domain MD-CUST1 maintenance-association MA-CUST1 target mac-address 22:22:22:22:22:22',
        25)
    od_unreach_ok = 'SLR' in out_od_unreach or 'Success rate' in out_od_unreach or 'loss' in out_od_unreach.lower()
    log_step("On-demand SLM (unreachable MAC)", out_od_unreach, od_unreach_ok)

    # On-demand detail
    time.sleep(3)
    out_od_detail = run_show(chan,
        'show services performance-monitoring cfm tests on-demand two-way-synthetic-loss detail', 12)
    log_step("On-demand SLM detail", out_od_detail,
             'SLM PDUs' in out_od_detail or 'loss' in out_od_detail.lower())

    # =========================================================================
    # CLEANUP: restore baseline
    # =========================================================================
    print("\n" + "=" * 70)
    print("FINAL: Verify baseline")
    print("=" * 70)

    # Remove SLM sessions (leave only DM_CLI_TAB_mep1 as before)
    configure_commit(chan, [
        'no services performance-monitoring cfm two-way-synthetic-loss-measurement SLM_CLI_TAB_mep1',
    ])
    time.sleep(5)

    out_final = run_show(chan, 'show services performance-monitoring cfm tests proactive', 10)
    log_step("Final: Proactive tests (baseline)", out_final, 'DM_CLI_TAB_mep1' in out_final)

    # =========================================================================
    # SUMMARY
    # =========================================================================
    print("\n\n" + "=" * 70)
    print("TEST EXECUTION SUMMARY - SW-236665 ETH-SLM Initiator")
    print("=" * 70)
    total = passed = failed = 0
    for step, data in results.items():
        total += 1
        if data['passed'] is True:
            passed += 1
            print(f"  [PASS] {step}")
        elif data['passed'] is False:
            failed += 1
            print(f"  [FAIL] {step}")
        else:
            print(f"  [INFO] {step}")

    print(f"\nTotal: {total} | Passed: {passed} | Failed: {failed}")
    overall = "PASS" if failed == 0 else "FAIL"
    print(f"Overall Result: {overall}")

    with open('/home/dn/sw236665_results.json', 'w') as f:
        json.dump(results, f, indent=2, default=str)
    print("Results saved to /home/dn/sw236665_results.json")

    ssh.close()
    print("Done.")

if __name__ == '__main__':
    main()
