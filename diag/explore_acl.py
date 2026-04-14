#!/usr/bin/env python3
import paramiko, time, sys, re
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

cmd('configure', 3)
cmd('access-lists', 2)

print('=== eth ? ===', flush=True)
out = cmd('eth ?', 5)
print(out[-1000:], flush=True)

print('\n=== eth DROP_CFM ? ===', flush=True)
out = cmd('eth DROP_CFM ?', 5)
print(out[-1000:], flush=True)

print('\n=== rule ? ===', flush=True)
out = cmd('rule ?', 5)
print(out[-1000:], flush=True)

print('\n=== rule 10 ? ===', flush=True)
out = cmd('rule 10 ?', 5)
print(out[-1000:], flush=True)

print('\n=== rule 10 match ? ===', flush=True)
out = cmd('rule 10 match ?', 5)
print(out[-1000:], flush=True)

print('\n=== rule 10 match ether-type ? ===', flush=True)
out = cmd('rule 10 match ether-type ?', 5)
print(out[-1000:], flush=True)

cmd('top', 2)
cmd('end', 3)
ssh.close()
print('DONE', flush=True)
