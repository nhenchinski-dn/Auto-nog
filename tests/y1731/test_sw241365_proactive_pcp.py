#!/usr/bin/env python3
"""
SW-241365: Verify proactive SLM PCP values reach the remote device.
Cycles through PCP 0-7 on the proactive session, waiting 30s at each
to generate enough traffic for the remote QoS summary to show.
"""

import paramiko, time, re, sys
from datetime import datetime

DEVICE_IP = "100.64.4.93"

def connect():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(DEVICE_IP, username='dnroot', password='dnroot',
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
    return re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)

def ensure_op(chan):
    run(chan, '', 2)
    check = run(chan, '', 2)
    if '(cfg' in check:
        run(chan, 'top', 2)
        r = run(chan, 'end', 3)
        if 'uncommitted' in r.lower():
            run(chan, 'no', 5)

def set_pcp(chan, pcp_val):
    ensure_op(chan)
    run(chan, 'configure', 3)
    run(chan, f'services performance-monitoring profiles cfm two-way-synthetic-loss-measurement SLM_PROF_CLI pcp {pcp_val}', 3)
    run(chan, 'top', 2)
    out = run(chan, 'commit', 15)
    run(chan, 'end', 3)
    if 'uncommitted' in out.lower():
        run(chan, 'no', 3)
    return 'Commit succeeded' in out

def get_slm_detail(chan, session):
    ensure_op(chan)
    out = run(chan, f'show services performance-monitoring cfm tests proactive two-way-synthetic-loss session-name {session} detail | no-more', 12)
    pcp_match = re.search(r'PCP:\s*(\d+)', out)
    valid_count = len(re.findall(r'\|\s*valid\s*\|', out))
    tx_match = re.search(r'SLM PDUs transmitted:\s*(\d+)', out)
    rx_match = re.search(r'SLR PDUs received:\s*(\d+)', out)
    return {
        'pcp': int(pcp_match.group(1)) if pcp_match else None,
        'valid_tests': valid_count,
        'slm_tx': int(tx_match.group(1)) if tx_match else 0,
        'slr_rx': int(rx_match.group(1)) if rx_match else 0,
    }

def main():
    print("=" * 65)
    print("SW-241365: Proactive SLM PCP Wire Verification")
    print(f"Device: {DEVICE_IP} (ncpl-cfm-nog)")
    print(f"Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print("=" * 65)
    print()
    print("Will cycle PCP 0-7 on proactive SLM_CLI_TAB_mep1,")
    print("waiting 30s at each PCP for traffic to flow to the remote device.")
    print()

    ssh, chan = connect()
    print("[OK] Connected\n")

    results = {}

    for pcp_val in range(0, 8):
        print(f"--- PCP {pcp_val} ---")
        ok = set_pcp(chan, pcp_val)
        print(f"  Config committed: {'yes' if ok else 'check'}")

        # Wait for 3 full proactive test cycles (repeat-interval=10s, duration=5s)
        print(f"  Waiting 30s for proactive traffic with PCP {pcp_val}...")
        time.sleep(30)

        d = get_slm_detail(chan, 'SLM_CLI_TAB_mep1')
        results[pcp_val] = d
        pcp_ok = d['pcp'] == pcp_val

        icon = '(/)' if pcp_ok else '(x)'
        print(f"  {icon} Detail PCP: {d['pcp']} (expected {pcp_val})")
        print(f"      Valid tests in history: {d['valid_tests']}")
        print(f"      Latest: SLM TX={d['slm_tx']}, SLR RX={d['slr_rx']}")
        print()

    # Restore PCP 5
    print("Restoring PCP to 5...")
    set_pcp(chan, 5)
    time.sleep(5)

    print("=" * 65)
    print("SUMMARY")
    print("=" * 65)
    all_pass = True
    for pcp_val in range(0, 8):
        r = results[pcp_val]
        ok = r['pcp'] == pcp_val
        if not ok:
            all_pass = False
        icon = '(/)' if ok else '(x)'
        print(f"  {icon} PCP {pcp_val}: detail={r['pcp']}, valid_tests={r['valid_tests']}, tx={r['slm_tx']}, rx={r['slr_rx']}")

    overall = 'PASS' if all_pass else 'FAIL'
    print(f"\nOverall: {overall}")
    print(f"End: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print("\n>>> Check the remote device QoS summary now - you should")
    print(">>> have seen traffic in each PCP queue (0-7) over this test.")

    ssh.close()
    return 0 if all_pass else 1

if __name__ == '__main__':
    sys.exit(main())
