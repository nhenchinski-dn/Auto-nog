#!/usr/bin/env python3
"""Debug Step 11 part 2: check routes and live counter delta."""

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

print("=" * 70)
print("1. ROUTE TABLE in urpf-vrf")
print("=" * 70)
out = run(chan, "show route vrf urpf-vrf | no-more", 10)
print(out)

print("\n" + "=" * 70)
print("2. Longest-match lookup for 10.100.10.100 in urpf-vrf")
print("=" * 70)
out = run(chan, "show route vrf urpf-vrf 10.100.10.100 | no-more", 10)
print(out)

print("\n" + "=" * 70)
print("3. Default route detail")
print("=" * 70)
out = run(chan, "show route vrf urpf-vrf 0.0.0.0/0 | no-more", 10)
print(out)

print("\n" + "=" * 70)
print("4. LIVE COUNTER DELTA (10s window)")
print("=" * 70)
c1 = run(chan, "show interfaces counters ge400-0/0/3.100 | no-more", 10)
v4_drops_1 = extract_counter(c1, "uRPF Ipv4 drops:")
rx_1 = extract_counter(c1, "RX packets:")
print(f"  T0: RX={rx_1:,}  uRPF_v4_drops={v4_drops_1:,}")

print("  Waiting 10 seconds...")
time.sleep(10)

c2 = run(chan, "show interfaces counters ge400-0/0/3.100 | no-more", 10)
v4_drops_2 = extract_counter(c2, "uRPF Ipv4 drops:")
rx_2 = extract_counter(c2, "RX packets:")
print(f"  T1: RX={rx_2:,}  uRPF_v4_drops={v4_drops_2:,}")

rx_delta = rx_2 - rx_1
drops_delta = v4_drops_2 - v4_drops_1
print(f"\n  DELTA: RX={rx_delta:,}  uRPF_v4_drops={drops_delta:,}")
if drops_delta > 0:
    print("  >>> DROPS ARE ACTIVELY HAPPENING <<<")
else:
    print("  >>> No new drops in last 10s (may be stale counters from earlier) <<<")

print("\n" + "=" * 70)
print("5. VRF config (static routes)")
print("=" * 70)
out = run(chan, "show config network-services vrf instance urpf-vrf protocols static | no-more", 15)
print(out)

ssh.close()
print("\nDone.")
