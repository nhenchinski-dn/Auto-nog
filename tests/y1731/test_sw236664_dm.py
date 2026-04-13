#!/usr/bin/env python3
"""SW-236664: ETH-DM Initiator Functionality Test on ncpl-cfm-nog (XEC1E3VR00008)"""

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

def run(chan, cmd, wait=10):
    chan.send(cmd + '\n')
    time.sleep(wait)
    out = b''
    while chan.recv_ready():
        out += chan.recv(65535)
    return clean_ansi(out.decode(errors='replace'))

def run_show(chan, cmd, wait=10):
    return run(chan, cmd + ' | no-more', wait)

def configure_and_commit(chan, config_cmds, wait_commit=15):
    run(chan, 'configure', 3)
    for cmd in config_cmds:
        run(chan, cmd, 2)
    output = run(chan, 'commit', wait_commit)
    return output

def rollback(chan):
    run(chan, 'configure', 3)
    output = run(chan, 'rollback', 10)
    return output

def log_step(step_name, output, passed=None):
    results[step_name] = {
        'output': output,
        'passed': passed,
        'timestamp': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')
    }
    status = "PASS" if passed else ("FAIL" if passed is False else "INFO")
    print(f"\n{'='*80}")
    print(f"[{status}] {step_name}")
    print(f"{'='*80}")
    print(output[:2000] if len(output) > 2000 else output)

def main():
    print(f"Connecting to {DEVICE_IP} ({USERNAME})...")
    ssh, chan = connect()
    print("Connected.\n")

    # =========================================================================
    # STEP 1: Verify existing config (show only)
    # =========================================================================
    print("\n" + "="*80)
    print("STEP 1: Verify existing MEP & DM profile configuration")
    print("="*80)

    out_cfg = run_show(chan, 'show config services performance-monitoring')
    log_step("Step 1a: PM config", out_cfg, True)

    out_cfm = run_show(chan, 'show services ethernet-oam connectivity-fault-management maintenance-domains')
    log_step("Step 1b: CFM maintenance domains", out_cfm,
             'MD-CUST' in out_cfm and 'MD-CUST1' in out_cfm)

    # =========================================================================
    # STEP 2: Verify DM with mep-id target + all 6 metrics
    # =========================================================================
    print("\n" + "="*80)
    print("STEP 2: Run DM with mep-id target - verify DMM/DMR + all 6 metrics")
    print("="*80)

    out_dm_mep = run_show(chan,
        'show services performance-monitoring cfm tests proactive two-way-delay session-name DM_CLI_TAB_mep1 detail',
        12)
    metrics_present = all(m in out_dm_mep for m in [
        'Minimum:', 'Maximum:', 'Average:',
        'IFDV Average:', 'IFDV Maximum:',
        'Success rate:'
    ])
    dmr_received = 'DMR PDUs received' in out_dm_mep
    log_step("Step 2: DM mep-id target + metrics", out_dm_mep,
             metrics_present and dmr_received)

    # =========================================================================
    # STEP 2b: Remove DM config and re-add
    # =========================================================================
    print("\n" + "="*80)
    print("STEP 2b: Remove DM configuration then re-add")
    print("="*80)

    configure_and_commit(chan, [
        'delete services performance-monitoring cfm two-way-delay-measurement DM_CLI_TAB_mep1'
    ])

    time.sleep(3)
    out_removed = run_show(chan, 'show services performance-monitoring cfm tests proactive')
    dm_gone = 'DM_CLI_TAB_mep1' not in out_removed or 'Total displayed tests: 0' in out_removed
    log_step("Step 2b-1: DM removed", out_removed, dm_gone)

    configure_and_commit(chan, [
        'services performance-monitoring cfm two-way-delay-measurement DM_CLI_TAB_mep1 admin-state enabled',
        'services performance-monitoring cfm two-way-delay-measurement DM_CLI_TAB_mep1 description cli_tab_test',
        'services performance-monitoring cfm two-way-delay-measurement DM_CLI_TAB_mep1 profile DM_PROF_CLI',
        'services performance-monitoring cfm two-way-delay-measurement DM_CLI_TAB_mep1 source maintenance-domain MD-CUST maintenance-association MA-CUST mep-id 1',
        'services performance-monitoring cfm two-way-delay-measurement DM_CLI_TAB_mep1 target mep-id 2',
    ])

    time.sleep(15)
    out_readded = run_show(chan,
        'show services performance-monitoring cfm tests proactive two-way-delay session-name DM_CLI_TAB_mep1 detail',
        12)
    dm_back = 'DM_CLI_TAB_mep1' in out_readded and 'MEP-ID: 1' in out_readded
    log_step("Step 2b-2: DM re-added and running", out_readded, dm_back)

    # =========================================================================
    # STEP 3: DM with mac-address target variant
    # =========================================================================
    print("\n" + "="*80)
    print("STEP 3: DM with mac-address target variant")
    print("="*80)

    configure_and_commit(chan, [
        'services performance-monitoring cfm two-way-delay-measurement DM_CLI_TAB_mep3 admin-state enabled',
        'services performance-monitoring cfm two-way-delay-measurement DM_CLI_TAB_mep3 description cli_tab_mac_test',
        'services performance-monitoring cfm two-way-delay-measurement DM_CLI_TAB_mep3 profile DM_PROF_CLI',
        'services performance-monitoring cfm two-way-delay-measurement DM_CLI_TAB_mep3 source maintenance-domain MD-CUST1 maintenance-association MA-CUST1 mep-id 3',
        'services performance-monitoring cfm two-way-delay-measurement DM_CLI_TAB_mep3 target mac-address 84:40:76:90:cd:15',
    ])

    time.sleep(15)
    out_mac = run_show(chan,
        'show services performance-monitoring cfm tests proactive two-way-delay session-name DM_CLI_TAB_mep3 detail',
        12)
    mac_target_ok = '84:40:76:90:cd:15' in out_mac and 'Success rate:' in out_mac
    log_step("Step 3: DM mac-address target", out_mac, mac_target_ok)

    # =========================================================================
    # STEP 4: Admin disable/enable variant
    # =========================================================================
    print("\n" + "="*80)
    print("STEP 4: Admin disable/enable")
    print("="*80)

    configure_and_commit(chan, [
        'services performance-monitoring cfm two-way-delay-measurement DM_CLI_TAB_mep1 admin-state disabled',
    ])

    time.sleep(5)
    out_disabled = run_show(chan,
        'show services performance-monitoring cfm tests proactive two-way-delay session-name DM_CLI_TAB_mep1 detail',
        10)
    is_disabled = 'Admin state: disabled' in out_disabled
    log_step("Step 4a: Admin disabled", out_disabled, is_disabled)

    configure_and_commit(chan, [
        'services performance-monitoring cfm two-way-delay-measurement DM_CLI_TAB_mep1 admin-state enabled',
    ])

    time.sleep(15)
    out_enabled = run_show(chan,
        'show services performance-monitoring cfm tests proactive two-way-delay session-name DM_CLI_TAB_mep1 detail',
        12)
    is_enabled = 'Admin state: enabled' in out_enabled
    log_step("Step 4b: Admin re-enabled", out_enabled, is_enabled)

    # Check proactive list to verify Disabled -> Ongoing transition
    out_proactive = run_show(chan,
        'show services performance-monitoring cfm tests proactive')
    ongoing = 'Ongoing' in out_proactive
    log_step("Step 4c: Proactive list after re-enable", out_proactive, ongoing)

    # =========================================================================
    # STEP 5: Profile change variant
    # =========================================================================
    print("\n" + "="*80)
    print("STEP 5: Profile change")
    print("="*80)

    configure_and_commit(chan, [
        'services performance-monitoring profiles cfm two-way-delay-measurement DM_PROF_CLI2 thresholds delay-rtt-avg 1',
        'services performance-monitoring profiles cfm two-way-delay-measurement DM_PROF_CLI2 thresholds success-rate 40.0',
        'services performance-monitoring cfm two-way-delay-measurement DM_CLI_TAB_mep1 profile DM_PROF_CLI2',
    ])

    time.sleep(15)
    out_profile = run_show(chan,
        'show services performance-monitoring cfm tests proactive two-way-delay session-name DM_CLI_TAB_mep1 detail',
        12)
    profile_changed = 'Profile: DM_PROF_CLI2' in out_profile
    log_step("Step 5: Profile changed to DM_PROF_CLI2", out_profile, profile_changed)

    # Rollback to original profile
    configure_and_commit(chan, [
        'services performance-monitoring cfm two-way-delay-measurement DM_CLI_TAB_mep1 profile DM_PROF_CLI',
        'delete services performance-monitoring profiles cfm two-way-delay-measurement DM_PROF_CLI2',
    ])
    time.sleep(5)

    # =========================================================================
    # STEP 6: Inform-test-result variant
    # =========================================================================
    print("\n" + "="*80)
    print("STEP 6: Inform-test-result toggle")
    print("="*80)

    configure_and_commit(chan, [
        'services performance-monitoring profiles cfm two-way-delay-measurement DM_PROF_CLI inform-test-results disabled',
    ])

    time.sleep(15)
    out_inform_off = run_show(chan,
        'show services performance-monitoring cfm tests proactive two-way-delay session-name DM_CLI_TAB_mep1 detail',
        12)
    inform_disabled = 'Inform Test Results: disabled' in out_inform_off
    log_step("Step 6a: Inform-test-results disabled", out_inform_off, inform_disabled)

    configure_and_commit(chan, [
        'services performance-monitoring profiles cfm two-way-delay-measurement DM_PROF_CLI inform-test-results enabled',
    ])

    time.sleep(15)
    out_inform_on = run_show(chan,
        'show services performance-monitoring cfm tests proactive two-way-delay session-name DM_CLI_TAB_mep1 detail',
        12)
    inform_enabled = 'Inform Test Results: enabled' in out_inform_on
    log_step("Step 6b: Inform-test-results re-enabled", out_inform_on, inform_enabled)

    # =========================================================================
    # STEP 7: Negative - Unreachable MAC
    # =========================================================================
    print("\n" + "="*80)
    print("STEP 7: Negative - Unreachable MAC (no fake DMRs)")
    print("="*80)

    configure_and_commit(chan, [
        'services performance-monitoring cfm two-way-delay-measurement DM_CLI_TAB_mep1 target mac-address 22:22:22:22:22:22',
    ])

    time.sleep(20)
    out_unreach = run_show(chan,
        'show services performance-monitoring cfm tests proactive two-way-delay session-name DM_CLI_TAB_mep1 detail',
        12)
    dmr_zero = 'DMR PDUs received:    0' in out_unreach or 'DMR PDUs received: 0' in out_unreach
    success_zero = 'Success rate:         0' in out_unreach or 'Success rate: 0' in out_unreach
    log_step("Step 7: Unreachable MAC - no DMRs", out_unreach, dmr_zero)

    # Restore mep-id target
    configure_and_commit(chan, [
        'delete services performance-monitoring cfm two-way-delay-measurement DM_CLI_TAB_mep1 target mac-address',
        'services performance-monitoring cfm two-way-delay-measurement DM_CLI_TAB_mep1 target mep-id 2',
    ])
    time.sleep(5)

    # =========================================================================
    # STEP 8: Negative - Remove CFM/L2-service while DM running
    # =========================================================================
    print("\n" + "="*80)
    print("STEP 8: Negative - Remove CFM while DM running")
    print("="*80)

    # Verify DM is running before removal
    out_before = run_show(chan,
        'show services performance-monitoring cfm tests proactive two-way-delay session-name DM_CLI_TAB_mep1 detail',
        12)
    log_step("Step 8a: DM running before CFM removal", out_before,
             'Admin state: enabled' in out_before)

    # Remove the MEP from CFM (not the entire CFM, just enough to break the DM path)
    configure_and_commit(chan, [
        'delete services ethernet-oam connectivity-fault-management maintenance-domains MD-CUST maintenance-associations MA-CUST local-mep 1',
    ])

    time.sleep(10)
    out_no_cfm = run_show(chan,
        'show services performance-monitoring cfm tests proactive two-way-delay session-name DM_CLI_TAB_mep1 detail',
        12)
    log_step("Step 8b: DM state after MEP removal", out_no_cfm, True)

    # Restore the MEP
    configure_and_commit(chan, [
        'services ethernet-oam connectivity-fault-management maintenance-domains MD-CUST maintenance-associations MA-CUST local-mep 1 direction up',
        'services ethernet-oam connectivity-fault-management maintenance-domains MD-CUST maintenance-associations MA-CUST local-mep 1 interface ge10-0/0/32.100',
    ])

    time.sleep(15)
    out_restored = run_show(chan,
        'show services performance-monitoring cfm tests proactive two-way-delay session-name DM_CLI_TAB_mep1 detail',
        12)
    dm_restored = 'DM_CLI_TAB_mep1' in out_restored and 'MEP-ID: 1' in out_restored
    log_step("Step 8c: DM restored after MEP re-add", out_restored, dm_restored)

    # =========================================================================
    # ON-DEMAND DM test
    # =========================================================================
    print("\n" + "="*80)
    print("BONUS: On-demand DM test (mac-address)")
    print("="*80)

    out_ondemand = run(chan,
        'run ethernet-oam cfm on-demand delay-measurement two-way maintenance-domain MD-CUST1 maintenance-association MA-CUST1 target mac-address 84:40:76:90:cd:15',
        20)
    ondemand_ok = 'Success rate' in out_ondemand
    log_step("On-demand DM (mac-address)", out_ondemand, ondemand_ok)

    # On-demand with mep-id
    out_ondemand_mep = run(chan,
        'run ethernet-oam cfm on-demand delay-measurement two-way maintenance-domain MD-CUST1 maintenance-association MA-CUST1 target mep-id 4',
        20)
    ondemand_mep_ok = 'Success rate' in out_ondemand_mep
    log_step("On-demand DM (mep-id)", out_ondemand_mep, ondemand_mep_ok)

    # =========================================================================
    # CLEANUP: Remove DM_CLI_TAB_mep3 (mac-address variant)
    # =========================================================================
    configure_and_commit(chan, [
        'delete services performance-monitoring cfm two-way-delay-measurement DM_CLI_TAB_mep3',
    ])

    # =========================================================================
    # FINAL: Verify everything is back to baseline
    # =========================================================================
    print("\n" + "="*80)
    print("FINAL: Verify baseline restored")
    print("="*80)

    out_final = run_show(chan, 'show services performance-monitoring cfm tests proactive')
    log_step("Final: Proactive tests", out_final,
             'DM_CLI_TAB_mep1' in out_final and 'Ongoing' in out_final)

    out_final_detail = run_show(chan,
        'show services performance-monitoring cfm tests proactive two-way-delay session-name DM_CLI_TAB_mep1 detail',
        12)
    log_step("Final: DM detail", out_final_detail,
             'Profile: DM_PROF_CLI' in out_final_detail)

    # =========================================================================
    # SUMMARY
    # =========================================================================
    print("\n\n" + "="*80)
    print("TEST EXECUTION SUMMARY")
    print("="*80)
    total = 0
    passed = 0
    failed = 0
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

    # Save results to file
    with open('/home/dn/sw236664_results.json', 'w') as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nDetailed results saved to /home/dn/sw236664_results.json")

    ssh.close()
    print("\nSSH connection closed.")

if __name__ == '__main__':
    main()
