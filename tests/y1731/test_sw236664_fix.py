#!/usr/bin/env python3
"""SW-236664: Re-run failed steps with proper sequencing"""

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
    print(output[:3000] if len(output) > 3000 else output)

def main():
    print(f"Connecting to {DEVICE_IP}...")
    ssh, chan = connect()
    print("Connected.\n")

    # Make sure we're not in configure mode
    run(chan, 'end', 3)

    # =========================================================================
    # FIX 1: Properly delete DM and verify removal
    # =========================================================================
    print("="*80)
    print("FIX 1: Delete DM_CLI_TAB_mep1 with proper commit verification")
    print("="*80)

    # Capture current session ID
    out_before = run_show(chan,
        'show services performance-monitoring cfm tests proactive two-way-delay session-name DM_CLI_TAB_mep1 detail', 12)
    before_match = re.search(r'Session ID: (\d+)', out_before)
    before_id = before_match.group(1) if before_match else "unknown"
    print(f"Current Session ID before delete: {before_id}")

    # Enter configure mode explicitly and delete
    run(chan, 'configure', 5)
    run(chan, 'delete services performance-monitoring cfm two-way-delay-measurement DM_CLI_TAB_mep1', 3)

    # Show the diff before committing
    out_diff = run(chan, 'show config compare', 10)
    print(f"Config diff:\n{out_diff}")

    # Commit and wait
    out_commit = run(chan, 'commit', 20)
    print(f"Commit output:\n{out_commit}")

    # Exit configure mode
    run(chan, 'end', 3)

    # Wait for the DM session to be removed
    time.sleep(5)

    # Verify DM is actually gone
    out_removed = run_show(chan,
        'show services performance-monitoring cfm tests proactive', 10)
    dm_gone = 'DM_CLI_TAB_mep1' not in out_removed
    log_step("Fix 1a: DM_CLI_TAB_mep1 removed", out_removed, dm_gone)

    # Re-add the DM session
    run(chan, 'configure', 5)
    run(chan, 'services performance-monitoring cfm two-way-delay-measurement DM_CLI_TAB_mep1 admin-state enabled', 2)
    run(chan, 'services performance-monitoring cfm two-way-delay-measurement DM_CLI_TAB_mep1 description cli_tab_test', 2)
    run(chan, 'services performance-monitoring cfm two-way-delay-measurement DM_CLI_TAB_mep1 profile DM_PROF_CLI', 2)
    run(chan, 'services performance-monitoring cfm two-way-delay-measurement DM_CLI_TAB_mep1 source maintenance-domain MD-CUST maintenance-association MA-CUST mep-id 1', 2)
    run(chan, 'services performance-monitoring cfm two-way-delay-measurement DM_CLI_TAB_mep1 target mep-id 2', 2)

    out_commit2 = run(chan, 'commit', 20)
    print(f"Re-add commit output:\n{out_commit2}")
    run(chan, 'end', 3)

    time.sleep(15)

    # Verify it's back with a new Session ID
    out_readded = run_show(chan,
        'show services performance-monitoring cfm tests proactive two-way-delay session-name DM_CLI_TAB_mep1 detail', 12)
    after_match = re.search(r'Session ID: (\d+)', out_readded)
    after_id = after_match.group(1) if after_match else "unknown"
    new_session = before_id != after_id
    log_step(f"Fix 1b: DM re-added (Session ID: {before_id} -> {after_id})", out_readded,
             'DM_CLI_TAB_mep1' in out_readded and 'MEP-ID: 1' in out_readded)

    # =========================================================================
    # FIX 2: On-demand DM (remove proactive DM_CLI_TAB_mep3 first)
    # =========================================================================
    print("\n" + "="*80)
    print("FIX 2: On-demand DM tests (after removing proactive DM_CLI_TAB_mep3)")
    print("="*80)

    # First check if DM_CLI_TAB_mep3 exists and remove it
    run(chan, 'configure', 5)
    run(chan, 'delete services performance-monitoring cfm two-way-delay-measurement DM_CLI_TAB_mep3', 3)
    out_commit3 = run(chan, 'commit', 20)
    print(f"Remove DM_CLI_TAB_mep3 commit:\n{out_commit3}")
    run(chan, 'end', 3)
    time.sleep(5)

    # On-demand DM with mac-address
    out_ondemand_mac = run(chan,
        'run ethernet-oam cfm on-demand delay-measurement two-way maintenance-domain MD-CUST1 maintenance-association MA-CUST1 target mac-address 84:40:76:90:cd:15',
        25)
    ondemand_mac_ok = 'Success rate' in out_ondemand_mac and 'in progress' not in out_ondemand_mac
    log_step("Fix 2a: On-demand DM (mac-address)", out_ondemand_mac, ondemand_mac_ok)

    time.sleep(5)

    # On-demand DM with mep-id
    out_ondemand_mep = run(chan,
        'run ethernet-oam cfm on-demand delay-measurement two-way maintenance-domain MD-CUST1 maintenance-association MA-CUST1 target mep-id 4',
        25)
    ondemand_mep_ok = 'Success rate' in out_ondemand_mep and 'in progress' not in out_ondemand_mep
    log_step("Fix 2b: On-demand DM (mep-id)", out_ondemand_mep, ondemand_mep_ok)

    # On-demand detail
    time.sleep(3)
    out_ondemand_detail = run_show(chan,
        'show services performance-monitoring cfm tests on-demand two-way-delay detail', 12)
    log_step("Fix 2c: On-demand DM detail", out_ondemand_detail,
             'Success rate' in out_ondemand_detail or 'DMM PDUs' in out_ondemand_detail)

    # On-demand with unreachable MAC (negative)
    time.sleep(5)
    out_ondemand_unreach = run(chan,
        'run ethernet-oam cfm on-demand delay-measurement two-way maintenance-domain MD-CUST1 maintenance-association MA-CUST1 target mac-address 22:22:22:22:22:22',
        25)
    ondemand_unreach_ok = 'Success rate' in out_ondemand_unreach
    log_step("Fix 2d: On-demand DM unreachable MAC", out_ondemand_unreach, ondemand_unreach_ok)

    # =========================================================================
    # SUMMARY
    # =========================================================================
    print("\n\n" + "="*80)
    print("RE-RUN SUMMARY")
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

    with open('/home/dn/sw236664_fix_results.json', 'w') as f:
        json.dump(results, f, indent=2, default=str)
    print(f"Results saved to /home/dn/sw236664_fix_results.json")

    ssh.close()
    print("Done.")

if __name__ == '__main__':
    main()
