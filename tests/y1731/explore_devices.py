#!/usr/bin/env python3
"""Explore CFM and PM config on both devices to understand current state."""
import sys, time, re
import paramiko

DEVICES = [
    ("WKY1C7VD00008P2", "dnroot", "dnroot"),
    ("xec1e3vr00008", "dnroot", "dnroot"),
]

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

def run_cmd(channel, cmd, timeout=20):
    channel.send(cmd + "\n")
    out = ""
    end = time.time() + timeout
    last_data = time.time()
    while time.time() < end:
        if channel.recv_ready():
            out += channel.recv(65536).decode(errors="ignore")
            last_data = time.time()
            clean = ANSI.sub("", out).strip()
            if clean.endswith("#") or clean.endswith(">"):
                if time.time() - last_data > 0.5:
                    break
        else:
            if time.time() - last_data > 3:
                break
            time.sleep(0.2)
    return ANSI.sub("", out)

def explore(host, user, password):
    print(f"\n{'='*70}")
    print(f"EXPLORING: {host}")
    print(f"{'='*70}")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, username=user, password=password, timeout=15,
                   banner_timeout=15, auth_timeout=15)
    ch = client.invoke_shell()
    ch.settimeout(20)
    time.sleep(2)
    while ch.recv_ready():
        ch.recv(65536)

    cmds = [
        "show version | no-more",
        "show config services ethernet-oam connectivity-fault-management | no-more",
        "show config services performance-monitoring | no-more",
        "show services performance-monitoring cfm tests proactive | no-more",
        "show services performance-monitoring cfm tests | no-more",
    ]
    for cmd in cmds:
        print(f"\n--- CMD: {cmd} ---")
        out = run_cmd(ch, cmd, timeout=20)
        lines = out.splitlines()
        for l in lines[:60]:
            print(f"  {l.rstrip()}")
        if len(lines) > 60:
            print(f"  ... ({len(lines)} total lines)")

    ch.close()
    client.close()

for host, user, pw in DEVICES:
    try:
        explore(host, user, pw)
    except Exception as e:
        print(f"\n[ERROR] {host}: {e}")
