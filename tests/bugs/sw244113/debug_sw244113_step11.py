#!/usr/bin/env python3
"""Debug SW-244113 Step 11: why is uRPF dropping with allow-default enabled?"""

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

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(DUT_IP, username='dnroot', password='dnroot', timeout=30,
            look_for_keys=False, allow_agent=False)
chan = ssh.invoke_shell(width=300, height=5000)
time.sleep(6)
chan.recv(65535)

print("=" * 70)
print("1. uRPF CONFIG on ge400-0/0/3.100")
print("=" * 70)
out = run(chan, "show config interfaces ge400-0/0/3.100 urpf | no-more", 10)
print(out)

print("\n" + "=" * 70)
print("2. uRPF DETAIL on ge400-0/0/3.100")
print("=" * 70)
out = run(chan, "show interfaces detail ge400-0/0/3.100 | no-more", 10)
for line in out.split('\n'):
    if any(k in line.lower() for k in ['urpf', 'rpf', 'allow', 'mode', 'admin', 'oper']):
        print(line)

print("\n" + "=" * 70)
print("3. FULL ROUTE TABLE in urpf-vrf (IPv4)")
print("=" * 70)
out = run(chan, "show route vrf urpf-vrf address-family ipv4-unicast | no-more", 10)
print(out)

print("\n" + "=" * 70)
print("4. DEFAULT ROUTE specifically")
print("=" * 70)
out = run(chan, "show route vrf urpf-vrf address-family ipv4-unicast 0.0.0.0/0 | no-more", 10)
print(out)

print("\n" + "=" * 70)
print("5. SPECIFIC ROUTE 10.100.10.0/24 (should be gone)")
print("=" * 70)
out = run(chan, "show route vrf urpf-vrf address-family ipv4-unicast 10.100.10.0/24 | no-more", 10)
print(out)

print("\n" + "=" * 70)
print("6. COUNTERS on ge400-0/0/3.100")
print("=" * 70)
out = run(chan, "show interfaces counters ge400-0/0/3.100 | no-more", 10)
for line in out.split('\n'):
    if any(k in line.lower() for k in ['urpf', 'rpf', 'drop', 'rx ', 'packets']):
        print(line)

print("\n" + "=" * 70)
print("7. FIB lookup for 10.100.10.100 in urpf-vrf")
print("=" * 70)
out = run(chan, "show route vrf urpf-vrf address-family ipv4-unicast 10.100.10.100 | no-more", 10)
print(out)

print("\n" + "=" * 70)
print("8. FULL uRPF config (all interfaces in VRF)")
print("=" * 70)
out = run(chan, "show config interfaces bundle-10.100 urpf | no-more", 10)
print(out)

print("\n" + "=" * 70)
print("9. Check if ge400-0/0/5 exists and has uRPF")
print("=" * 70)
out = run(chan, "show config interfaces ge400-0/0/5 urpf | no-more", 10)
print(out)

ssh.close()
print("\nDone.")
