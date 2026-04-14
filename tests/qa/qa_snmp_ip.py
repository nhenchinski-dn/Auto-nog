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

# Check management VRF
print('=== SHOW CONFIG SYSTEM MANAGEMENT ===', flush=True)
print(cmd('show config system management | no-more', 8), flush=True)

# Check VRFs
print('\n=== SHOW VRF ===', flush=True)
print(cmd('show vrf | no-more', 8), flush=True)

# Check management interface/OOB
print('\n=== SHOW SYSTEM MANAGEMENT INTERFACE ===', flush=True)
print(cmd('show system management-interface | no-more', 8), flush=True)

# Check if SNMP responds on management VRF
print('\n=== RE-CONFIG SNMP FOR MANAGEMENT VRF ===', flush=True)
cmd('configure', 3)
print(cmd('system snmp community testacl123 vrf management', 5), flush=True)
print(cmd('commit', 15), flush=True)
cmd('end', 3)

# Show updated SNMP communities
print('\n=== UPDATED SNMP COMMUNITIES ===', flush=True)
out = cmd('show system snmp summary | no-more', 10)
for line in out.split('\n'):
    if 'testacl' in line.lower() or 'Community' in line or '---' in line or '+' in line:
        print(line, flush=True)

ssh.close()
print('\nDONE', flush=True)
