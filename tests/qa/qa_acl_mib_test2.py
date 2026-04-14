#!/usr/bin/env python3
"""Phase 2: ACL MIB runtime testing - fixed version with robust end/commit handling."""
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

def ensure_operational_mode():
    """Force back to operational mode."""
    cmd('end', 3)
    out = cmd('', 2)
    if 'uncommitted' in out.lower():
        cmd('no', 3)
    cmd('', 2)

def snmpwalk(oid, wait=30):
    ensure_operational_mode()
    return cmd(f'run system snmp walk "{oid}" | no-more', wait)

def snmpget(oid, wait=10):
    ensure_operational_mode()
    return cmd(f'run system snmp get "{oid}" | no-more', wait)

# First, fix any leftover state from previous run
print('=== FIXING DEVICE STATE ===', flush=True)
ensure_operational_mode()
# Rollback any uncommitted changes
cmd('configure', 3)
cmd('rollback', 5)
cmd('end', 3)
out = cmd('', 2)
if 'uncommitted' in out.lower():
    cmd('no', 3)

# Check current ACL state
print('\n=== CURRENT ACL CONFIG ===', flush=True)
print(cmd('show config access-lists | no-more', 10), flush=True)

print('\n=== CURRENT ACL COUNTERS ===', flush=True)
print(cmd('show access-lists counters | no-more', 10), flush=True)

# Now walk ACL MIB with current state
print('\n=== ACL MIB WALK (current state) ===', flush=True)
print(snmpwalk('1.3.6.1.4.1.49739.2.17', 30), flush=True)

# Configure test ACLs properly
print('\n=== CONFIGURING TEST ACLs ===', flush=True)
cmd('configure', 3)

# Create IPv4 ACL with rules
cmd('access-lists ipv4 test-ipv4-acl rule 10 allow protocol tcp', 3)
cmd('top', 2)
cmd('access-lists ipv4 test-ipv4-acl rule 20 deny', 3)
cmd('top', 2)

# Create IPv6 ACL
cmd('access-lists ipv6 test-ipv6-acl rule 10 allow protocol udp', 3)
cmd('top', 2)

# Attach IPv4 ACL to ge10-0/0/32 ingress
cmd('interfaces ge10-0/0/32 access-list ipv4 test-ipv4-acl direction ingress', 5)
cmd('top', 2)

# Attach IPv6 ACL to ge10-0/0/32 ingress
cmd('interfaces ge10-0/0/32 access-list ipv6 test-ipv6-acl direction ingress', 5)
cmd('top', 2)

# Commit
print('--- Committing ---', flush=True)
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
    # Verify config
    print('\n=== ACL CONFIG AFTER ===', flush=True)
    print(cmd('show config access-lists | no-more', 10), flush=True)

    print('\n=== ACL COUNTERS AFTER ===', flush=True)
    print(cmd('show access-lists counters ge10-0/0/32 | no-more', 10), flush=True)

    # Wait for SNMP agent periodic refresh (90 seconds)
    print('\n--- Waiting 100s for SNMP agent refresh ---', flush=True)
    time.sleep(100)

    # Walk mapping table
    print('\n=== dnAclObjectTable (mapping table) ===', flush=True)
    print(snmpwalk('1.3.6.1.4.1.49739.2.17.1.1', 30), flush=True)

    # Walk IPv4 counter table
    print('\n=== IPv4 Counter Table ===', flush=True)
    print(snmpwalk('1.3.6.1.4.1.49739.2.17.1.2', 30), flush=True)

    # Walk IPv6 counter table
    print('\n=== IPv6 Counter Table ===', flush=True)
    print(snmpwalk('1.3.6.1.4.1.49739.2.17.1.3', 30), flush=True)

    # Walk Ethernet counter table
    print('\n=== Ethernet Counter Table ===', flush=True)
    print(snmpwalk('1.3.6.1.4.1.49739.2.17.1.4', 30), flush=True)

    # Walk Default IPv4 counter table
    print('\n=== Default IPv4 Counter Table ===', flush=True)
    print(snmpwalk('1.3.6.1.4.1.49739.2.17.1.5', 30), flush=True)

    # Walk Default IPv6 counter table
    print('\n=== Default IPv6 Counter Table ===', flush=True)
    print(snmpwalk('1.3.6.1.4.1.49739.2.17.1.6', 30), flush=True)

    # Walk Default Ethernet counter table
    print('\n=== Default Ethernet Counter Table ===', flush=True)
    print(snmpwalk('1.3.6.1.4.1.49739.2.17.1.7', 30), flush=True)

    # SNMP GET tests
    print('\n=== SNMP GET on non-existent OID ===', flush=True)
    print(snmpget('1.3.6.1.4.1.49739.2.17.1.2.1.6.99999.1.99999.1', 10), flush=True)

    # SNMP GETNEXT test
    print('\n=== SNMP GETNEXT test ===', flush=True)
    print(cmd('run system snmp getnext "1.3.6.1.4.1.49739.2.17" | no-more', 10), flush=True)

    # Compare CLI vs SNMP
    print('\n=== CLI show access-lists counters ge10-0/0/32 ===', flush=True)
    print(cmd('show access-lists counters ge10-0/0/32 | no-more', 10), flush=True)

# CLEANUP
print('\n=== CLEANUP ===', flush=True)
cmd('configure', 3)
cmd('interfaces ge10-0/0/32', 2)
cmd('no access-list ipv4 test-ipv4-acl', 3)
cmd('no access-list ipv6 test-ipv6-acl', 3)
cmd('top', 2)
cmd('no access-lists ipv4 test-ipv4-acl', 3)
cmd('no access-lists ipv6 test-ipv6-acl', 3)
out = cmd('commit', 20)
has_err = bool(re.search(r'error|failed', out, re.IGNORECASE))
print(f'Cleanup: {"FAIL" if has_err else "OK"}', flush=True)
if has_err:
    print(out[-500:], flush=True)
    cmd('rollback', 5)
cmd('end', 3)
out = cmd('', 2)
if 'uncommitted' in out.lower():
    cmd('no', 3)

# Also remove SNMP community
cmd('configure', 3)
cmd('no system snmp community testacl123', 3)
out = cmd('commit', 15)
print(f'Remove SNMP community: {"FAIL" if "error" in out.lower() else "OK"}', flush=True)
cmd('end', 3)
out = cmd('', 2)
if 'uncommitted' in out.lower():
    cmd('no', 3)

ssh.close()
print('\nDONE', flush=True)
