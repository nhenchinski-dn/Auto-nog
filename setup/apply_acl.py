#!/usr/bin/env python3
"""
Create an eth ACL that drops CFM frames (EtherType 0x8902) on ingress
of ge10-0/0/32.100, wait for a few SLM test cycles, then remove it.
This should cause near-end loss: remote sends SLR but local drops them.
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

def cmd(c, wait=5):
    chan.send(c + '\n')
    time.sleep(wait)
    out = b''
    while chan.recv_ready():
        out += chan.recv(65535)
    return ANSI_RE.sub('', out.decode(errors='replace')).strip()

def show(c, wait=10):
    return cmd(c + ' | no-more', wait)


# Step 1: Check SLM status before
print('=== BEFORE: SLM status ===', flush=True)
out = show('show services performance-monitoring cfm tests proactive two-way-synthetic-loss session-name SLM_CLI_TAB_mep1 detail', 12)
for line in out.split('\n')[-25:]:
    print(line, flush=True)

# Step 2: Explore the ether-type options inside the deny rule context
print('\n=== Exploring deny rule match options ===', flush=True)
cmd('configure', 3)
cmd('access-lists', 2)
cmd('eth DROP_CFM', 2)
cmd('rule 10 deny', 2)

out = cmd('ether-type ?', 5)
print(out[-1000:], flush=True)

# Try setting ether-type to CFM (0x8902 = 35074)
print('\n=== Setting ether-type 0x8902 ===', flush=True)
out = cmd('ether-type 0x8902', 5)
print(out[-500:], flush=True)

# If 0x didn't work, try decimal
if 'ERROR' in out or 'error' in out.lower():
    print('Trying decimal 35074...', flush=True)
    out = cmd('ether-type 35074', 5)
    print(out[-500:], flush=True)

# Exit rule context, add allow-all rule
cmd('exit', 2)
print('\n=== Adding allow-all rule 65000 ===', flush=True)
cmd('rule 65000 allow', 2)
out = cmd('exit', 2)

# Show what we have
cmd('top', 2)
print('\n=== ACL config so far ===', flush=True)
out = show('show config access-lists eth DROP_CFM')
print(out[-1000:], flush=True)

# Step 3: Bind ACL to interface ingress
print('\n=== Binding ACL to ge10-0/0/32.100 ingress ===', flush=True)
cmd('interfaces ge10-0/0/32.100', 2)
out = cmd('access-list eth ?', 5)
print(f'access-list eth ?: {out[-500:]}', flush=True)

# The syntax might be: access-list eth <name> direction ingress
out = cmd('access-list eth DROP_CFM ?', 5)
print(f'access-list eth DROP_CFM ?: {out[-500:]}', flush=True)

out = cmd('access-list eth DROP_CFM direction ?', 5)
print(f'direction ?: {out[-500:]}', flush=True)

out = cmd('access-list eth DROP_CFM direction ingress', 5)
print(f'direction ingress: {out[-500:]}', flush=True)

cmd('top', 2)

# Commit
print('\n=== Committing ACL ===', flush=True)
out = cmd('commit', 20)
has_error = bool(re.search(r'error|failed', out, re.IGNORECASE))
print(f'Commit result: {"FAIL" if has_error else "OK"}', flush=True)
if has_error:
    print(out[-1000:], flush=True)
    cmd('rollback', 5)

cmd('end', 3)

if not has_error:
    # Step 4: Wait for 2-3 SLM test cycles (each is ~15s total: 5s probes + 10s repeat)
    print('\n=== ACL applied. Waiting 35s for SLM test cycles with loss ===', flush=True)
    time.sleep(35)

    # Step 5: Check SLM results - should show near-end loss
    print('\n=== AFTER ACL: SLM status ===', flush=True)
    out = show('show services performance-monitoring cfm tests proactive two-way-synthetic-loss session-name SLM_CLI_TAB_mep1 detail', 12)
    for line in out.split('\n')[-30:]:
        print(line, flush=True)

    # Step 6: Remove ACL
    print('\n=== Removing ACL ===', flush=True)
    cmd('configure', 3)
    cmd('interfaces ge10-0/0/32.100', 2)
    cmd('no access-list eth DROP_CFM', 3)
    cmd('top', 2)
    cmd('no access-lists eth DROP_CFM', 3)
    out = cmd('commit', 20)
    has_error2 = bool(re.search(r'error|failed', out, re.IGNORECASE))
    print(f'Cleanup commit: {"FAIL" if has_error2 else "OK"}', flush=True)
    if has_error2:
        print(out[-500:], flush=True)
    cmd('end', 3)

    # Step 7: Wait for normal cycles to resume
    print('\n=== Waiting 20s for normal cycles ===', flush=True)
    time.sleep(20)

    print('\n=== AFTER CLEANUP: SLM status ===', flush=True)
    out = show('show services performance-monitoring cfm tests proactive two-way-synthetic-loss session-name SLM_CLI_TAB_mep1 detail', 12)
    for line in out.split('\n')[-30:]:
        print(line, flush=True)

    # Step 8: Check system events / syslog for CFM_PROACTIVE events
    print('\n=== Checking for events ===', flush=True)
    out = show('show system event-log | match CFM_PROACTIVE', 10)
    print(out[-1000:], flush=True)

    # Try other event locations
    for ev_cmd in [
        'show system event-log',
        'show system alarm',
        'show system alarms',
        'show services ethernet-oam connectivity-fault-management maintenance-domains MD-CUST maintenance-associations MA-CUST local-mep 1',
    ]:
        print(f'\n=== {ev_cmd} ===', flush=True)
        out = show(ev_cmd, 10)
        if 'ERROR' not in out:
            print(out[-2000:], flush=True)
            break

ssh.close()
print('\nDONE', flush=True)
