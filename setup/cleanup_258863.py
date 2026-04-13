#!/usr/bin/env python3
import paramiko, time, re, sys

ANSI_RE = re.compile(r'\x1b\[[0-9;]*[A-Za-z]|\x1b\].*?\x07|\x1b[()][A-B012]|\x0f')

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('100.64.6.73', username='dnroot', password='dnroot',
            timeout=30, look_for_keys=False, allow_agent=False)
chan = ssh.invoke_shell(width=300)
time.sleep(5)
chan.recv(65535)

def run(cmd, wait=8):
    chan.send(cmd + '\n')
    time.sleep(wait)
    out = b''
    while chan.recv_ready():
        out += chan.recv(65535)
    return ANSI_RE.sub('', out.decode(errors='replace'))

print('Connected', flush=True)
run('configure')
run('interfaces ge400-0/0/26 admin-state enabled')
run('protocols lldp interface ge400-0/0/26')
out = run('commit', wait=15)
ok = 'Commit succeeded' in out
print(f'Restore: {"OK" if ok else "FAILED"}', flush=True)
for line in out.splitlines():
    s = line.strip()
    if any(kw in s.lower() for kw in ['commit', 'error', 'notice']):
        print(f'  {s}', flush=True)
run('exit')

out = run('show config interfaces ge400-0/0/26 | no-more')
print('Final config:', flush=True)
for line in out.splitlines():
    s = line.strip()
    if s and 'NCP3' not in s and 'show ' not in s:
        print(f'  {s}', flush=True)

out = run('show interfaces breakout | no-more')
for line in out.splitlines():
    if '26' in line:
        print(f'Breakout: {line.strip()}', flush=True)

ssh.close()
print('Done.', flush=True)
