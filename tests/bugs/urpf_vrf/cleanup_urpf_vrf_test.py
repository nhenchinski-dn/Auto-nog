#!/usr/bin/env python3
"""Cleanup + final verification: remove bundle-10 urpf, observe VRF, then remove all."""

import paramiko, time, re

DUT = '100.64.8.59'

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

print("\n=== STEP A: remove bundle-10 urpf only ===", flush=True)
C(s, "configure", 3)
C(s, "interfaces", 2)
C(s, "bundle-10", 2)
C(s, "no urpf", 3)
C(s, "top", 2)
C(s, "show config interfaces bundle-10 urpf", 3)
C(s, "show config interfaces bundle-20 urpf", 3)
C(s, "commit", 15)
C(s, "exit", 2)

show_xray(s, "after removing bundle-10 urpf (only bundle-20 remains, IPv4-enabled + allow-default=enabled)")

print("\n=== STEP B: remove bundle-20 urpf (full cleanup) ===", flush=True)
C(s, "configure", 3)
C(s, "interfaces", 2)
C(s, "bundle-20", 2)
C(s, "no urpf", 3)
C(s, "top", 2)
C(s, "show config interfaces bundle-10 urpf", 3)
C(s, "show config interfaces bundle-20 urpf", 3)
C(s, "commit", 15)
C(s, "exit", 2)

show_xray(s, "post full cleanup")

ssh.close()
print("\nDONE", flush=True)
