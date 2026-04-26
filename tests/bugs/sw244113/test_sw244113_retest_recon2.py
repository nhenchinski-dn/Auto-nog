#!/usr/bin/env python3
"""SW-244113 RETEST recon #2 - CLI probe and interface inventory."""
import paramiko, time, re, json, os

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
    ("ns_vrf_list",         "show network-services vrf | no-more", 10),
    ("ns_vrf_cfg_full",     "show config network-services | no-more", 10),
    ("all_ifaces",          "show interfaces | no-more", 20),
    ("ge_0_0_3",            "show interfaces ge400-0/0/3 | no-more", 6),
    ("ge_0_0_11",           "show interfaces ge400-0/0/11 | no-more", 6),
    ("bundle10_cfg",        "show config interfaces bundle-10 | no-more", 6),
    ("static_cfg",          "show config protocols static | no-more", 6),
    ("urpf_help",           "show config interfaces bundle-10.100 urpf ?", 4),
]

res = {}
for label, cmd, w in cmds:
    print(f"\n>>> {label}: {cmd}")
    out = run(cmd, w)
    res[label] = out
    print(out[-5000:] if len(out) > 5000 else out)

chan.close(); ssh.close()
os.makedirs("/home/dn/output", exist_ok=True)
with open("/home/dn/output/sw244113_retest_recon2.json", "w") as f:
    json.dump(res, f, indent=2)
print("\nSaved.")
