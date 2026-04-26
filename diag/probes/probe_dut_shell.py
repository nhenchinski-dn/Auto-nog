#!/usr/bin/env python3
"""Probe what shell we land in and what commands work."""
import paramiko, time, re

HOST = "100.64.8.59"
USER = "dnroot"
PASS = "dnroot"


def clean(t):
    t = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", t)
    t = re.sub(r"\r", "", t)
    return t


ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASS, timeout=30,
            look_for_keys=False, allow_agent=False)
chan = ssh.invoke_shell(width=300, height=5000)
time.sleep(8)
out = chan.recv(65535).decode(errors="replace")
print("=== Initial banner / prompt ===")
print(clean(out))
print("=" * 60)

def send(cmd, wait=4):
    chan.send(cmd + "\n")
    time.sleep(wait)
    o = b""
    while chan.recv_ready():
        o += chan.recv(65535)
        time.sleep(0.3)
    print(f"\n>>> {cmd}")
    print(clean(o.decode(errors="replace")))


send("", 2)
send("?", 3)
send("help", 3)
send("show version | no-more", 5)
send("cli", 4)
send("show version | no-more", 5)

chan.close()
ssh.close()
