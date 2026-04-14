#!/usr/bin/env python3
"""Minimal ACL MIB test - no show commands during config mode."""
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
    cleaned = ANSI_RE.sub('', out.decode(errors='replace')).strip()
    return cleaned

# Cleanup any leftover
cmd('configure', 3)
cmd('rollback', 5)
cmd('end', 3)
time.sleep(2)
# Handle any uncommitted warning
chan.send('no\n')
time.sleep(2)
if chan.recv_ready():
    chan.recv(65535)

# PHASE 1: Configure ACLs and SNMP (no show commands in config mode!)
print('=== PHASE 1: CONFIGURE ===', flush=True)
cmd('configure', 3)
cmd('system snmp community qa-test vrf default', 3)
cmd('top', 2)

# Create IPv4 ACL
cmd('access-lists', 2)
cmd('ipv4 qa-ipv4', 2)
cmd('rule 10 allow', 2)
cmd('exit', 2)
cmd('rule 20 deny', 2)
cmd('exit', 2)
cmd('exit', 2)
cmd('exit', 2)

# Bind IPv4 ACL to interface
cmd('interfaces ge10-0/0/32', 2)
out = cmd('access-list ipv4 qa-ipv4 direction in', 5)
print(f'Bind output: {repr(out[-200:])}', flush=True)
cmd('top', 2)

# Commit (NO show commands before this!)
out = cmd('commit', 25)
has_error = bool(re.search(r'error|failed', out, re.IGNORECASE))
print(f'Commit: {"FAIL" if has_error else "OK"}', flush=True)
if has_error:
    print(out[-1000:], flush=True)
    cmd('rollback', 5)

cmd('end', 3)
time.sleep(2)
# Handle any warning
out_check = cmd('', 1)
if 'uncommitted' in out_check.lower():
    cmd('no', 3)

# PHASE 2: Verify (outside config mode)
if not has_error:
    print('\n=== PHASE 2: VERIFY ===', flush=True)
    
    print('\n--- Interface ge10-0/0/32 config ---', flush=True)
    print(cmd('show config interfaces ge10-0/0/32 | no-more', 10), flush=True)

    print('\n--- Access-lists config ---', flush=True)
    print(cmd('show config access-lists | no-more', 10), flush=True)

    print('\n--- CLI counters ---', flush=True)
    print(cmd('show access-lists counters | no-more', 10), flush=True)

    print('\n--- CLI counters ge10-0/0/32 ---', flush=True)
    print(cmd('show access-lists counters ge10-0/0/32 | no-more', 10), flush=True)

    print('\n--- Waiting 100s for SNMP refresh ---', flush=True)
    time.sleep(100)

    print('\n--- MAPPING TABLE ---', flush=True)
    print(cmd('run system snmp walk "1.3.6.1.4.1.49739.2.17.1.1" | no-more', 30), flush=True)

    print('\n--- IPv4 COUNTER TABLE ---', flush=True)
    print(cmd('run system snmp walk "1.3.6.1.4.1.49739.2.17.1.2" | no-more', 30), flush=True)

    print('\n--- DEFAULT IPv4 COUNTER TABLE ---', flush=True)
    print(cmd('run system snmp walk "1.3.6.1.4.1.49739.2.17.1.5" | no-more', 30), flush=True)

# PHASE 3: Cleanup
print('\n=== PHASE 3: CLEANUP ===', flush=True)
cmd('configure', 3)
cmd('no interfaces ge10-0/0/32 access-list ipv4 qa-ipv4', 3)
cmd('top', 2)
cmd('no access-lists ipv4 qa-ipv4', 3)
cmd('top', 2)
cmd('no system snmp community qa-test', 3)
cmd('top', 2)
out = cmd('commit', 20)
print(f'Cleanup: {"FAIL" if "error" in out.lower() else "OK"}', flush=True)
if 'error' in out.lower():
    print(out[-500:], flush=True)
    cmd('rollback', 5)
cmd('end', 3)
time.sleep(2)
out_check = cmd('', 1)
if 'uncommitted' in out_check.lower():
    cmd('no', 3)

ssh.close()
print('\nDONE', flush=True)
