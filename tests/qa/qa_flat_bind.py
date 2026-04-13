#!/usr/bin/env python3
"""Test ACL binding with completely flat config commands, no nav."""
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
    if 'uncommitted' in cleaned.lower():
        chan.send('no\n')
        time.sleep(3)
        if chan.recv_ready():
            chan.recv(65535)
        print(f'  [!] Uncommitted changes warning for: {c}', flush=True)
    return cleaned

# Cleanup
cmd('configure', 3)
cmd('rollback', 5)
cmd('end', 3)

# Test 1: Exact same approach as apply_acl_v2.py but for IPv4
print('=== TEST: Replicate apply_acl_v2 approach for IPv4 ===', flush=True)
cmd('configure', 3)

# Create ACL (flat from config top)
print('--- Creating ACL ---', flush=True)
out = cmd('access-lists ipv4 qa-ipv4 rule 10 allow', 3)
print(f'  create rule 10: {out[-100:]}', flush=True)
cmd('top', 2)
out = cmd('access-lists ipv4 qa-ipv4 rule 20 deny', 3)
print(f'  create rule 20: {out[-100:]}', flush=True)
cmd('top', 2)

# Bind to interface (flat from config top, exactly like apply_acl_v2.py)
print('--- Binding ACL ---', flush=True)
out = cmd('interfaces ge10-0/0/32 access-list ipv4 qa-ipv4 direction in', 5)
print(f'  bind output: {repr(out[-300:])}', flush=True)
cmd('top', 2)

# Also add SNMP community
cmd('system snmp community qa-test vrf default', 3)
cmd('top', 2)

# Commit immediately
print('--- Committing ---', flush=True)
out = cmd('commit', 25)
has_error = bool(re.search(r'error|failed', out, re.IGNORECASE))
print(f'  Commit: {"FAIL" if has_error else "OK"}', flush=True)
if has_error:
    print(f'  {out[-1000:]}', flush=True)
    cmd('rollback', 5)

cmd('end', 3)

# Verify
if not has_error:
    print('\n=== VERIFY ===', flush=True)
    print(cmd('show config interfaces ge10-0/0/32 | no-more', 10), flush=True)
    print(cmd('show access-lists counters | no-more', 10), flush=True)
    print(cmd('show access-lists counters ge10-0/0/32 | no-more', 10), flush=True)

    print('\n--- Waiting 100s for SNMP refresh ---', flush=True)
    time.sleep(100)

    print('\n--- ACL MIB walk ---', flush=True)
    print(cmd('run system snmp walk "1.3.6.1.4.1.49739.2.17" | no-more', 60), flush=True)

# Cleanup
print('\n=== CLEANUP ===', flush=True)
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

ssh.close()
print('\nDONE', flush=True)
