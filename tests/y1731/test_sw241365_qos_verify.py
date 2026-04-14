#!/usr/bin/env python3
"""
SW-241365: Re-enable proactive SLM with PCP 3, then capture QoS summary
before and after to see if OAM frames hit the correct queue.
"""

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

def get_qos_summary(chan):
    ensure_op(chan)
    out = run(chan, 'show qos summary | no-more', 15)
    return out

def parse_egress_counters(summary_text):
    """Extract ge100-0/0/70.100 egress counters per rule."""
    counters = {}
    for line in summary_text.splitlines():
        if 'ge100-0/0/70.100' in line and '| out' in line:
            parts = [p.strip() for p in line.split('|')]
            if len(parts) >= 11:
                rule = parts[4]
                tc = parts[5]
                match_pkts = parts[9]
                key = f"{rule} ({tc})" if tc else f"{rule}"
                counters[key] = int(match_pkts)
    return counters

ssh, chan = connect()
print("[OK] Connected\n")

# Step 1: Get baseline QoS summary
print("=== BASELINE QOS SUMMARY (before enabling proactive) ===")
baseline = get_qos_summary(chan)
baseline_counters = parse_egress_counters(baseline)
for k, v in sorted(baseline_counters.items()):
    print(f"  {k}: {v} pkts")
print()

# Step 2: Set PCP=3 and enable proactive SLM
print("Setting PCP=3 and enabling proactive SLM_CLI_TAB_mep1...")
ensure_op(chan)
run(chan, 'configure', 3)
run(chan, 'services performance-monitoring profiles cfm two-way-synthetic-loss-measurement SLM_PROF_CLI pcp 3', 3)
run(chan, 'top', 2)
run(chan, 'services performance-monitoring cfm two-way-synthetic-loss-measurement SLM_CLI_TAB_mep1 admin-state enabled', 3)
run(chan, 'top', 2)
out = run(chan, 'commit', 15)
print(f"  Commit: {'succeeded' if 'Commit succeeded' in out else 'check output'}")
run(chan, 'end', 3)
if 'uncommitted' in out.lower():
    run(chan, 'no', 3)

# Step 3: Verify it's running with PCP 3
time.sleep(5)
ensure_op(chan)
detail = run(chan, 'show services performance-monitoring cfm tests proactive two-way-synthetic-loss session-name SLM_CLI_TAB_mep1 detail | no-more', 12)
pcp_match = re.search(r'PCP:\s*(\d+)', detail)
admin_match = re.search(r'Admin state:\s*(\w+)', detail)
print(f"  Admin state: {admin_match.group(1) if admin_match else '?'}")
print(f"  PCP: {pcp_match.group(1) if pcp_match else '?'}")
print()

# Step 4: Wait 60 seconds for traffic
print("Waiting 60 seconds for proactive SLM traffic with PCP 3...")
time.sleep(60)

# Step 5: Get post-traffic QoS summary
print("=== POST-TRAFFIC QOS SUMMARY ===")
after = get_qos_summary(chan)
after_counters = parse_egress_counters(after)
for k, v in sorted(after_counters.items()):
    print(f"  {k}: {v} pkts")
print()

# Step 6: Show delta
print("=== DELTA (new packets in 60s) ===")
for k in sorted(set(list(baseline_counters.keys()) + list(after_counters.keys()))):
    before = baseline_counters.get(k, 0)
    after_v = after_counters.get(k, 0)
    delta = after_v - before
    if delta != 0:
        print(f"  {k}: +{delta} pkts")
print()

# Also check if SLM PDUs went through the right queue
qos3_before = baseline_counters.get('3 (QOS-TAG-3)', 0)
qos3_after = after_counters.get('3 (QOS-TAG-3)', 0)
default_before = baseline_counters.get('default ()', baseline_counters.get('default', 0))
default_after = after_counters.get('default ()', after_counters.get('default', 0))

print(f"QOS-TAG-3 delta: +{qos3_after - qos3_before} pkts")
print(f"Default queue delta: +{default_after - default_before} pkts")

if qos3_after > qos3_before:
    print("\n>>> OAM frames ARE hitting the correct QoS queue (PCP 3 -> QOS-TAG-3)")
else:
    print("\n>>> OAM frames are NOT hitting QOS-TAG-3 - they bypass ingress QoS classification")
    print(">>> The PCP is stamped in the Ethernet header but the internal qos-tag stays at 0 (default queue)")

ssh.close()
