#!/usr/bin/env python3
import paramiko, time, sys

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("XGU1F7VC0001AP2", username="dnroot", password="dnroot", timeout=15, allow_agent=False, look_for_keys=False)
print("Connected", flush=True)
chan = ssh.invoke_shell(width=250, height=50)
time.sleep(4)
while chan.recv_ready(): chan.recv(65536)

def cmd(c, w=5):
    chan.send(c + "\n")
    time.sleep(w)
    d = b""
    while chan.recv_ready(): d += chan.recv(65536)
    return d.decode(errors='replace')

cmd("set cli screen-length 0", 2)

out = cmd("show interfaces ge10-0/0/0.100", 8)
print("=== ge10-0/0/0.100 ===", flush=True)
print(out, flush=True)

out = cmd("show interfaces ge10-0/0/0.300", 8)
print("=== ge10-0/0/0.300 ===", flush=True)
print(out, flush=True)

chan.close()
ssh.close()
