#!/usr/bin/env python3
"""Try to enable ge400-0/0/3 and check state."""
import paramiko, time, re

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

def run(cmd, wait=6):
    chan.send(cmd + '\n')
    time.sleep(wait)
    out = b''
    while chan.recv_ready():
        out += chan.recv(65535)
        time.sleep(0.3)
    return clean(out.decode(errors='replace'))

def cfg(cmd, wait=4):
    return run(cmd, wait)

print(">>> Breakout parent?")
print(run("show config platform | no-more", 8))
print("\n>>> platform sub-config for ge400-0/0/3")
print(run("show config platform fabric-port ge400-0/0/3 | no-more", 6))
print("\n>>> Try enable ge400-0/0/3")
cfg("configure", 2)
cfg("interfaces ge400-0/0/3 admin-state enabled", 2)
out = cfg("commit", 10)
print(out[-2000:])
cfg("end", 2)
time.sleep(5)
print("\n>>> ge400-0/0/3 status after enable")
print(run("show interfaces ge400-0/0/3 | no-more", 6))

chan.close(); ssh.close()
