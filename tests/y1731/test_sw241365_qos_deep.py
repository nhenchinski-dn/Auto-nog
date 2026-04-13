#!/usr/bin/env python3
"""Check interface QoS config, qos-tagging, and capture ge10/ge100 ingress counters delta."""

import paramiko, time, re, sys

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

# 1. Check interface config for qos-tagging
print("=" * 70)
print("1. Interface config (ge100-0/0/70 sub-if 100)")
print("=" * 70)
ensure_op(chan)
run(chan, 'configure', 3)
out = run(chan, 'show full-configuration interfaces interface ge100-0/0/70 sub-interface 100 | no-more', 10)
print(out)
run(chan, 'end', 3)

print("=" * 70)
print("2. Interface config (ge10-0/0/32 sub-if 100)")
print("=" * 70)
ensure_op(chan)
run(chan, 'configure', 3)
out = run(chan, 'show full-configuration interfaces interface ge10-0/0/32 sub-interface 100 | no-more', 10)
print(out)
run(chan, 'end', 3)

# 3. Get the full QoS summary (both ingress and egress for both interfaces)
print("=" * 70)
print("3. Full QoS summary - BEFORE 30s wait")
print("=" * 70)
ensure_op(chan)
before = run(chan, 'show qos summary | no-more', 15)

# Parse all rows
def parse_all_counters(text):
    counters = {}
    for line in text.splitlines():
        if '|' in line and ('ge10-0/0/32.100' in line or 'ge100-0/0/70.100' in line):
            parts = [p.strip() for p in line.split('|')]
            if len(parts) >= 11:
                iface = parts[1]
                direction = parts[2]
                rule = parts[4]
                tc = parts[5]
                match_pkts = parts[9]
                try:
                    key = f"{iface}|{direction}|{rule}|{tc}"
                    counters[key] = int(match_pkts)
                except:
                    pass
    return counters

before_c = parse_all_counters(before)

# 4. Wait 30s for proactive traffic
print("Waiting 30s for proactive SLM traffic...")
time.sleep(30)

# 5. Get QoS summary after
print("=" * 70)
print("4. Full QoS summary - AFTER 30s wait")
print("=" * 70)
after = run(chan, 'show qos summary | no-more', 15)
after_c = parse_all_counters(after)

# 6. Show deltas
print("=" * 70)
print("5. COUNTER DELTAS (30s window)")
print("=" * 70)
print(f"{'Interface':<22} {'Dir':<5} {'Rule':<9} {'Traffic-class':<15} {'Delta pkts':>12}")
print("-" * 70)

for key in sorted(set(list(before_c.keys()) + list(after_c.keys()))):
    b = before_c.get(key, 0)
    a = after_c.get(key, 0)
    delta = a - b
    if delta != 0:
        parts = key.split('|')
        print(f"{parts[0]:<22} {parts[1]:<5} {parts[2]:<9} {parts[3]:<15} {delta:>12}")

# 7. Check the PM session status
print()
print("=" * 70)
print("6. Current PM session detail")
print("=" * 70)
ensure_op(chan)
detail = run(chan, 'show services performance-monitoring cfm tests proactive two-way-synthetic-loss session-name SLM_CLI_TAB_mep1 detail | no-more', 12)
for line in detail.splitlines():
    if any(k in line.lower() for k in ['pcp', 'admin', 'pdu', 'transmit', 'receiv', 'valid', 'invalid', 'session:', 'interface']):
        print(f"  {line.strip()}")

ssh.close()
