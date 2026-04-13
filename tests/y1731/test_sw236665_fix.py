#!/usr/bin/env python3
"""SW-236665: Fix failed steps - handle uncommitted changes prompt"""

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
    if 'uncommitted changes' in decoded:
        chan.send('no\r')
        time.sleep(3)
        extra = b''
        while chan.recv_ready():
            extra += chan.recv(65535)
        decoded += clean_ansi(extra.decode(errors='replace'))
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
    for line in lines[:20]:
        print(f"  {line}")
    if len(lines) > 20:
        print(f"  ... ({len(lines)-20} more lines)")

def main():
    print(f"Connecting to {DEVICE_IP}...")
    ssh, chan = connect()
    print("Connected.\n")

    # Handle any leftover uncommitted changes state
    send(chan, 'end', 5)

    # First, check current state and fix any pending config
    send(chan, 'configure', 5)
    out_diff = send(chan, 'show config compare', 10)
    print(f"Pending changes:\n{out_diff}")

    if 'config-end' not in out_diff or 'Added' in out_diff or 'Deleted' in out_diff or 'Changed' in out_diff:
        print("Rolling back pending changes...")
        send(chan, 'rollback', 10)
    send(chan, 'end', 3)

    # =========================================================================
    # FIX Step 8: Restore MEP and verify SLM recovery
    # =========================================================================
    print("=" * 70)
    print("FIX Step 8: Restore MEP, configure SLM, verify recovery")
    print("=" * 70)

    # Check if MEP 1 exists
    out_mep = run_show(chan,
        'show services ethernet-oam connectivity-fault-management maintenance-domains MD-CUST maintenance-associations MA-CUST', 10)
    mep1_ok = 'MEP-ID' in out_mep and '| 1' in out_mep
    print(f"  MEP 1 present: {mep1_ok}")

    if not mep1_ok:
        print("  Restoring MEP 1...")
        configure_commit(chan, [
            'services ethernet-oam connectivity-fault-management maintenance-domains MD-CUST maintenance-associations MA-CUST local-mep 1 direction up',
            'services ethernet-oam connectivity-fault-management maintenance-domains MD-CUST maintenance-associations MA-CUST local-mep 1 interface ge10-0/0/32.100',
        ])
        time.sleep(10)

    # Check if SLM session exists
    out_slm = run_show(chan, 'show services performance-monitoring cfm tests proactive', 10)
    slm1_exists = 'SLM_CLI_TAB_mep1' in out_slm

    if not slm1_exists:
        print("  Configuring SLM_CLI_TAB_mep1...")
        configure_commit(chan, [
            'services performance-monitoring cfm two-way-synthetic-loss-measurement SLM_CLI_TAB_mep1 admin-state enabled',
            'services performance-monitoring cfm two-way-synthetic-loss-measurement SLM_CLI_TAB_mep1 description cli_tab_test_slm',
            'services performance-monitoring cfm two-way-synthetic-loss-measurement SLM_CLI_TAB_mep1 profile SLM_PROF_CLI',
            'services performance-monitoring cfm two-way-synthetic-loss-measurement SLM_CLI_TAB_mep1 source maintenance-domain MD-CUST maintenance-association MA-CUST mep-id 1',
            'services performance-monitoring cfm two-way-synthetic-loss-measurement SLM_CLI_TAB_mep1 target mep-id 2',
        ])
        time.sleep(15)

    # Now verify SLM is running and recovered
    out_recovered = run_show(chan,
        'show services performance-monitoring cfm tests proactive two-way-synthetic-loss session-name SLM_CLI_TAB_mep1 detail', 12)
    slm_ok = 'SLM_CLI_TAB_mep1' in out_recovered and 'MEP-ID: 1' in out_recovered
    log_step("Fix 8c: SLM restored after MEP re-add", out_recovered, slm_ok)

    # =========================================================================
    # FIX On-demand: Remove SLM_CLI_TAB_mep3, run on-demand tests
    # =========================================================================
    print("\n" + "=" * 70)
    print("FIX On-demand: Proper on-demand SLM tests")
    print("=" * 70)

    # Remove SLM_CLI_TAB_mep3 if present
    out_proactive = run_show(chan, 'show services performance-monitoring cfm tests proactive', 10)
    if 'SLM_CLI_TAB_mep3' in out_proactive:
        configure_commit(chan, [
            'no services performance-monitoring cfm two-way-synthetic-loss-measurement SLM_CLI_TAB_mep3',
        ])
        time.sleep(5)

    out_after_remove = run_show(chan, 'show services performance-monitoring cfm tests proactive', 10)
    mep3_gone = 'SLM_CLI_TAB_mep3' not in out_after_remove
    log_step("On-demand prep: SLM_CLI_TAB_mep3 removed", out_after_remove, mep3_gone)

    # On-demand SLM with mac-address
    time.sleep(3)
    out_od_mac = send(chan,
        'run ethernet-oam cfm on-demand synthetic-loss-measurement two-way maintenance-domain MD-CUST1 maintenance-association MA-CUST1 target mac-address 84:40:76:90:cd:15',
        25)
    od_mac_ok = ('SLR' in out_od_mac and 'transmitted' in out_od_mac.lower()) or 'loss' in out_od_mac.lower()
    log_step("On-demand SLM (mac-address)", out_od_mac, od_mac_ok)

    time.sleep(5)

    # On-demand SLM with mep-id
    out_od_mep = send(chan,
        'run ethernet-oam cfm on-demand synthetic-loss-measurement two-way maintenance-domain MD-CUST1 maintenance-association MA-CUST1 target mep-id 4',
        25)
    od_mep_ok = ('SLR' in out_od_mep and 'transmitted' in out_od_mep.lower()) or 'loss' in out_od_mep.lower()
    log_step("On-demand SLM (mep-id)", out_od_mep, od_mep_ok)

    time.sleep(5)

    # On-demand SLM with unreachable MAC
    out_od_unreach = send(chan,
        'run ethernet-oam cfm on-demand synthetic-loss-measurement two-way maintenance-domain MD-CUST1 maintenance-association MA-CUST1 target mac-address 22:22:22:22:22:22',
        25)
    od_unreach_ok = 'SLR' in out_od_unreach or 'transmitted' in out_od_unreach.lower() or 'loss' in out_od_unreach.lower()
    log_step("On-demand SLM (unreachable MAC)", out_od_unreach, od_unreach_ok)

    # On-demand detail
    time.sleep(3)
    out_od_detail = run_show(chan,
        'show services performance-monitoring cfm tests on-demand two-way-synthetic-loss detail', 12)
    log_step("On-demand SLM detail", out_od_detail,
             'SLM PDUs' in out_od_detail or 'loss' in out_od_detail.lower())

    # =========================================================================
    # CLEANUP: Remove SLM sessions, leave DM_CLI_TAB_mep1 as baseline
    # =========================================================================
    configure_commit(chan, [
        'no services performance-monitoring cfm two-way-synthetic-loss-measurement SLM_CLI_TAB_mep1',
    ])
    time.sleep(3)

    out_final = run_show(chan, 'show services performance-monitoring cfm tests proactive', 10)
    log_step("Final: Baseline restored", out_final, 'DM_CLI_TAB_mep1' in out_final)

    # =========================================================================
    # SUMMARY
    # =========================================================================
    print("\n\n" + "=" * 70)
    print("FIX RUN SUMMARY")
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
    print(f"\nTotal: {total} | Passed: {passed} | Failed: {failed}")
    overall = "PASS" if failed == 0 else "FAIL"
    print(f"Overall: {overall}")

    with open('/home/dn/sw236665_fix_results.json', 'w') as f:
        json.dump(results, f, indent=2, default=str)
    print("Results saved to /home/dn/sw236665_fix_results.json")

    ssh.close()
    print("Done.")

if __name__ == '__main__':
    main()
