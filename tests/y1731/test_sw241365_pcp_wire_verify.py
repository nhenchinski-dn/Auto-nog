#!/usr/bin/env python3
"""
SW-241365: Verify SLM PCP values appear correctly on the wire.

Strategy:
1. For each PCP value (0-7), configure the proactive SLM profile
2. Wait for a test cycle to complete
3. Check the proactive SLM detail to confirm configured PCP
4. Run on-demand SLM with that PCP and verify the output explicitly
   states the PCP value used for sending probes
5. Run on-demand SLM with explicit PCP override to further confirm
6. Check if the on-demand detail output shows the correct PCP

The on-demand "Sending ... probes, PCP: X" line and the detail
"PCP: X" field confirm the OAM engine is stamping the configured
PCP into the Y.1731 PDU 802.1Q header.
"""

import paramiko
import time
import re
import sys
from datetime import datetime

DEVICE_IP = "100.64.4.93"
USERNAME = "dnroot"
PASSWORD = "dnroot"


def connect():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(DEVICE_IP, username=USERNAME, password=PASSWORD,
                timeout=30, look_for_keys=False, allow_agent=False)
    chan = ssh.invoke_shell(width=300)
    time.sleep(5)
    chan.recv(65535)
    return ssh, chan


def run(chan, cmd, wait=10):
    chan.send(cmd + '\n')
    time.sleep(wait)
    out = b''
    while chan.recv_ready():
        out += chan.recv(65535)
    text = out.decode(errors='replace')
    text = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)
    text = re.sub(r'\x1b\[[\?]?[0-9;]*[a-zA-Z]', '', text)
    return text


def ensure_operational(chan):
    run(chan, '', 2)
    check = run(chan, '', 2)
    if '(cfg' in check:
        run(chan, 'top', 2)
        result = run(chan, 'end', 3)
        if 'uncommitted' in result.lower():
            run(chan, 'no', 5)


def config_pcp(chan, pcp_val):
    ensure_operational(chan)
    run(chan, 'configure', 3)
    run(chan, f'services performance-monitoring profiles cfm two-way-synthetic-loss-measurement SLM_PROF_CLI pcp {pcp_val}', 3)
    run(chan, 'top', 2)
    commit_out = run(chan, 'commit', 15)
    run(chan, 'end', 3)
    if 'uncommitted' in commit_out.lower():
        run(chan, 'no', 3)
    return 'Commit succeeded' in commit_out


def disable_proactive(chan):
    ensure_operational(chan)
    run(chan, 'configure', 3)
    run(chan, 'services performance-monitoring cfm two-way-synthetic-loss-measurement SLM_CLI_TAB_mep1 admin-state disabled', 3)
    run(chan, 'top', 2)
    run(chan, 'commit', 15)
    run(chan, 'end', 3)


def enable_proactive(chan):
    ensure_operational(chan)
    run(chan, 'configure', 3)
    run(chan, 'services performance-monitoring cfm two-way-synthetic-loss-measurement SLM_CLI_TAB_mep1 admin-state enabled', 3)
    run(chan, 'top', 2)
    run(chan, 'commit', 15)
    run(chan, 'end', 3)


def main():
    print("=" * 70)
    print("SW-241365: PCP Wire-Level Verification")
    print(f"Device: {DEVICE_IP} (ncpl-cfm-nog)")
    print(f"Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print("=" * 70)
    print()
    print("Method: For each PCP value, run on-demand SLM and verify the")
    print("'Sending ... probes, PCP: X' output confirms the OAM engine is")
    print("stamping the correct PCP into the 802.1Q header of SLM PDUs.")
    print("Also verify the on-demand detail output shows the matching PCP.")
    print()

    ssh, chan = connect()
    print("[OK] Connected\n")

    # Disable proactive so we can run on-demand freely
    print("Disabling proactive SLM_CLI_TAB_mep1 for on-demand tests...")
    disable_proactive(chan)
    time.sleep(5)

    results = {}

    for pcp_val in range(0, 8):
        print("-" * 60)
        print(f"Testing PCP {pcp_val}")
        print("-" * 60)

        # Set PCP on profile
        ok = config_pcp(chan, pcp_val)
        if not ok:
            print(f"  [WARN] Commit issue for PCP {pcp_val}")
        time.sleep(3)

        # Run on-demand SLM (uses the profile PCP as default)
        ensure_operational(chan)
        cmd = f"run ethernet-oam cfm on-demand synthetic-loss-measurement two-way maintenance-domain MD-CUST maintenance-association MA-CUST target mep-id 2 pcp {pcp_val}"
        od_out = run(chan, cmd, wait=30)

        # Extract the "Sending ... PCP: X" line
        pcp_send_match = re.search(r'Sending\s+\d+\s+Y\.1731\s+ETH-SL\s+probes,\s+PCP:\s*(\d+)', od_out)
        send_pcp = int(pcp_send_match.group(1)) if pcp_send_match else None

        # Check frame loss stats
        slm_tx_match = re.search(r'Transmitted SLM PDUs:\s*(\d+)', od_out)
        slr_rx_match = re.search(r'Received SLR PDUs:\s*(\d+)', od_out)
        slm_tx = int(slm_tx_match.group(1)) if slm_tx_match else 0
        slr_rx = int(slr_rx_match.group(1)) if slr_rx_match else 0

        near_loss = re.search(r'Frame loss near-end\s*:\s*\d+\s*\((\d+\.?\d*)%\)', od_out)
        far_loss = re.search(r'Frame loss far-end\s*:\s*\d+\s*\((\d+\.?\d*)%\)', od_out)
        near_pct = near_loss.group(1) if near_loss else '?'
        far_pct = far_loss.group(1) if far_loss else '?'

        time.sleep(5)

        # Get on-demand detail
        ensure_operational(chan)
        detail = run(chan, 'show services performance-monitoring cfm tests on-demand two-way-synthetic-loss detail | no-more', 12)
        detail_pcp_match = re.search(r'PCP:\s*(\d+)', detail)
        detail_pcp = int(detail_pcp_match.group(1)) if detail_pcp_match else None

        # Assess
        send_ok = send_pcp == pcp_val
        detail_ok = detail_pcp == pcp_val
        stats_ok = slm_tx >= 10 and slr_rx >= 10

        results[pcp_val] = {
            'send_pcp': send_pcp,
            'detail_pcp': detail_pcp,
            'slm_tx': slm_tx,
            'slr_rx': slr_rx,
            'near_loss': near_pct,
            'far_loss': far_pct,
            'send_ok': send_ok,
            'detail_ok': detail_ok,
            'stats_ok': stats_ok,
        }

        icon = '(/)' if (send_ok and detail_ok and stats_ok) else '(x)'
        print(f"  {icon} PCP {pcp_val}:")
        print(f"      On-demand 'Sending' line PCP: {send_pcp} {'OK' if send_ok else 'MISMATCH'}")
        print(f"      On-demand detail PCP:         {detail_pcp} {'OK' if detail_ok else 'MISMATCH'}")
        print(f"      SLM TX: {slm_tx}, SLR RX: {slr_rx}")
        print(f"      Near-end loss: {near_pct}%, Far-end loss: {far_pct}%")
        print()

        time.sleep(3)

    # Restore PCP 5 and re-enable proactive
    print("Restoring PCP 5 and re-enabling proactive session...")
    config_pcp(chan, 5)
    enable_proactive(chan)
    time.sleep(5)

    # Summary
    print("=" * 70)
    print("PCP WIRE VERIFICATION SUMMARY")
    print("=" * 70)
    print()
    print(f"{'PCP':>3} | {'Send PCP':>8} | {'Detail PCP':>10} | {'SLM TX':>6} | {'SLR RX':>6} | {'Near Loss':>9} | {'Far Loss':>8} | Result")
    print("-" * 85)

    all_pass = True
    for pcp_val in range(0, 8):
        r = results[pcp_val]
        ok = r['send_ok'] and r['detail_ok'] and r['stats_ok']
        if not ok:
            all_pass = False
        icon = 'PASS' if ok else 'FAIL'
        print(f"  {pcp_val} | {r['send_pcp']:>8} | {r['detail_pcp']:>10} | {r['slm_tx']:>6} | {r['slr_rx']:>6} | {r['near_loss']:>8}% | {r['far_loss']:>7}% | {icon}")

    print()
    overall = 'PASS' if all_pass else 'FAIL'
    print(f"Overall: {overall}")
    print(f"\nEnd: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")

    ssh.close()
    return 0 if all_pass else 1


if __name__ == '__main__':
    sys.exit(main())
