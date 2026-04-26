#!/usr/bin/env python3
"""Probe WKY1C7VD00008P2 for interface/port format and current state."""
import paramiko
import time
import re

HOST = "wky1c7vd00008p2"
USER = "dnroot"
PASS = "dnroot"


def clean(text):
    text = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text)
    text = re.sub(r"\r", "", text)
    text = re.sub(r"-- More -- \(Press q to quit\)\s*", "", text)
    return text


def recv_all(shell, timeout=5):
    out = ""
    end = time.time() + timeout
    while time.time() < end:
        time.sleep(0.4)
        while shell.recv_ready():
            out += shell.recv(65536).decode("utf-8", errors="replace")
            end = time.time() + 1.5
    return out


def send(shell, cmd, wait=2):
    shell.send(cmd + "\n")
    time.sleep(wait)
    return recv_all(shell, timeout=4)


print(f"=== Connecting to {HOST} ===", flush=True)
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(HOST, username=USER, password=PASS,
               look_for_keys=False, allow_agent=False, timeout=15)
shell = client.invoke_shell(width=250, height=5000)
time.sleep(6)
_ = recv_all(shell, timeout=3)

for cmd in [
    "show system | no-more",
    "show interfaces | no-more | include ge100-0/0/3",
    "show interfaces detail ge100-0/0/3/0 | no-more",
    "show interfaces detail ge100-0/0/3/1 | no-more",
    "show config interfaces ge100-0/0/3/0 | no-more",
    "show config interfaces ge100-0/0/3/1 | no-more",
    "show config network-services bridge-domain | no-more",
    "show config interfaces | no-more | include irb",
]:
    print(f"\n############ {cmd} ############", flush=True)
    out = send(shell, cmd, wait=3)
    print(clean(out), flush=True)

send(shell, "exit", wait=1)
client.close()
print("\n=== DONE ===", flush=True)
