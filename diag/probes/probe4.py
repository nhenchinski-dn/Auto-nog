#!/usr/bin/env python3
import paramiko, time, re
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("100.64.8.59", username="dnroot", password="dnroot", timeout=30,
            look_for_keys=False, allow_agent=False)
chan = ssh.invoke_shell(width=300, height=5000)
time.sleep(8)
chan.recv(65535)

def send(cmd, w=5):
    chan.send(cmd + "\n"); time.sleep(w)
    o = b""
    while chan.recv_ready():
        o += chan.recv(65535); time.sleep(0.3)
    print(f"\n>>> {cmd}")
    print(re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", o.decode(errors='replace')).replace("\r",""))

send("request system ?", 3)
send("show system stack", 6)
send("show system install", 6)
chan.close(); ssh.close()
