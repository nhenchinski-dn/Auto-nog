#!/usr/bin/env python3
"""Investigate ACL binding and counter table population."""
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

def ensure_op():
    cmd('end', 3)
    out = cmd('', 2)
    if 'uncommitted' in out.lower():
        cmd('no', 3)

# Step 1: Configure SNMP community
print('=== SETUP SNMP ===', flush=True)
cmd('configure', 3)
cmd('system snmp community qa-test vrf default', 3)
out = cmd('commit', 15)
print(f'SNMP commit: {"FAIL" if "error" in out.lower() else "OK"}', flush=True)
cmd('end', 3)
out = cmd('', 2)
if 'uncommitted' in out.lower():
    cmd('no', 3)

# Step 2: Create IPv4 ACL using hierarchical mode (step by step)
print('\n=== CREATE IPv4 ACL (hierarchical) ===', flush=True)
cmd('configure', 3)
cmd('access-lists', 2)
cmd('ipv4 qa-test-v4', 2)
cmd('rule 10 allow', 2)
print(cmd('protocol tcp', 3), flush=True)
cmd('exit', 2)  # back to ACL context
cmd('rule 20 deny', 2)
cmd('exit', 2)  # back to ACL context
cmd('exit', 2)  # back to access-lists
cmd('exit', 2)  # back to top

# Step 3: Verify ACL syntax for binding
print('\n=== CHECK BIND SYNTAX ===', flush=True)
cmd('interfaces ge10-0/0/32', 2)
print(cmd('access-list ?', 5), flush=True)
print(cmd('access-list ipv4 ?', 5), flush=True)
print(cmd('access-list ipv4 qa-test-v4 ?', 5), flush=True)
print(cmd('access-list ipv4 qa-test-v4 direction ?', 5), flush=True)
out = cmd('access-list ipv4 qa-test-v4 direction ingress', 5)
print(f'Bind result: {out}', flush=True)
cmd('top', 2)

# Step 4: Show candidate config for interfaces
print('\n=== CANDIDATE CONFIG (interfaces ge10-0/0/32) ===', flush=True)
print(cmd('show config interfaces ge10-0/0/32 | no-more', 10), flush=True)

# Step 5: Commit
print('\n=== COMMIT ===', flush=True)
out = cmd('commit', 20)
has_error = bool(re.search(r'error|failed', out, re.IGNORECASE))
print(f'Commit: {"FAIL - " + out[-500:] if has_error else "OK"}', flush=True)
if has_error:
    cmd('rollback', 5)
cmd('end', 3)
out = cmd('', 2)
if 'uncommitted' in out.lower():
    cmd('no', 3)

if not has_error:
    # Step 6: Verify binding is in committed config
    print('\n=== COMMITTED INTERFACE CONFIG ===', flush=True)
    print(cmd('show config interfaces ge10-0/0/32 | no-more', 10), flush=True)

    # Step 7: Show access-lists counters
    print('\n=== CLI COUNTERS ===', flush=True)
    print(cmd('show access-lists counters | no-more', 10), flush=True)
    print(cmd('show access-lists counters ge10-0/0/32 | no-more', 10), flush=True)

    # Step 8: Show access-lists (should show binding info)
    print('\n=== SHOW ACCESS-LISTS IPv4 ===', flush=True)
    print(cmd('show access-lists ipv4 | no-more', 10), flush=True)

    # Step 9: Wait for SNMP refresh and walk
    print('\n--- Waiting 100s for SNMP agent refresh ---', flush=True)
    time.sleep(100)

    # Walk entire ACL MIB
    print('\n=== FULL ACL MIB WALK ===', flush=True)
    ensure_op()
    print(cmd('run system snmp walk "1.3.6.1.4.1.49739.2.17" | no-more', 30), flush=True)

# CLEANUP
print('\n=== CLEANUP ===', flush=True)
cmd('configure', 3)
cmd('interfaces ge10-0/0/32', 2)
cmd('no access-list ipv4 qa-test-v4', 3)
cmd('top', 2)
cmd('no access-lists ipv4 qa-test-v4', 3)
cmd('no system snmp community qa-test', 3)
out = cmd('commit', 20)
print(f'Cleanup: {"FAIL" if "error" in out.lower() else "OK"}', flush=True)
if 'error' in out.lower():
    print(out[-500:], flush=True)
    cmd('rollback', 5)
cmd('end', 3)
out = cmd('', 2)
if 'uncommitted' in out.lower():
    cmd('no', 3)

ssh.close()
print('\nDONE', flush=True)
