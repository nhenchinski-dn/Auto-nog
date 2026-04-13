#!/usr/bin/env python3
"""Phase 2: Map ACL MIB feature to runtime behavior via CLI SNMP walk."""
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

def snmpwalk(oid, wait=30):
    return cmd(f'run system snmp walk "{oid}" | no-more', wait)

def snmpget(oid, wait=10):
    return cmd(f'run system snmp get "{oid}" | no-more', wait)

# ---------------------------------------------------------------
# TEST 1: Walk ACL MIB base (with existing cfmblock ACL, no bindings)
# ---------------------------------------------------------------
print('='*70, flush=True)
print('TEST 1: Walk ACL MIB with unattached ACL (cfmblock exists)', flush=True)
print('='*70, flush=True)

print('\n--- dnAclObjectTable (mapping table) ---', flush=True)
print(snmpwalk('1.3.6.1.4.1.49739.2.17.1.1', 30), flush=True)

print('\n--- Full ACL MIB walk ---', flush=True)
print(snmpwalk('1.3.6.1.4.1.49739.2.17', 30), flush=True)

# ---------------------------------------------------------------
# TEST 2: Create IPv4 ACL, attach to interface, walk MIB
# ---------------------------------------------------------------
print('\n' + '='*70, flush=True)
print('TEST 2: Create IPv4 ACL + attach to interface', flush=True)
print('='*70, flush=True)

cmd('configure', 3)
cmd('access-lists', 2)
cmd('ipv4 test-ipv4-acl', 2)
cmd('rule 10 allow', 2)
cmd('protocol tcp', 2)
cmd('exit', 2)  # exit rule
cmd('rule 20 deny', 2)
cmd('exit', 2)  # exit rule
cmd('exit', 2)  # exit acl

# Create IPv6 ACL
cmd('ipv6 test-ipv6-acl', 2)
cmd('rule 10 allow', 2)
cmd('protocol udp', 2)
cmd('exit', 2)  # exit rule
cmd('exit', 2)  # exit acl

cmd('top', 2)

# Attach IPv4 ACL to an up interface (ge10-0/0/32 is up)
cmd('interfaces ge10-0/0/32', 2)
cmd('access-list ipv4 test-ipv4-acl direction ingress', 5)
cmd('top', 2)

# Attach IPv6 ACL to same interface
cmd('interfaces ge10-0/0/32', 2)
cmd('access-list ipv6 test-ipv6-acl direction ingress', 5)
cmd('top', 2)

# Commit
print('\n--- Committing ACLs ---', flush=True)
out = cmd('commit', 20)
has_error = bool(re.search(r'error|failed', out, re.IGNORECASE))
print(f'Commit: {"FAIL - " + out[-500:] if has_error else "OK"}', flush=True)
cmd('end', 3)

if not has_error:
    # Wait for SNMP agent to refresh
    print('\n--- Waiting 90s for SNMP agent periodic refresh ---', flush=True)
    time.sleep(90)

    print('\n--- dnAclObjectTable (mapping table) ---', flush=True)
    print(snmpwalk('1.3.6.1.4.1.49739.2.17.1.1', 30), flush=True)

    print('\n--- IPv4 Counter Table ---', flush=True)
    print(snmpwalk('1.3.6.1.4.1.49739.2.17.1.2', 30), flush=True)

    print('\n--- IPv6 Counter Table ---', flush=True)
    print(snmpwalk('1.3.6.1.4.1.49739.2.17.1.3', 30), flush=True)

    print('\n--- Ethernet Counter Table ---', flush=True)
    print(snmpwalk('1.3.6.1.4.1.49739.2.17.1.4', 30), flush=True)

    print('\n--- Default Rule Counter Tables ---', flush=True)
    print(snmpwalk('1.3.6.1.4.1.49739.2.17.1.5', 30), flush=True)
    print(snmpwalk('1.3.6.1.4.1.49739.2.17.1.6', 30), flush=True)
    print(snmpwalk('1.3.6.1.4.1.49739.2.17.1.7', 30), flush=True)

    # Compare with CLI counters
    print('\n--- CLI: show access-lists counters ---', flush=True)
    print(cmd('show access-lists counters | no-more', 10), flush=True)

    print('\n--- CLI: show access-lists counters ge10-0/0/32 ---', flush=True)
    print(cmd('show access-lists counters ge10-0/0/32 | no-more', 10), flush=True)

    # Test SNMP GET on specific OID
    print('\n--- SNMP GET on first mapping entry ---', flush=True)
    print(snmpget('1.3.6.1.4.1.49739.2.17.1.1.1.3.1', 10), flush=True)

    # Test SNMP GET on non-existent OID
    print('\n--- SNMP GET on non-existent OID ---', flush=True)
    print(snmpget('1.3.6.1.4.1.49739.2.17.1.2.1.6.99999.1.99999.1', 10), flush=True)

    # ---------------------------------------------------------------
    # TEST 3: Duplicate ACL name across address types
    # ---------------------------------------------------------------
    print('\n' + '='*70, flush=True)
    print('TEST 3: Same ACL name across different address types', flush=True)
    print('='*70, flush=True)

    cmd('configure', 3)
    cmd('access-lists', 2)
    cmd('ipv4 shared-name', 2)
    cmd('rule 10 allow', 2)
    cmd('exit', 2)  # exit rule
    cmd('exit', 2)  # exit acl
    cmd('ipv6 shared-name', 2)
    cmd('rule 10 allow', 2)
    cmd('exit', 2)  # exit rule
    cmd('exit', 2)  # exit acl
    cmd('eth shared-name', 2)
    cmd('rule 10 allow', 2)
    cmd('exit', 2)  # exit rule
    cmd('exit', 2)  # exit acl
    cmd('top', 2)
    out = cmd('commit', 20)
    has_error2 = bool(re.search(r'error|failed', out, re.IGNORECASE))
    print(f'Commit shared-name ACLs: {"FAIL" if has_error2 else "OK"}', flush=True)
    cmd('end', 3)

    if not has_error2:
        time.sleep(90)
        print('\n--- dnAclObjectTable after shared-name ACLs ---', flush=True)
        print(snmpwalk('1.3.6.1.4.1.49739.2.17.1.1', 30), flush=True)

# ---------------------------------------------------------------
# CLEANUP: Remove test ACLs
# ---------------------------------------------------------------
print('\n' + '='*70, flush=True)
print('CLEANUP: Removing test ACLs', flush=True)
print('='*70, flush=True)

cmd('configure', 3)
cmd('interfaces ge10-0/0/32', 2)
cmd('no access-list ipv4 test-ipv4-acl', 3)
cmd('no access-list ipv6 test-ipv6-acl', 3)
cmd('top', 2)
cmd('no access-lists ipv4 test-ipv4-acl', 3)
cmd('no access-lists ipv6 test-ipv6-acl', 3)
cmd('no access-lists ipv4 shared-name', 3)
cmd('no access-lists ipv6 shared-name', 3)
cmd('no access-lists eth shared-name', 3)
out = cmd('commit', 20)
has_error3 = bool(re.search(r'error|failed', out, re.IGNORECASE))
print(f'Cleanup commit: {"FAIL" if has_error3 else "OK"}', flush=True)
if has_error3:
    print(out[-500:], flush=True)
    cmd('rollback', 5)
cmd('end', 3)

ssh.close()
print('\nDONE', flush=True)
