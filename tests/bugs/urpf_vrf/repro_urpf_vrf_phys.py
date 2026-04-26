#!/usr/bin/env python3
"""Reproduce VRF URPF allow-default bug on PHYSICAL interfaces."""

import paramiko, time, re

DUT = '100.64.8.59'
INTF1 = 'ge100-0/0/3/0'
INTF2 = 'ge100-0/0/3/1'

def clean(t):
    t = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', t)
    return re.sub(r'\r', '', t)

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
        if re.search(r"\[default\]#\s*$", clean(buf)):
            return clean(buf)
    return clean(buf)

def show_xray(s, label):
    print(f"\n=== XRAY: {label} ===", flush=True)
    pre = enter_ncp_shell(s)
    print(f"(in ncp shell) tail: {pre[-200:]}", flush=True)
    C(s, 'xraycli /wb_agent/vrf/active 2>&1 | head -80', 8)
    C(s, "exit", 4)

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(DUT, username='dnroot', password='dnroot', timeout=30,
            look_for_keys=False, allow_agent=False)
s = ssh.invoke_shell(width=250, height=5000)
time.sleep(6); s.recv(65535)

show_xray(s, "baseline")

print("\n=== Apply scenario on physical interfaces ===", flush=True)
C(s, "configure", 3)
C(s, "interfaces", 2)
C(s, INTF1, 2)
C(s, "urpf", 2)
C(s, "admin-state enabled", 2)
C(s, "allow-default disabled", 2)
C(s, "address-family ipv4", 2)
C(s, "admin-state disabled", 2)
C(s, "top", 2)
C(s, "interfaces", 2)
C(s, INTF2, 2)
C(s, "urpf", 2)
C(s, "admin-state disabled", 2)
C(s, "address-family ipv4", 2)
C(s, "admin-state enabled", 2)
C(s, "allow-default enabled", 2)
C(s, "top", 2)
C(s, f"show config interfaces {INTF1} urpf", 3)
C(s, f"show config interfaces {INTF2} urpf", 3)
C(s, "commit", 20)
C(s, "exit", 2)

show_xray(s, f"after commit (phys: {INTF1} global-en/IPv4-dis, {INTF2} global-dis/IPv4-en+ad-en)")

print("\n=== Trigger recalc by toggling intf2 IPv4 allow-default ===", flush=True)
C(s, "configure", 3)
C(s, "interfaces", 2)
C(s, INTF2, 2)
C(s, "urpf", 2)
C(s, "address-family ipv4", 2)
C(s, "allow-default disabled", 2)
C(s, "top", 2)
C(s, "commit", 15)
C(s, "interfaces", 2)
C(s, INTF2, 2)
C(s, "urpf", 2)
C(s, "address-family ipv4", 2)
C(s, "allow-default enabled", 2)
C(s, "top", 2)
C(s, "commit", 15)
C(s, "exit", 2)

show_xray(s, "after recalc trigger")

print("\n=== CLEANUP: remove URPF from both interfaces ===", flush=True)
C(s, "configure", 3)
C(s, "interfaces", 2)
C(s, INTF1, 2)
C(s, "no urpf", 3)
C(s, "top", 2)
C(s, "interfaces", 2)
C(s, INTF2, 2)
C(s, "no urpf", 3)
C(s, "top", 2)
C(s, f"show config interfaces {INTF1} urpf", 3)
C(s, f"show config interfaces {INTF2} urpf", 3)
C(s, "commit", 15)
C(s, "exit", 2)

show_xray(s, "post-cleanup")

ssh.close()
print("\nDONE", flush=True)
