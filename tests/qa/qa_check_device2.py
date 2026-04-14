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

# Check interfaces with ACLs bound
print('=== INTERFACES WITH ACLS ===', flush=True)
print(cmd('show access-lists interfaces | no-more', 10), flush=True)

# Check available interfaces
print('\n=== INTERFACES STATUS (first 30) ===', flush=True)
print(cmd('show interfaces terse | no-more', 15), flush=True)

# Check NCP type / platform
print('\n=== PLATFORM INFO ===', flush=True)
print(cmd('show system ncp | no-more', 8), flush=True)

# Check if cfmblock is attached
print('\n=== ACL DETAIL ===', flush=True)
print(cmd('show access-lists detail | no-more', 10), flush=True)

ssh.close()
print('\nDONE', flush=True)
