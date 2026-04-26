#!/usr/bin/env python3
"""Deep debug: why uRPF drops with allow-default enabled for src 10.100.10.100."""

import paramiko, time, re

DUT_IP = '100.64.8.59'

def clean(text):
    text = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)
    text = re.sub(r'\r', '', text)
    text = re.sub(r'-- More -- \(Press q to quit\)\s*', '', text)
    return text.strip()

def run(chan, cmd, wait=10):
    chan.send(cmd + '\n')
    time.sleep(wait)
    out = b''
    while chan.recv_ready():
        out += chan.recv(65535)
        time.sleep(0.5)
    return clean(out.decode(errors='replace'))

def extract_counter(text, label):
    for line in text.split('\n'):
        if label in line:
            nums = re.findall(r'(\d+)', line.split(label)[-1])
            if nums:
                return int(nums[0])
    return 0

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(DUT_IP, username='dnroot', password='dnroot', timeout=30,
            look_for_keys=False, allow_agent=False)
chan = ssh.invoke_shell(width=300, height=5000)
time.sleep(6)
chan.recv(65535)

# 1. Full detail on uRPF operational state
print("=" * 70)
print("1. FULL interfaces detail (uRPF lines)")
print("=" * 70)
out = run(chan, "show interfaces detail ge400-0/0/3.100 | no-more", 10)
for line in out.split('\n'):
    ll = line.lower()
    if any(k in ll for k in ['urpf', 'rpf', 'allow', 'mode', 'admin', 'oper', 'vrf', 'vlan', 'encap']):
        print(line)

# 2. Try toggling: disable allow-default, re-enable, commit
print("\n" + "=" * 70)
print("2. TOGGLE allow-default: disable → commit → re-enable → commit")
print("=" * 70)
run(chan, "configure", 5)
run(chan, "interfaces ge400-0/0/3.100 urpf address-family ipv4 allow-default disabled", 5)
run(chan, "interfaces ge400-0/0/3.100 urpf address-family ipv6 allow-default disabled", 5)
run(chan, "interfaces ge400-0/0/3.100 urpf allow-default disabled", 5)
out = run(chan, "commit", 15)
print(f"  Disable commit: {'OK' if 'succeeded' in out.lower() else out[-200:]}")

time.sleep(2)
run(chan, "interfaces ge400-0/0/3.100 urpf address-family ipv4 allow-default enabled", 5)
run(chan, "interfaces ge400-0/0/3.100 urpf address-family ipv6 allow-default enabled", 5)
run(chan, "interfaces ge400-0/0/3.100 urpf allow-default enabled", 5)
out = run(chan, "commit", 15)
print(f"  Re-enable commit: {'OK' if 'succeeded' in out.lower() else out[-200:]}")
run(chan, "exit", 3)

# 3. Also try adding global admin-state (just to test hardware programming)
print("\n" + "=" * 70)
print("3. ADD global admin-state enabled (test if this changes HW behavior)")
print("=" * 70)
run(chan, "configure", 5)
run(chan, "interfaces ge400-0/0/3.100 urpf admin-state enabled", 5)
out = run(chan, "commit", 15)
print(f"  Commit: {'OK' if 'succeeded' in out.lower() else out[-200:]}")
run(chan, "exit", 3)

# 4. Verify config after toggle
print("\n" + "=" * 70)
print("4. CONFIG after toggle")
print("=" * 70)
out = run(chan, "show config interfaces ge400-0/0/3.100 urpf | no-more", 10)
print(out)

# 5. Counter snapshot before/after 10s
print("\n" + "=" * 70)
print("5. COUNTER DELTA after toggle (10s)")
print("=" * 70)
c1 = run(chan, "show interfaces counters ge400-0/0/3.100 | no-more", 10)
v4_1 = extract_counter(c1, "uRPF Ipv4 drops:")
rx_1 = extract_counter(c1, "RX packets:")
print(f"  T0: RX={rx_1:,}  uRPF_v4={v4_1:,}")

time.sleep(10)

c2 = run(chan, "show interfaces counters ge400-0/0/3.100 | no-more", 10)
v4_2 = extract_counter(c2, "uRPF Ipv4 drops:")
rx_2 = extract_counter(c2, "RX packets:")
print(f"  T1: RX={rx_2:,}  uRPF_v4={v4_2:,}")
print(f"  DELTA: RX={rx_2-rx_1:,}  uRPF_v4={v4_2-v4_1:,}")

if v4_2 - v4_1 > 0:
    print("  >>> STILL DROPPING after toggle + global admin-state <<<")
else:
    print("  >>> FIXED — no more drops <<<")

# 6. Check platform-level uRPF state if available
print("\n" + "=" * 70)
print("6. Platform uRPF check (if available)")
print("=" * 70)
for cmd in [
    "show interfaces ge400-0/0/3.100 urpf | no-more",
    "show urpf interface ge400-0/0/3.100 | no-more",
    "show urpf | no-more",
]:
    out = run(chan, cmd, 8)
    if 'error' not in out.lower() and 'unknown' not in out.lower():
        print(f"  CMD: {cmd}")
        print(out)
        break
    else:
        print(f"  [{cmd}] → not available")

ssh.close()
print("\nDone.")
