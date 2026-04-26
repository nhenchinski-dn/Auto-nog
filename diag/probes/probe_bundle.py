#!/usr/bin/env python3
import paramiko, time, re
HOST = "WKY1C7VD00008P2"
ANSI = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')
def clean(t):
    return re.sub(r'\r','',ANSI.sub('',t))
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
    "show interfaces bundle-10 | no-more",
    "show interfaces bundle-20 | no-more",
    "show interfaces ge400-0/0/11 | no-more",
    "show interfaces ge400-0/0/12 | no-more",
    "show route 20.0.0.2 | no-more",
    "show route ipv4 | inc 20.0.0 | no-more",
    "show arp | inc 20.0.0 | no-more",
    "show lag brief | no-more",
]:
    print(f"\n### {c} ###")
    print(run(sh,c))
ssh.close()
