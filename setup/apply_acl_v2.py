#!/usr/bin/env python3
"""
Create an eth ACL that drops CFM frames (EtherType 0x8902) on ingress
of ge10-0/0/32.100, wait for SLM test cycles, then remove it.
"""
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
ERR_RE = re.compile(r'error|failed|unknown\s+word|syntax\s+error', re.IGNORECASE)

def cmd(c, wait=5):
    chan.send(c + '\n')
    time.sleep(wait)
    out = b''
    while chan.recv_ready():
        out += chan.recv(65535)
    return ANSI_RE.sub('', out.decode(errors='replace')).strip()

def show(c, wait=10):
    return cmd(c + ' | no-more', wait)


# Step 1: SLM status before
print('=== BEFORE: SLM status ===', flush=True)
out = show('show services performance-monitoring cfm tests proactive two-way-synthetic-loss session-name SLM_CLI_TAB_mep1 detail', 12)
for line in out.split('\n')[-25:]:
    print(line, flush=True)

# Step 2: Create ACL + bind to interface in one commit
print('\n=== Creating ACL and binding to interface ===', flush=True)
cmd('configure', 3)

# Define ACL
cmd('access-lists eth DROP_CFM rule 10 deny', 2)
cmd('ether-type CFM_0x8902', 2)
cmd('top', 2)
cmd('access-lists eth DROP_CFM rule 65000 allow', 2)
cmd('top', 2)

# Bind to interface with direction "in"
cmd('interfaces ge10-0/0/32.100 access-list eth DROP_CFM direction in', 3)
cmd('top', 2)

# Show candidate config diff
out = show('show config diff', 10)
print(out[-2000:], flush=True)

# Commit
print('\n=== Committing ===', flush=True)
out = cmd('commit', 25)
has_error = bool(ERR_RE.search(out))
print(f'Commit: {"FAIL - " + out[-500:] if has_error else "OK"}', flush=True)

if has_error:
    cmd('rollback', 5)
    cmd('end', 3)
    ssh.close()
    sys.exit(1)

cmd('end', 3)

# Verify ACL is applied
print('\n=== Verify interface shows ACL ===', flush=True)
out = show('show interfaces ge10-0/0/32.100', 10)
for line in out.split('\n'):
    if 'access' in line.lower() or 'acl' in line.lower():
        print(f'  {line.strip()}', flush=True)

# Step 3: Wait for SLM cycles with ACL active
# Profile: 5 probes @ 1s interval + 10s repeat = ~15s per cycle
print('\n=== ACL active. Waiting 45s for ~3 SLM cycles ===', flush=True)
time.sleep(45)

# Step 4: Check SLM results with ACL
print('\n=== SLM status WITH ACL (should show loss) ===', flush=True)
out = show('show services performance-monitoring cfm tests proactive two-way-synthetic-loss session-name SLM_CLI_TAB_mep1 detail', 12)
for line in out.split('\n')[-30:]:
    print(line, flush=True)

# Step 5: Remove ACL
print('\n=== Removing ACL ===', flush=True)
cmd('configure', 3)
cmd('no interfaces ge10-0/0/32.100 access-list eth DROP_CFM', 3)
cmd('no access-lists eth DROP_CFM', 3)
cmd('top', 2)
out = cmd('commit', 25)
has_error2 = bool(ERR_RE.search(out))
print(f'Cleanup commit: {"FAIL - " + out[-500:] if has_error2 else "OK"}', flush=True)
cmd('end', 3)

# Step 6: Wait for recovery and check events
print('\n=== Waiting 30s for normal SLM to resume ===', flush=True)
time.sleep(30)

print('\n=== AFTER CLEANUP: SLM status ===', flush=True)
out = show('show services performance-monitoring cfm tests proactive two-way-synthetic-loss session-name SLM_CLI_TAB_mep1 detail', 12)
for line in out.split('\n')[-30:]:
    print(line, flush=True)

# Check for events in syslog
print('\n=== Checking syslog for CFM events ===', flush=True)
for ev_cmd in [
    'show system alarms',
    'show system alarm',
    'show services ethernet-oam connectivity-fault-management maintenance-domains MD-CUST maintenance-associations MA-CUST local-mep 1',
]:
    out = show(ev_cmd, 10)
    if 'ERROR' not in out and len(out.strip()) > 50:
        print(f'\n--- {ev_cmd} ---', flush=True)
        print(out[-2000:], flush=True)

ssh.close()
print('\nDONE', flush=True)
