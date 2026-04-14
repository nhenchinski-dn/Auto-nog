#!/usr/bin/env python3
"""Quick PCP check: run on-demand SLM with PCP 3, 5, 7 to generate
traffic so the remote device can verify QoS classification."""

import paramiko, time, re

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

ssh, chan = connect()
print("[OK] Connected\n")

# Disable proactive first
ensure_op(chan)
run(chan, 'configure', 3)
run(chan, 'services performance-monitoring cfm two-way-synthetic-loss-measurement SLM_CLI_TAB_mep1 admin-state disabled', 3)
run(chan, 'top', 2)
run(chan, 'commit', 15)
run(chan, 'end', 3)
time.sleep(5)

for pcp in [0, 3, 5, 7]:
    print(f"--- On-demand SLM with PCP {pcp} ---")
    ensure_op(chan)
    cmd = f"run ethernet-oam cfm on-demand synthetic-loss-measurement two-way maintenance-domain MD-CUST maintenance-association MA-CUST target mep-id 2 pcp {pcp}"
    out = run(chan, cmd, 30)

    sending = re.search(r'Sending\s+\d+.*PCP:\s*(\d+)', out)
    near = re.search(r'Frame loss near-end\s*:\s*\d+\s*\((\S+)\)', out)
    far = re.search(r'Frame loss far-end\s*:\s*\d+\s*\((\S+)\)', out)
    tx = re.search(r'Transmitted SLM PDUs:\s*(\d+),.*Received SLR PDUs:\s*(\d+)', out)

    print(f"  Sending PCP: {sending.group(1) if sending else '?'}")
    if tx:
        print(f"  SLM TX: {tx.group(1)}, SLR RX: {tx.group(2)}")
    if near and far:
        print(f"  Loss: near={near.group(1)}, far={far.group(1)}")
    print()
    time.sleep(5)

# Re-enable proactive
ensure_op(chan)
run(chan, 'configure', 3)
run(chan, 'services performance-monitoring cfm two-way-synthetic-loss-measurement SLM_CLI_TAB_mep1 admin-state enabled', 3)
run(chan, 'top', 2)
run(chan, 'commit', 15)
run(chan, 'end', 3)

print("Proactive re-enabled. Check QoS summary on remote device now.")
ssh.close()
