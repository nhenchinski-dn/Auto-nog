#!/usr/bin/env python3
"""Clean ACL binding test - no ? commands to avoid session disruption."""
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

# First cleanup any leftover state
print('=== CLEANUP LEFTOVER ===', flush=True)
cmd('configure', 3)
cmd('rollback', 5)
cmd('end', 3)
out = cmd('', 2)
if 'uncommitted' in out.lower():
    cmd('no', 3)

# Now do everything cleanly in a single configure session
print('\n=== CONFIGURE ALL ===', flush=True)
cmd('configure', 3)

# 1. Create SNMP community
cmd('system snmp community qa-test vrf default', 3)

# 2. Create IPv4 ACL with rules (correct syntax - match inside rule context)
cmd('access-lists ipv4 qa-ipv4 rule 10 allow', 3)
cmd('top', 2)
cmd('access-lists ipv4 qa-ipv4 rule 20 deny', 3)
cmd('top', 2)

# 3. Create IPv6 ACL
cmd('access-lists ipv6 qa-ipv6 rule 10 allow', 3)
cmd('top', 2)

# 4. Attach IPv4 ACL to ge10-0/0/32 ingress
cmd('interfaces ge10-0/0/32 access-list ipv4 qa-ipv4 direction ingress', 5)
cmd('top', 2)

# 5. Attach IPv6 ACL to ge10-0/0/33 ingress (different interface)
cmd('interfaces ge10-0/0/33 access-list ipv6 qa-ipv6 direction ingress', 5)
cmd('top', 2)

# Show candidate before commit
print('\n=== CANDIDATE - ACCESS-LISTS ===', flush=True)
print(cmd('show config access-lists | no-more', 10), flush=True)

print('\n=== CANDIDATE - INTERFACE ge10-0/0/32 ===', flush=True)
print(cmd('show config interfaces ge10-0/0/32 | no-more', 10), flush=True)

print('\n=== CANDIDATE - INTERFACE ge10-0/0/33 ===', flush=True)
print(cmd('show config interfaces ge10-0/0/33 | no-more', 10), flush=True)

# Commit
print('\n=== COMMIT ===', flush=True)
out = cmd('commit', 20)
has_error = bool(re.search(r'error|failed', out, re.IGNORECASE))
print(f'Commit: {"FAIL" if has_error else "OK"}', flush=True)
if has_error:
    print(out[-1000:], flush=True)
    cmd('rollback', 5)
cmd('end', 3)
out = cmd('', 2)
if 'uncommitted' in out.lower():
    cmd('no', 3)

if not has_error:
    # Verify committed config
    print('\n=== COMMITTED INTERFACE ge10-0/0/32 ===', flush=True)
    print(cmd('show config interfaces ge10-0/0/32 | no-more', 10), flush=True)

    print('\n=== COMMITTED INTERFACE ge10-0/0/33 ===', flush=True)
    print(cmd('show config interfaces ge10-0/0/33 | no-more', 10), flush=True)

    # Check CLI counters
    print('\n=== CLI: show access-lists ===', flush=True)
    print(cmd('show access-lists | no-more', 10), flush=True)

    print('\n=== CLI: show access-lists counters ===', flush=True)
    print(cmd('show access-lists counters | no-more', 10), flush=True)

    print('\n=== CLI: show access-lists counters ge10-0/0/32 ===', flush=True)
    print(cmd('show access-lists counters ge10-0/0/32 | no-more', 10), flush=True)

    # Wait for SNMP agent refresh
    print('\n--- Waiting 100s for SNMP agent refresh ---', flush=True)
    time.sleep(100)

    # Walk ACL MIB
    print('\n=== FULL ACL MIB WALK ===', flush=True)
    print(cmd('run system snmp walk "1.3.6.1.4.1.49739.2.17" | no-more', 60), flush=True)

# CLEANUP
print('\n=== CLEANUP ===', flush=True)
cmd('configure', 3)
cmd('no interfaces ge10-0/0/32 access-list ipv4 qa-ipv4', 3)
cmd('top', 2)
cmd('no interfaces ge10-0/0/33 access-list ipv6 qa-ipv6', 3)
cmd('top', 2)
cmd('no access-lists ipv4 qa-ipv4', 3)
cmd('top', 2)
cmd('no access-lists ipv6 qa-ipv6', 3)
cmd('top', 2)
cmd('no system snmp community qa-test', 3)
cmd('top', 2)
out = cmd('commit', 20)
print(f'Cleanup commit: {"FAIL" if "error" in out.lower() else "OK"}', flush=True)
if 'error' in out.lower():
    print(out[-500:], flush=True)
    cmd('rollback', 5)
cmd('end', 3)
out = cmd('', 2)
if 'uncommitted' in out.lower():
    cmd('no', 3)

ssh.close()
print('\nDONE', flush=True)
