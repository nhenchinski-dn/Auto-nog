#!/usr/bin/env python3
import paramiko, time

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("XGU1F7VC0001AP2", username="dnroot", password="dnroot", timeout=15, allow_agent=False, look_for_keys=False)
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
cmd("configure", 2)

# Remove remaining sub-interface
cmd("no interfaces ge10-0/0/0.400", 2)
result = cmd("commit", 15)
print(f"Cleanup commit: {result}", flush=True)
cmd("exit", 2)

out = cmd("show config interfaces", 5)
print(f"Final config:\n{out}", flush=True)

chan.close()
ssh.close()
print("Cleanup done!", flush=True)
