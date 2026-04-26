#!/usr/bin/env python3
import paramiko, time, re
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("100.64.8.59", username="dnroot", password="dnroot", timeout=30,
            look_for_keys=False, allow_agent=False)
chan = ssh.invoke_shell(width=300, height=5000)
time.sleep(8)
banner = chan.recv(65535).decode(errors="replace")
print("banner:", banner[-200:])

def send(cmd, w=5):
    chan.send(cmd + "\n"); time.sleep(w)
    o = b""
    while chan.recv_ready(): o += chan.recv(65535); time.sleep(0.3)
    out = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", o.decode(errors='replace')).replace("\r","")
    return out

for cmd in [
    "show system | no-more",
    "show network-services vrf | no-more",
    "show interfaces | include ge400-0/0/3 | no-more",
    "show interfaces detail ge400-0/0/3 | no-more",
    "show interfaces detail ge400-0/0/18 | no-more",
    "show config interfaces bundle-10 | no-more",
    "show bgp | no-more",
]:
    print(f"\n===== {cmd} =====")
    print(send(cmd, 6))

chan.close(); ssh.close()
