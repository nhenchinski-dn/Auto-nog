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
run(sh,"set cli-no-confirm",w=2)
run(sh,"configure",w=3)
# Go into the interface node
print(run(sh,"protocols bfd interface bundle-10",w=3))
print("=== Attributes inside bfd interface ===")
print(run(sh,"?",w=3))
print("=== Try neighbor 20.0.0.2 ===")
print(run(sh,"neighbor 20.0.0.2",w=3))
print(run(sh,"?",w=3))
print("=== Try admin-state ===")
print(run(sh,"admin-state ?",w=3))
print(run(sh,"top",w=2))
print("=== BGP neighbor bfd ===")
print(run(sh,"protocols bgp 65001 neighbor 20.0.0.2 bfd ?",w=3))
print(run(sh,"rollback",w=3))
ssh.close()
