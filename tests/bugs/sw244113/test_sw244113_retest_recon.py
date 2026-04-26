#!/usr/bin/env python3
"""SW-244113 RETEST: Quick recon of current device state."""
import paramiko, time, re, json, sys

HOST = "100.64.8.59"
USER = "dnroot"
PASS = "dnroot"

def clean(t):
    t = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', t)
    t = re.sub(r'\r', '', t)
    t = re.sub(r'-- More -- \(Press q to quit\)\s*', '', t)
    return t.strip()

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASS, timeout=30,
            look_for_keys=False, allow_agent=False)
chan = ssh.invoke_shell(width=300, height=5000)
time.sleep(6)
chan.recv(65535)

def run(cmd, wait=8):
    chan.send(cmd + '\n')
    time.sleep(wait)
    out = b''
    while chan.recv_ready():
        out += chan.recv(65535)
        time.sleep(0.4)
    return clean(out.decode(errors='replace'))

cmds = [
    ("version", "show system version | no-more", 6),
    ("vrfs", "show vrfs | no-more", 6),
    ("vrf_urpf_cfg", "show config network-services vrf instance urpf-vrf | no-more", 6),
    ("ge_sub_cfg", "show config interfaces ge400-0/0/3.100 | no-more", 6),
    ("bundle_sub_cfg", "show config interfaces bundle-10.100 | no-more", 6),
    ("ge_urpf_cfg", "show config interfaces ge400-0/0/3.100 urpf | no-more", 6),
    ("bundle_urpf_cfg", "show config interfaces bundle-10.100 urpf | no-more", 6),
    ("ge_detail", "show interfaces detail ge400-0/0/3.100 | no-more", 8),
    ("bundle_detail", "show interfaces detail bundle-10.100 | no-more", 8),
    ("route_vrf_v4", "show route vrf urpf-vrf table ipv4-unicast | no-more", 8),
    ("route_vrf_v6", "show route vrf urpf-vrf table ipv6-unicast | no-more", 8),
    ("ge_counters", "show interfaces counters ge400-0/0/3.100 | no-more", 6),
    ("bundle_counters", "show interfaces counters bundle-10.100 | no-more", 6),
    ("bundle10_members", "show interfaces bundle-10 | no-more", 6),
]

results = {}
for label, cmd, w in cmds:
    print(f"\n>>> {label}: {cmd}")
    out = run(cmd, w)
    print(out[-4000:] if len(out) > 4000 else out)
    results[label] = out

chan.close()
ssh.close()

import os
os.makedirs("/home/dn/output", exist_ok=True)
with open("/home/dn/output/sw244113_retest_recon.json", "w") as f:
    json.dump(results, f, indent=2)
print("\nSaved recon.")
