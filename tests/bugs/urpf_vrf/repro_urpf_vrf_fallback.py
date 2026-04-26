#!/usr/bin/env python3
"""Reproduce VRF URPF allow-default fallback bug on wky1c7vd00008p2."""

import paramiko, time, re

DUT = '100.64.8.59'

def clean(t):
    t = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', t)
    t = re.sub(r'\r', '', t)
    return t

def recv(s, secs=4):
    out = ""
    for _ in range(int(secs)):
        time.sleep(1)
        while s.recv_ready():
            out += s.recv(65535).decode("utf-8", "replace")
    return clean(out)

def C(s, c, secs=3):
    s.send(c + "\n")
    o = recv(s, secs)
    print(f">>> {c}\n{o[-1200:]}", flush=True)
    return o

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(DUT, username='dnroot', password='dnroot', timeout=30,
            look_for_keys=False, allow_agent=False)
s = ssh.invoke_shell(width=250, height=5000)
time.sleep(6); s.recv(65535)

def enter_ncp_shell(s):
    s.send("run start shell ncp 0\n")
    deadline = time.time() + 20
    buf = ""
    while time.time() < deadline:
        time.sleep(1)
        while s.recv_ready():
            buf += s.recv(65535).decode("utf-8", "replace")
        if "assword" in buf:
            s.send("dnroot\n"); break
    deadline = time.time() + 20
    while time.time() < deadline:
        time.sleep(1)
        while s.recv_ready():
            buf += s.recv(65535).decode("utf-8", "replace")
        if re.search(r"\[default\]#\s*$", clean(buf)) or re.search(r"datapath:.*#\s*$", clean(buf)):
            return clean(buf)
    return clean(buf)

def show_xray(s, label):
    print(f"\n=== XRAY: {label} ===", flush=True)
    pre = enter_ncp_shell(s)
    print(f"(entered ncp shell)\n{pre[-300:]}", flush=True)
    C(s, 'xraycli /wb_agent/vrf/active 2>&1 | head -80', 8)
    C(s, "exit", 4)

print("\n=== STEP 1: baseline xray ===", flush=True)
show_xray(s, "baseline (no urpf cfg)")

print("\n=== STEP 2: apply scenario ===", flush=True)
C(s, "configure", 3)
# bundle-10: globally enabled, allow-default disabled, IPv4 disabled
C(s, "interfaces", 2)
C(s, "bundle-10", 2)
C(s, "urpf", 2)
C(s, "admin-state enabled", 2)
C(s, "allow-default disabled", 2)
C(s, "address-family ipv4", 2)
C(s, "admin-state disabled", 2)
C(s, "top", 2)
# bundle-20: globally disabled, IPv4 enabled with allow-default enabled
C(s, "interfaces", 2)
C(s, "bundle-20", 2)
C(s, "urpf", 2)
C(s, "admin-state disabled", 2)
C(s, "address-family ipv4", 2)
C(s, "admin-state enabled", 2)
C(s, "allow-default enabled", 2)
C(s, "top", 2)
C(s, "show config interfaces bundle-10 urpf", 3)
C(s, "show config interfaces bundle-20 urpf", 3)
C(s, "commit", 20)
C(s, "exit", 2)

show_xray(s, "after initial commit")

print("\n=== STEP 3: trigger recalc by toggling bundle-20 IPv4 allow-default ===", flush=True)
C(s, "configure", 3)
C(s, "interfaces", 2)
C(s, "bundle-20", 2)
C(s, "urpf", 2)
C(s, "address-family ipv4", 2)
C(s, "allow-default disabled", 2)
C(s, "top", 2)
C(s, "commit", 15)
C(s, "interfaces", 2)
C(s, "bundle-20", 2)
C(s, "urpf", 2)
C(s, "address-family ipv4", 2)
C(s, "allow-default enabled", 2)
C(s, "top", 2)
C(s, "commit", 15)
C(s, "exit", 2)

show_xray(s, "after recalc trigger (toggle off then on)")

print("\n=== STEP 4: ROLLBACK ===", flush=True)
C(s, "configure", 3)
C(s, "delete interfaces bundle-10 urpf", 3)
C(s, "delete interfaces bundle-20 urpf", 3)
C(s, "commit", 15)
C(s, "show config interfaces bundle-10 urpf", 3)
C(s, "show config interfaces bundle-20 urpf", 3)
C(s, "exit", 2)

show_xray(s, "post-rollback")

ssh.close()
print("\nDONE", flush=True)
