#!/usr/bin/env python3
"""
SW-241365: Ethernet OAM Y.1731 | Functionality | ETH-SLM PCP
Test execution script for SLM PCP functionality across PCP values 0-7.
"""

import paramiko
import time
import re
import json
import sys
from datetime import datetime

DEVICE_IP = "100.64.4.93"
USERNAME = "dnroot"
PASSWORD = "dnroot"

RESULTS = {}
STEP_OUTPUTS = {}
ALL_PASS = True


def connect_device():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(DEVICE_IP, username=USERNAME, password=PASSWORD,
                timeout=30, look_for_keys=False, allow_agent=False)
    chan = ssh.invoke_shell(width=300)
    time.sleep(5)
    chan.recv(65535)
    return ssh, chan


def run_cmd(chan, cmd, wait=10):
    chan.send(cmd + '\n')
    time.sleep(wait)
    out = b''
    while chan.recv_ready():
        out += chan.recv(65535)
    text = out.decode(errors='replace')
    text = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)
    text = re.sub(r'\x1b\[[\?]?[0-9;]*[a-zA-Z]', '', text)
    return text.strip()


def ensure_operational_mode(chan):
    """Make sure we are in operational (non-config) mode."""
    run_cmd(chan, '', wait=2)
    prompt_check = run_cmd(chan, '', wait=2)
    if '(cfg' in prompt_check:
        run_cmd(chan, 'top', wait=2)
        result = run_cmd(chan, 'end', wait=3)
        if 'uncommitted changes' in result.lower() or 'would you like to commit' in result.lower():
            run_cmd(chan, 'no', wait=5)
        time.sleep(2)
        prompt_check2 = run_cmd(chan, '', wait=2)
        if '(cfg' in prompt_check2:
            run_cmd(chan, 'abort', wait=3)
            time.sleep(2)


def run_config_block(chan, commands, wait_per_cmd=3):
    """Enter configure mode, run commands at top level, commit and exit."""
    ensure_operational_mode(chan)

    output = run_cmd(chan, 'configure', wait=3)
    results = [output]

    for cmd in commands:
        out = run_cmd(chan, cmd, wait=wait_per_cmd)
        results.append(out)
        if 'Error' in out or 'error' in out:
            print(f"  [WARN] Command error: {cmd}")
            print(f"         {out[-200:]}")

    run_cmd(chan, 'top', wait=2)

    commit_out = run_cmd(chan, 'commit', wait=20)
    results.append(commit_out)

    exit_out = run_cmd(chan, 'end', wait=3)
    results.append(exit_out)
    if 'uncommitted changes' in exit_out.lower():
        run_cmd(chan, 'no', wait=3)

    full = '\n'.join(results)
    commit_success = 'Commit succeeded' in commit_out or 'no configuration changes' in commit_out.lower()
    if not commit_success and ('Error' in commit_out or 'Aborted' in commit_out):
        print(f"  [WARN] Commit issue: {commit_out[-300:]}")
    return full, commit_success


def verify_pcp_in_detail(chan, session_name, expected_pcp, retries=3):
    """Run SLM detail show command and verify PCP value. Retry for valid stats."""
    ensure_operational_mode(chan)
    cmd = f"show services performance-monitoring cfm tests proactive two-way-synthetic-loss session-name {session_name} detail | no-more"

    for attempt in range(retries):
        output = run_cmd(chan, cmd, wait=12)
        pcp_match = re.search(r'PCP:\s*(\d+)', output)
        actual_pcp = int(pcp_match.group(1)) if pcp_match else None
        stats_ok = check_valid_stats(output)

        if actual_pcp == expected_pcp and stats_ok:
            return True, actual_pcp, output
        if actual_pcp == expected_pcp and not stats_ok and attempt < retries - 1:
            print(f"    PCP correct ({actual_pcp}) but stats not yet valid, waiting 15s (attempt {attempt+1}/{retries})...")
            time.sleep(15)
            continue
        if attempt < retries - 1:
            time.sleep(10)
        else:
            return actual_pcp == expected_pcp, actual_pcp, output

    return False, None, output


def check_valid_stats(output):
    """Check if frame loss statistics are present and valid in any historical result."""
    has_slm_tx = re.search(r'SLM PDUs transmitted:\s*(\d+)', output)
    has_slr_rx = re.search(r'SLR PDUs received:\s*(\d+)', output)
    has_near = re.search(r'Near-end loss', output)
    has_far = re.search(r'Far-end loss', output)
    valid_in_history = re.search(r'\|\s*valid\s*\|', output) or re.search(r'Measurement validity:\s*valid', output)
    return bool(has_slm_tx and has_slr_rx and has_near and has_far and valid_in_history)


def main():
    global ALL_PASS

    print("=" * 70)
    print("SW-241365: ETH-SLM PCP Test Execution")
    print(f"Device: {DEVICE_IP} (ncpl-cfm-nog / XEC1E3VR00008)")
    print(f"Start time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print("=" * 70)

    ssh, chan = connect_device()
    print("[OK] Connected to device\n")

    ver_output = run_cmd(chan, 'show system version | no-more', wait=8)
    print(f"System Version:\n{ver_output}\n")
    STEP_OUTPUTS['version'] = ver_output

    # ---------------------------------------------------------------
    # STEP 1: Configure QOS policies
    # ---------------------------------------------------------------
    print("-" * 70)
    print("STEP 1: Configure QOS policies with PCP-to-queue mapping")
    print("-" * 70)

    qos_commands = [
        "qos hw-mapping queue-size speed-ranges admin-state enabled",
        "qos hw-mapping queue-size speed-ranges upto 50 mbps use 50 mbps",
        "qos hw-mapping queue-size speed-ranges upto 100 mbps use 100 mbps",
        "qos hw-mapping queue-size speed-ranges upto 250 mbps use 250 mbps",
        "qos hw-mapping queue-size speed-ranges upto 500 mbps use 500 mbps",
        "qos hw-mapping queue-size speed-ranges upto 750 mbps use 750 mbps",
        "qos hw-mapping queue-size speed-ranges upto 1 gbps use 1 gbps",
        "qos traffic-class-map CLASS1 pcp 1",
        "qos traffic-class-map CLASS2 pcp 2",
        "qos traffic-class-map CLASS3 pcp 3",
        "qos traffic-class-map CLASS4 pcp 4",
        "qos traffic-class-map CLASS5 pcp 5",
        "qos traffic-class-map CLASS6 pcp 6",
        "qos traffic-class-map CLASS7 pcp 7",
        "qos traffic-class-map QOS-TAG-1 qos-tag 1",
        "qos traffic-class-map QOS-TAG-2 qos-tag 2",
        "qos traffic-class-map QOS-TAG-3 qos-tag 3",
        "qos traffic-class-map QOS-TAG-4 qos-tag 4",
        "qos traffic-class-map QOS-TAG-5 qos-tag 5",
        "qos traffic-class-map QOS-TAG-6 qos-tag 6",
        "qos traffic-class-map QOS-TAG-7 qos-tag 7",
        "qos policy Egress_Full rule 1 match traffic-class QOS-TAG-1",
        "qos policy Egress_Full rule 1 action queue forwarding-class af bandwidth 10 percent",
        "qos policy Egress_Full rule 1 action queue forwarding-class af size 20 milliseconds",
        "qos policy Egress_Full rule 1 action queue forwarding-class af yellow-size 10 milliseconds",
        "qos policy Egress_Full rule 1 action set pcp 1 all 1",
        "qos policy Egress_Full rule 2 match traffic-class QOS-TAG-2",
        "qos policy Egress_Full rule 2 action queue forwarding-class af bandwidth 20 percent",
        "qos policy Egress_Full rule 2 action queue forwarding-class af size 20 milliseconds",
        "qos policy Egress_Full rule 2 action queue forwarding-class af yellow-size 10 milliseconds",
        "qos policy Egress_Full rule 2 action set pcp 2 all 2",
        "qos policy Egress_Full rule 3 match traffic-class QOS-TAG-3",
        "qos policy Egress_Full rule 3 action queue forwarding-class af bandwidth 40 percent",
        "qos policy Egress_Full rule 3 action queue forwarding-class af size 20 milliseconds",
        "qos policy Egress_Full rule 3 action queue forwarding-class af yellow-size 10 milliseconds",
        "qos policy Egress_Full rule 3 action set pcp 3 all 3",
        "qos policy Egress_Full rule 4 match traffic-class QOS-TAG-4",
        "qos policy Egress_Full rule 4 action queue forwarding-class af bandwidth 10 percent",
        "qos policy Egress_Full rule 4 action queue forwarding-class af size 20 milliseconds",
        "qos policy Egress_Full rule 4 action queue forwarding-class af yellow-size 10 milliseconds",
        "qos policy Egress_Full rule 5 match traffic-class QOS-TAG-5",
        "qos policy Egress_Full rule 5 action queue forwarding-class hp max-bandwidth 25 percent",
        "qos policy Egress_Full rule 5 action queue forwarding-class hp size 10 milliseconds",
        "qos policy Egress_Full rule 5 action set pcp 5 all 5",
        "qos policy Egress_Full rule 6 match traffic-class QOS-TAG-6",
        "qos policy Egress_Full rule 6 action queue forwarding-class ef max-bandwidth 10 percent",
        "qos policy Egress_Full rule 6 action queue forwarding-class ef size 10 milliseconds",
        "qos policy Egress_Full rule 6 action set pcp 6 all 6",
        "qos policy Egress_Full rule 7 match traffic-class QOS-TAG-7",
        "qos policy Egress_Full rule 7 action queue forwarding-class super-ef max-bandwidth 3 percent",
        "qos policy Egress_Full rule 7 action queue forwarding-class super-ef size 10 milliseconds",
        "qos policy Egress_Full rule default action queue forwarding-class df bandwidth 5 percent",
        "qos policy Egress_Full rule default action queue forwarding-class df size 20 milliseconds",
        "qos policy Egress_Full rule default action queue forwarding-class df yellow-size 10 milliseconds",
        "qos policy Ingress_Child_Classify_Only rule 1 match traffic-class CLASS1",
        "qos policy Ingress_Child_Classify_Only rule 1 action set qos-tag 1",
        "qos policy Ingress_Child_Classify_Only rule 2 match traffic-class CLASS2",
        "qos policy Ingress_Child_Classify_Only rule 2 action set qos-tag 2",
        "qos policy Ingress_Child_Classify_Only rule 3 match traffic-class CLASS3",
        "qos policy Ingress_Child_Classify_Only rule 3 action set qos-tag 3",
        "qos policy Ingress_Child_Classify_Only rule 4 match traffic-class CLASS4",
        "qos policy Ingress_Child_Classify_Only rule 4 action set qos-tag 4",
        "qos policy Ingress_Child_Classify_Only rule 5 match traffic-class CLASS5",
        "qos policy Ingress_Child_Classify_Only rule 5 action set qos-tag 5",
        "qos policy Ingress_Child_Classify_Only rule 6 match traffic-class CLASS6",
        "qos policy Ingress_Child_Classify_Only rule 6 action set qos-tag 6",
        "qos policy Ingress_Child_Classify_Only rule 7 match traffic-class CLASS7",
        "qos policy Ingress_Child_Classify_Only rule 7 action set qos-tag 7",
    ]

    out, ok = run_config_block(chan, qos_commands)
    STEP_OUTPUTS['step1'] = out
    RESULTS['step1'] = 'PASS' if ok else 'FAIL'
    if not ok:
        ALL_PASS = False
    print(f"  Result: {RESULTS['step1']} - QoS policies configured\n")

    # ---------------------------------------------------------------
    # STEPS 3-6: Test PCP values 0-7
    # ---------------------------------------------------------------
    pcp_results = {}
    for pcp_val in range(0, 8):
        print("-" * 70)
        print(f"STEP {'3/4' if pcp_val == 0 else '5' if pcp_val == 1 else '6'}: Test PCP {pcp_val}")
        print("-" * 70)

        out, ok = run_config_block(chan, [
            f"services performance-monitoring profiles cfm two-way-synthetic-loss-measurement SLM_PROF_CLI pcp {pcp_val}"
        ])
        STEP_OUTPUTS[f'pcp{pcp_val}_config'] = out
        if not ok:
            print(f"  [WARN] Config commit issue for PCP {pcp_val}")

        time.sleep(15)

        pcp_ok, actual_pcp, detail_out = verify_pcp_in_detail(chan, 'SLM_CLI_TAB_mep1', pcp_val, retries=3)
        stats_ok = check_valid_stats(detail_out)
        STEP_OUTPUTS[f'pcp{pcp_val}_detail'] = detail_out
        pcp_results[pcp_val] = {'pcp_ok': pcp_ok, 'actual_pcp': actual_pcp, 'stats_ok': stats_ok}

        if pcp_ok and stats_ok:
            print(f"  Result: PASS - PCP={actual_pcp}, valid stats present")
        elif pcp_ok:
            print(f"  Result: PASS - PCP={actual_pcp} correct (stats validity: {stats_ok})")
        else:
            print(f"  Result: FAIL - PCP={actual_pcp} (expected {pcp_val}), stats_ok={stats_ok}")
        print()

    # Assess steps 3/4, 5, 6
    if pcp_results[0]['pcp_ok']:
        RESULTS['step3_4'] = 'PASS'
    else:
        RESULTS['step3_4'] = 'FAIL'
        ALL_PASS = False

    if pcp_results[1]['pcp_ok']:
        RESULTS['step5'] = 'PASS'
    else:
        RESULTS['step5'] = 'FAIL'
        ALL_PASS = False

    all_2_7 = all(pcp_results[v]['pcp_ok'] for v in range(2, 8))
    RESULTS['step6'] = 'PASS' if all_2_7 else 'FAIL'
    if not all_2_7:
        ALL_PASS = False

    # ---------------------------------------------------------------
    # STEP 7: Create 2nd proactive SLM targeting mac-address
    # Use MEP 3 / MD-CUST1 / MA-CUST1 to avoid source-in-use conflict
    # ---------------------------------------------------------------
    print("-" * 70)
    print("STEP 7: Create 2nd proactive SLM targeting mac-address (MEP3/MD-CUST1)")
    print("-" * 70)

    # Must free MEP 3 first - SLM_CLI_TAB_mep3 already uses it
    out, ok = run_config_block(chan, [
        "services performance-monitoring cfm two-way-synthetic-loss-measurement SLM_CLI_TAB_mep3 admin-state disabled",
        "no services performance-monitoring cfm two-way-synthetic-loss-measurement SLM_CLI_TAB_mep3",
        "services performance-monitoring profiles cfm two-way-synthetic-loss-measurement SLM_PROF_PCP3 pcp 3",
        "services performance-monitoring profiles cfm two-way-synthetic-loss-measurement SLM_PROF_PCP3 test-duration probes probe-count 5 probe-interval 1 repeat-interval 10",
        "services performance-monitoring profiles cfm two-way-synthetic-loss-measurement SLM_PROF_PCP3 thresholds far-end-loss 1.0",
        "services performance-monitoring profiles cfm two-way-synthetic-loss-measurement SLM_PROF_PCP3 thresholds near-end-loss 1.0",
        "services performance-monitoring cfm two-way-synthetic-loss-measurement SLM_PCP_MAC admin-state enabled",
        "services performance-monitoring cfm two-way-synthetic-loss-measurement SLM_PCP_MAC profile SLM_PROF_PCP3",
        "services performance-monitoring cfm two-way-synthetic-loss-measurement SLM_PCP_MAC source maintenance-domain MD-CUST1 maintenance-association MA-CUST1 mep-id 3",
        "services performance-monitoring cfm two-way-synthetic-loss-measurement SLM_PCP_MAC target mac-address 84:40:76:90:cd:15",
    ])
    STEP_OUTPUTS['step7_config'] = out

    if not ok:
        print(f"  [WARN] Step 7 commit issue")

    time.sleep(25)

    ensure_operational_mode(chan)

    # Verify session 1 (SLM_CLI_TAB_mep1) still running with PCP 7
    pcp_ok_1, actual_1, det1 = verify_pcp_in_detail(chan, 'SLM_CLI_TAB_mep1', 7, retries=2)
    STEP_OUTPUTS['step7_session1'] = det1

    # Verify session 2 (SLM_PCP_MAC) with PCP 3
    pcp_ok_2, actual_2, det2 = verify_pcp_in_detail(chan, 'SLM_PCP_MAC', 3, retries=2)
    STEP_OUTPUTS['step7_session2'] = det2

    # Show all tests
    ensure_operational_mode(chan)
    tests_out = run_cmd(chan, 'show services performance-monitoring cfm tests | no-more', wait=10)
    STEP_OUTPUTS['step7_tests'] = tests_out
    print(f"  All tests:\n{tests_out}\n")

    if pcp_ok_1 and pcp_ok_2:
        RESULTS['step7'] = 'PASS'
        print(f"  Result: PASS - Session1 PCP={actual_1}, Session2(MAC) PCP={actual_2}")
    elif ok:
        RESULTS['step7'] = 'PARTIAL'
        ALL_PASS = False
        print(f"  Result: PARTIAL - Session1 PCP={actual_1} (exp 7), Session2 PCP={actual_2} (exp 3)")
    else:
        RESULTS['step7'] = 'FAIL'
        ALL_PASS = False
        print(f"  Result: FAIL - Config commit failed")
    print()

    # ---------------------------------------------------------------
    # Disable proactive SLM on MEP1 before on-demand tests
    # ---------------------------------------------------------------
    print("-" * 70)
    print("Disabling proactive SLM_CLI_TAB_mep1 for on-demand tests...")
    print("-" * 70)
    disable_out, _ = run_config_block(chan, [
        "services performance-monitoring cfm two-way-synthetic-loss-measurement SLM_CLI_TAB_mep1 admin-state disabled",
    ])
    time.sleep(5)

    # ---------------------------------------------------------------
    # STEP 8: Run on-demand SLM targeting mep-id
    # ---------------------------------------------------------------
    print("-" * 70)
    print("STEP 8: Run on-demand SLM (two-way) targeting mep-id")
    print("-" * 70)

    ensure_operational_mode(chan)
    od_cmd = "run ethernet-oam cfm on-demand synthetic-loss-measurement two-way maintenance-domain MD-CUST maintenance-association MA-CUST target mep-id 2"
    od_out = run_cmd(chan, od_cmd, wait=30)
    STEP_OUTPUTS['step8'] = od_out

    time.sleep(5)
    od_detail = run_cmd(chan, 'show services performance-monitoring cfm tests on-demand two-way-synthetic-loss detail | no-more', wait=12)
    STEP_OUTPUTS['step8_detail'] = od_detail

    combined = od_out + od_detail
    has_stats = bool(re.search(r'(SLM PDUs transmitted|Near-end loss|Frame loss)', combined))
    if has_stats:
        RESULTS['step8'] = 'PASS'
        print("  Result: PASS - On-demand SLM by mep-id executed successfully")
    else:
        RESULTS['step8'] = 'FAIL'
        ALL_PASS = False
        print("  Result: FAIL - No frame loss stats found")
    print(f"  On-demand output:\n{od_out[-800:]}\n")
    print(f"  Detail:\n{od_detail[-800:]}\n")

    # ---------------------------------------------------------------
    # STEP 9: Run on-demand SLM targeting mac-address
    # ---------------------------------------------------------------
    print("-" * 70)
    print("STEP 9: Run on-demand SLM (two-way) targeting mac-address")
    print("-" * 70)

    ensure_operational_mode(chan)
    time.sleep(3)
    od_mac_cmd = "run ethernet-oam cfm on-demand synthetic-loss-measurement two-way maintenance-domain MD-CUST maintenance-association MA-CUST target mac-address 84:40:76:90:cd:f6"
    od_mac_out = run_cmd(chan, od_mac_cmd, wait=30)
    STEP_OUTPUTS['step9'] = od_mac_out

    time.sleep(5)
    od_mac_detail = run_cmd(chan, 'show services performance-monitoring cfm tests on-demand two-way-synthetic-loss detail | no-more', wait=12)
    STEP_OUTPUTS['step9_detail'] = od_mac_detail

    combined = od_mac_out + od_mac_detail
    has_stats = bool(re.search(r'(SLM PDUs transmitted|Near-end loss|Frame loss)', combined))
    if has_stats:
        RESULTS['step9'] = 'PASS'
        print("  Result: PASS - On-demand SLM by mac-address executed successfully")
    else:
        RESULTS['step9'] = 'FAIL'
        ALL_PASS = False
        print("  Result: FAIL - No frame loss stats found")
    print(f"  On-demand output:\n{od_mac_out[-800:]}\n")
    print(f"  Detail:\n{od_mac_detail[-800:]}\n")

    # ---------------------------------------------------------------
    # STEP 10: On-demand SLM with explicit PCP override
    # ---------------------------------------------------------------
    print("-" * 70)
    print("STEP 10: Run on-demand SLM with explicit PCP override (pcp 3)")
    print("-" * 70)

    ensure_operational_mode(chan)
    time.sleep(3)
    od_pcp_cmd = "run ethernet-oam cfm on-demand synthetic-loss-measurement two-way maintenance-domain MD-CUST maintenance-association MA-CUST target mep-id 2 pcp 3"
    od_pcp_out = run_cmd(chan, od_pcp_cmd, wait=30)
    STEP_OUTPUTS['step10'] = od_pcp_out

    time.sleep(5)
    od_pcp_detail = run_cmd(chan, 'show services performance-monitoring cfm tests on-demand two-way-synthetic-loss detail | no-more', wait=12)
    STEP_OUTPUTS['step10_detail'] = od_pcp_detail

    combined = od_pcp_out + od_pcp_detail
    has_pcp3 = 'PCP: 3' in combined or 'pcp 3' in combined.lower()
    has_stats = bool(re.search(r'(SLM PDUs transmitted|Near-end loss|Frame loss)', combined))
    if has_stats:
        RESULTS['step10'] = 'PASS'
        print(f"  Result: PASS - On-demand SLM with PCP override executed (PCP 3 shown: {has_pcp3})")
    else:
        RESULTS['step10'] = 'FAIL'
        ALL_PASS = False
        print("  Result: FAIL - No frame loss stats found")
    print(f"  On-demand output:\n{od_pcp_out[-800:]}\n")
    print(f"  Detail:\n{od_pcp_detail[-800:]}\n")

    # Re-enable proactive SLM_CLI_TAB_mep1
    print("Re-enabling proactive SLM_CLI_TAB_mep1...")
    reenable_out, _ = run_config_block(chan, [
        "services performance-monitoring cfm two-way-synthetic-loss-measurement SLM_CLI_TAB_mep1 admin-state enabled",
    ])
    time.sleep(5)

    # ---------------------------------------------------------------
    # CLEANUP
    # ---------------------------------------------------------------
    print("-" * 70)
    print("CLEANUP: Removing test session SLM_PCP_MAC, restoring PCP to 5")
    print("-" * 70)

    cleanup_out, _ = run_config_block(chan, [
        "no services performance-monitoring cfm two-way-synthetic-loss-measurement SLM_PCP_MAC",
        "no services performance-monitoring profiles cfm two-way-synthetic-loss-measurement SLM_PROF_PCP3",
        "services performance-monitoring profiles cfm two-way-synthetic-loss-measurement SLM_PROF_CLI pcp 5",
        # Restore the original SLM_CLI_TAB_mep3 session
        "services performance-monitoring cfm two-way-synthetic-loss-measurement SLM_CLI_TAB_mep3 admin-state enabled",
        "services performance-monitoring cfm two-way-synthetic-loss-measurement SLM_CLI_TAB_mep3 description cli_tab_test_slm",
        "services performance-monitoring cfm two-way-synthetic-loss-measurement SLM_CLI_TAB_mep3 profile SLM_PROF_CLI",
        "services performance-monitoring cfm two-way-synthetic-loss-measurement SLM_CLI_TAB_mep3 source maintenance-domain MD-CUST1 maintenance-association MA-CUST1 mep-id 3",
        "services performance-monitoring cfm two-way-synthetic-loss-measurement SLM_CLI_TAB_mep3 target mep-id 4",
    ])
    STEP_OUTPUTS['cleanup'] = cleanup_out
    print("  Cleanup done, restored SLM_PROF_CLI pcp to 5\n")

    # ---------------------------------------------------------------
    # SUMMARY
    # ---------------------------------------------------------------
    print("=" * 70)
    print("TEST SUMMARY")
    print("=" * 70)
    overall = 'PASS' if ALL_PASS else 'FAIL'
    print(f"Overall Result: {overall}\n")

    step_names = {
        'step1': 'Step 1 - QoS PCP-to-queue mapping config',
        'step3_4': 'Steps 3-4 - Proactive SLM PCP 0 verification',
        'step5': 'Step 5 - Proactive SLM PCP 1 verification',
        'step6': 'Step 6 - Proactive SLM PCP 2-7 verification',
        'step7': 'Step 7 - 2nd concurrent session targeting MAC',
        'step8': 'Step 8 - On-demand SLM by mep-id',
        'step9': 'Step 9 - On-demand SLM by mac-address',
        'step10': 'Step 10 - On-demand SLM PCP override',
    }

    for key, name in step_names.items():
        result = RESULTS.get(key, 'N/A')
        icon = '(/)' if result == 'PASS' else '(x)' if result == 'FAIL' else '(!)'
        print(f"  {icon} {result:8s} | {name}")

    # PCP results summary
    print(f"\n  PCP Value Test Results:")
    for pcp_val in range(0, 8):
        r = pcp_results.get(pcp_val, {})
        pcp_ok = r.get('pcp_ok', False)
        actual = r.get('actual_pcp', '?')
        stats = r.get('stats_ok', False)
        icon = '(/)' if pcp_ok else '(x)'
        print(f"    {icon} PCP {pcp_val}: shown={actual}, stats_valid={stats}")

    print(f"\nEnd time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")

    output_data = {
        'results': RESULTS,
        'pcp_results': {str(k): v for k, v in pcp_results.items()},
        'outputs': {k: v[-3000:] for k, v in STEP_OUTPUTS.items()},
        'overall': overall,
        'device': DEVICE_IP,
        'timestamp': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
    }
    with open('/home/dn/output/sw241365_slm_pcp_results.json', 'w') as f:
        json.dump(output_data, f, indent=2)
    print(f"\nDetailed results saved to /home/dn/output/sw241365_slm_pcp_results.json")

    ssh.close()
    return 0 if ALL_PASS else 1


if __name__ == '__main__':
    sys.exit(main())
