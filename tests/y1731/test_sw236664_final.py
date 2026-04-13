#!/usr/bin/env python3
"""SW-236664: Fix failed steps using correct 'no' syntax for deletion"""

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
    print(f"  > {cmd}")
    return decoded

def run_show(chan, cmd, wait=10):
    return send(chan, cmd + ' | no-more', wait)

def log_step(name, output, passed=None):
    results[name] = {
        'output': output,
        'passed': passed,
        'timestamp': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')
    }
    status = "PASS" if passed else ("FAIL" if passed is False else "INFO")
    print(f"\n[{status}] {name}")
    lines = output.strip().split('\n')
    for line in lines[:30]:
        print(f"  {line}")
    if len(lines) > 30:
        print(f"  ... ({len(lines)-30} more lines)")

def main():
    print(f"Connecting to {DEVICE_IP}...")
    ssh, chan = connect()
    print("Connected.\n")

    send(chan, 'end', 3)

    # =========================================================================
    # FIX STEP 2b: Remove DM_CLI_TAB_mep1 using 'no' prefix, then re-add
    # =========================================================================
    print("=" * 70)
    print("FIX STEP 2b: Remove DM config using 'no' prefix")
    print("=" * 70)

    out_before = run_show(chan,
        'show services performance-monitoring cfm tests proactive two-way-delay session-name DM_CLI_TAB_mep1 detail', 12)
    before_match = re.search(r'Session ID: (\d+)', out_before)
    before_id = before_match.group(1) if before_match else "?"
    print(f"  Session ID before delete: {before_id}")

    send(chan, 'configure', 5)
    send(chan, 'no services performance-monitoring cfm two-way-delay-measurement DM_CLI_TAB_mep1', 3)
    out_diff = send(chan, 'show config compare', 10)
    print(f"  Config diff after 'no':\n{out_diff}")
    out_commit = send(chan, 'commit', 20)
    print(f"  Commit result: {out_commit.strip()}")
    send(chan, 'end', 3)

    time.sleep(5)
    out_removed = run_show(chan,
        'show services performance-monitoring cfm tests proactive', 10)
    dm1_gone = 'DM_CLI_TAB_mep1' not in out_removed
    log_step("Step 2b-1: DM_CLI_TAB_mep1 removed", out_removed, dm1_gone)

    # Re-add DM_CLI_TAB_mep1
    send(chan, 'configure', 5)
    send(chan, 'services performance-monitoring cfm two-way-delay-measurement DM_CLI_TAB_mep1 admin-state enabled', 2)
    send(chan, 'services performance-monitoring cfm two-way-delay-measurement DM_CLI_TAB_mep1 description cli_tab_test', 2)
    send(chan, 'services performance-monitoring cfm two-way-delay-measurement DM_CLI_TAB_mep1 profile DM_PROF_CLI', 2)
    send(chan, 'services performance-monitoring cfm two-way-delay-measurement DM_CLI_TAB_mep1 source maintenance-domain MD-CUST maintenance-association MA-CUST mep-id 1', 2)
    send(chan, 'services performance-monitoring cfm two-way-delay-measurement DM_CLI_TAB_mep1 target mep-id 2', 2)
    out_commit2 = send(chan, 'commit', 20)
    print(f"  Re-add commit: {out_commit2.strip()}")
    send(chan, 'end', 3)

    time.sleep(15)
    out_readded = run_show(chan,
        'show services performance-monitoring cfm tests proactive two-way-delay session-name DM_CLI_TAB_mep1 detail', 12)
    after_match = re.search(r'Session ID: (\d+)', out_readded)
    after_id = after_match.group(1) if after_match else "?"
    new_session = before_id != after_id
    log_step(f"Step 2b-2: DM re-added (Session {before_id} -> {after_id})", out_readded,
             'DM_CLI_TAB_mep1' in out_readded and 'MEP-ID: 1' in out_readded)

    # =========================================================================
    # FIX ON-DEMAND: Remove DM_CLI_TAB_mep3 first, then run on-demand
    # =========================================================================
    print("\n" + "=" * 70)
    print("FIX ON-DEMAND: Remove DM_CLI_TAB_mep3 then run on-demand DM")
    print("=" * 70)

    send(chan, 'configure', 5)
    send(chan, 'no services performance-monitoring cfm two-way-delay-measurement DM_CLI_TAB_mep3', 3)
    out_diff2 = send(chan, 'show config compare', 10)
    print(f"  Config diff:\n{out_diff2}")
    out_commit3 = send(chan, 'commit', 20)
    print(f"  Commit: {out_commit3.strip()}")
    send(chan, 'end', 3)

    time.sleep(5)
    out_proactive = run_show(chan, 'show services performance-monitoring cfm tests proactive', 10)
    mep3_gone = 'DM_CLI_TAB_mep3' not in out_proactive
    log_step("On-demand prep: DM_CLI_TAB_mep3 removed", out_proactive, mep3_gone)

    # On-demand DM with mac-address on MD-CUST1
    time.sleep(3)
    out_od_mac = send(chan,
        'run ethernet-oam cfm on-demand delay-measurement two-way maintenance-domain MD-CUST1 maintenance-association MA-CUST1 target mac-address 84:40:76:90:cd:15',
        25)
    od_mac_ok = 'Success rate' in out_od_mac and 'in progress' not in out_od_mac
    log_step("On-demand DM (mac-address)", out_od_mac, od_mac_ok)

    time.sleep(5)

    # On-demand DM with mep-id on MD-CUST1
    out_od_mep = send(chan,
        'run ethernet-oam cfm on-demand delay-measurement two-way maintenance-domain MD-CUST1 maintenance-association MA-CUST1 target mep-id 4',
        25)
    od_mep_ok = 'Success rate' in out_od_mep and 'in progress' not in out_od_mep
    log_step("On-demand DM (mep-id)", out_od_mep, od_mep_ok)

    time.sleep(5)

    # On-demand DM with unreachable MAC
    out_od_unreach = send(chan,
        'run ethernet-oam cfm on-demand delay-measurement two-way maintenance-domain MD-CUST1 maintenance-association MA-CUST1 target mac-address 22:22:22:22:22:22',
        25)
    od_unreach_ok = 'Success rate' in out_od_unreach
    no_fake_dmr = 'DMR PDUs received: 0' in out_od_unreach.replace(' ', '') or 'received:    0' in out_od_unreach or 'received: 0' in out_od_unreach
    log_step("On-demand DM (unreachable MAC)", out_od_unreach, od_unreach_ok)

    # On-demand detail
    time.sleep(3)
    out_od_detail = run_show(chan,
        'show services performance-monitoring cfm tests on-demand two-way-delay detail', 12)
    log_step("On-demand DM detail", out_od_detail,
             'DMM PDUs' in out_od_detail)

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
        else:
            print(f"  [INFO] {step}")

    print(f"\nTotal: {total} | Passed: {passed} | Failed: {failed}")
    overall = "PASS" if failed == 0 else "FAIL"
    print(f"Overall: {overall}")

    with open('/home/dn/sw236664_fix_final.json', 'w') as f:
        json.dump(results, f, indent=2, default=str)
    print("Results saved to /home/dn/sw236664_fix_final.json")

    ssh.close()
    print("Done.")

if __name__ == '__main__':
    main()
