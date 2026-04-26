#!/usr/bin/env python3
"""Fix: enable allow-default on ALL interfaces in urpf-vrf for consistency."""

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

# 1. Discard any uncommitted changes from previous script
print("1. Discarding uncommitted changes...")
run(chan, "no", 3)
run(chan, "configure", 5)
run(chan, "abort", 5)
run(chan, "configure", 5)

# 2. Enable allow-default on bundle-10.100 to match ge400-0/0/3.100
print("2. Enabling allow-default on bundle-10.100...")
cmds = [
    "interfaces bundle-10.100 urpf allow-default enabled",
    "top",
    "interfaces bundle-10.100 urpf address-family ipv4 allow-default enabled",
    "top",
    "interfaces bundle-10.100 urpf address-family ipv6 allow-default enabled",
    "top",
    # Also ensure global admin-state on ge400-0/0/3.100
    "interfaces ge400-0/0/3.100 urpf admin-state enabled",
    "top",
]
for cmd in cmds:
    run(chan, cmd, 3)

out = run(chan, "commit", 15)
print(f"  Commit: {out[-300:]}")

if 'succeeded' in out.lower():
    print("  COMMIT SUCCEEDED")
else:
    print("  COMMIT MAY HAVE ISSUES — checking...")

run(chan, "exit", 3)

# 3. Verify config on both interfaces
print("\n3. Verify config:")
print("--- ge400-0/0/3.100 ---")
out = run(chan, "show config interfaces ge400-0/0/3.100 urpf | no-more", 10)
print(out)

print("\n--- bundle-10.100 ---")
out = run(chan, "show config interfaces bundle-10.100 urpf | no-more", 10)
print(out)

# 4. Counter delta (10s) — is the user still sending traffic?
print("\n4. Counter delta (10s)...")
c1 = run(chan, "show interfaces counters ge400-0/0/3.100 | no-more", 10)
v4_1 = extract_counter(c1, "uRPF Ipv4 drops:")
rx_1 = extract_counter(c1, "RX packets:")
print(f"  T0: RX={rx_1:,}  uRPF_v4={v4_1:,}")

time.sleep(10)

c2 = run(chan, "show interfaces counters ge400-0/0/3.100 | no-more", 10)
v4_2 = extract_counter(c2, "uRPF Ipv4 drops:")
rx_2 = extract_counter(c2, "RX packets:")
print(f"  T1: RX={rx_2:,}  uRPF_v4={v4_2:,}")

rx_d = rx_2 - rx_1
drops_d = v4_2 - v4_1
print(f"  DELTA: RX={rx_d:,}  uRPF_v4={drops_d:,}")

if rx_d == 0:
    print("  No traffic arriving — please start sending and re-check")
elif drops_d == 0:
    print("  FIXED — traffic arriving, zero uRPF drops!")
else:
    print(f"  STILL DROPPING — {drops_d:,} drops out of {rx_d:,} RX")

ssh.close()
print("\nDone.")
