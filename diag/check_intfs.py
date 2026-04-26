#!/usr/bin/env python3
"""Quick check of available interfaces on the device."""
import paramiko, time, re

HOST = "WKY1C7VD00008P2"

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(HOST, username='dnroot', password='dnroot',
               look_for_keys=False, allow_agent=False, timeout=15)
shell = client.invoke_shell(width=250, height=5000)
time.sleep(6)
while shell.recv_ready():
    shell.recv(65535)

def send(cmd, wait=8):
    shell.send(cmd + "\n")
    time.sleep(wait)
    out = ""
    while shell.recv_ready():
        out += shell.recv(65535).decode('utf-8', errors='replace')
        time.sleep(0.5)
    out = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', out)
    out = re.sub(r'\r', '', out)
    out = re.sub(r'-- More -- \(Press q to quit\)\s*', '', out)
    return out

print("=== show interfaces brief ===")
print(send("show interfaces brief | no-more", wait=10))

print("\n=== show system ===")
print(send("show system | no-more", wait=8))

client.close()
