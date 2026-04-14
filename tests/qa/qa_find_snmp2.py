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

# Show NCC 0
print('=== NCC 0 ===', flush=True)
print(cmd('show system ncc 0 | no-more', 10), flush=True)

# Show NCC 1
print('\n=== NCC 1 ===', flush=True)
print(cmd('show system ncc 1 | no-more', 10), flush=True)

# Show NCF
print('\n=== NCF ? ===', flush=True)
print(cmd('show system ncf ?', 5), flush=True)

# Show all system nodes
print('\n=== SYSTEM NODES ===', flush=True)
print(cmd('show system nodes | no-more', 10), flush=True)

# Management/SNMP config
print('\n=== SNMP CONFIG FULL ===', flush=True)
print(cmd('show config system snmp | no-more', 8), flush=True)

# Try to find management IP
print('\n=== INTERFACES WITH IPs ===', flush=True)
out = cmd('show interfaces | include inet | no-more', 10)
print(out, flush=True)

# Local test - try to query SNMP from CLI
print('\n=== SNMP LOCAL TEST ===', flush=True)
print(cmd('show system snmp mibs | no-more', 8), flush=True)

ssh.close()
print('\nDONE', flush=True)
