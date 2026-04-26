#!/usr/bin/env python3
import paramiko, time, re
HOST = "WKY1C7VD00008P2"
ANSI = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')
def clean(t): return re.sub(r'\r','',ANSI.sub('',t))
def run(s,c,w=3):
    s.send(c+"\n"); time.sleep(w)
    out=b""; end=time.time()+5
    while time.time()<end:
        time.sleep(0.3)
        while s.recv_ready():
            out+=s.recv(65536); end=time.time()+1
    return clean(out.decode(errors='replace'))

ssh=paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST,username='dnroot',password='dnroot',look_for_keys=False,allow_agent=False,timeout=20)
sh=ssh.invoke_shell(width=250,height=5000); time.sleep(6); sh.recv(65535)
for c in [
    "show config network-services | no-more",
    "show route vrf test | no-more",
    "show arp vrf test | no-more",
    "show config interfaces bundle-10 | no-more",
    "show config interfaces bundle-20 | no-more",
    "show config interfaces ge100-0/0/3/0 | no-more",
]:
    print(f"\n### {c} ###")
    print(run(sh,c))
ssh.close()
