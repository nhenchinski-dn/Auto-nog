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

# Check show access-lists ?
print('=== SHOW ACCESS-LISTS ? ===', flush=True)
print(cmd('show access-lists ?', 5), flush=True)

# Check show interfaces summary
print('\n=== INTERFACES SUMMARY ===', flush=True)
out = cmd('show interfaces summary | no-more', 15)
lines = out.split('\n')
print('\n'.join(lines[:50]), flush=True)
if len(lines) > 50:
    print(f'... ({len(lines)} lines total)', flush=True)

# Check NCP info
print('\n=== SYSTEM NCP ? ===', flush=True)
print(cmd('show system ncp ?', 5), flush=True)

print('\n=== SHOW SYSTEM NCP NCF ===', flush=True)
print(cmd('show system ncp summary | no-more', 10), flush=True)

# Check what ACLs are available/show
print('\n=== SHOW ACCESS-LISTS eth cfmblock ===', flush=True)
print(cmd('show access-lists eth cfmblock | no-more', 8), flush=True)

# Check running config for interface ACL bindings
print('\n=== CONFIG WITH ACL BINDINGS ===', flush=True)
print(cmd('show running-config | match access-list | no-more', 10), flush=True)

ssh.close()
print('\nDONE', flush=True)
