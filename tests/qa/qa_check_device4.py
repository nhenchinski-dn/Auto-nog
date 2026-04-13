#!/usr/bin/env python3
import paramiko, time, re, sys
sys.stdout.reconfigure(line_buffering=True)

host = 'XEC1E3VR00008'
user = 'dnroot'
pw = 'dnroot'

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(host, username=user, password=pw, timeout=15, look_for_keys=False, allow_agent=False)

chan = ssh.invoke_shell(width=400, height=1000)
time.sleep(5)
chan.recv(65535)

ANSI_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07|\x1b[()][AB012]|\x1b\[\?[0-9;]*[hlm]|\r')

def cmd(c, wait=5):
    chan.send(c + '\n')
    time.sleep(wait)
    out = b''
    while chan.recv_ready():
        out += chan.recv(65535)
    return ANSI_RE.sub('', out.decode(errors='replace')).strip()

# Find ACL bindings in config
print('=== CONFIG WITH ACL BINDINGS ===', flush=True)
print(cmd('show config | match access-list | no-more', 15), flush=True)

# Check specific NCP
print('\n=== NCP INFO ===', flush=True)
print(cmd('show system ncp 0 | no-more', 8), flush=True)

# Check interfaces 
print('\n=== INTERFACES STATUS ===', flush=True)
out = cmd('show interfaces | no-more', 15)
lines = out.split('\n')
for l in lines[:60]:
    print(l, flush=True)
if len(lines) > 60:
    print(f'... ({len(lines)} total lines)', flush=True)

# Check ACL counters for the eth ACL specifically
print('\n=== ACL COUNTERS ETH ===', flush=True)
print(cmd('show access-lists counters eth cfmblock | no-more', 8), flush=True)

# Check show config for interfaces with ACLs
print('\n=== INTERFACES WITH ACL BINDING ===', flush=True)
print(cmd('show config interfaces | match access | no-more', 15), flush=True)

ssh.close()
print('\nDONE', flush=True)
