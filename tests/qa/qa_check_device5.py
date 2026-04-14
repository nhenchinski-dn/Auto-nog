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

# Try pipe include syntax
print('=== CONFIG INTERFACES WITH ACL ===', flush=True)
print(cmd('show config interfaces | include access | no-more', 15), flush=True)

# Check acl counters syntax
print('\n=== ACL COUNTERS ? ===', flush=True)
print(cmd('show access-lists counters ?', 5), flush=True)

# Check specific access-lists counters
print('\n=== ALL ACL COUNTERS ===', flush=True)
print(cmd('show access-lists counters | no-more', 10), flush=True)

# Now configure SNMP community and test SNMP walk
print('\n=== CONFIGURING SNMP COMMUNITY ===', flush=True)
cmd('configure', 3)
print(cmd('system snmp community testacl123 vrf default', 5), flush=True)
print(cmd('commit', 15), flush=True)
cmd('end', 3)

# Verify community was added
print('\n=== VERIFY SNMP COMMUNITY ===', flush=True)
print(cmd('show system snmp summary | no-more', 10), flush=True)

ssh.close()
print('\nDONE', flush=True)
