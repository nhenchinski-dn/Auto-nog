#!/usr/bin/env python3
"""Discover valid threshold keywords for DM profile, then set one to force violations."""
import paramiko, time, re

ANSI = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')
DEVICE = 'WKY1C7VD00008P2'

def run_cli(ch, cmd, wait=8):
    ch.send(cmd + '\n')
    time.sleep(wait)
    out = ''
    while ch.recv_ready():
        out += ch.recv(65536).decode(errors='ignore')
        time.sleep(0.3)
    return ANSI.sub('', out)

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(DEVICE, username='dnroot', password='dnroot', timeout=15,
          banner_timeout=15, auth_timeout=15)
ch = c.invoke_shell(width=250)
ch.settimeout(30)
time.sleep(3)
while ch.recv_ready():
    ch.recv(65536)

print("STEP 1: Discover available threshold keywords")
out = run_cli(ch, 'config', 3)
out = run_cli(ch, 'services performance-monitoring profiles cfm two-way-delay-measurement DM_PROF_CLI thresholds ?', 5)
print(out)

print("\nSTEP 2: Set jitter-rtt-max to 0 (current jitter is 0-1 usec, so even 0 should trigger)")
out = run_cli(ch, 'services performance-monitoring profiles cfm two-way-delay-measurement DM_PROF_CLI thresholds jitter-rtt-max 0', 5)
print(out)

print("\nSTEP 2b: Try success-rate threshold if available")
out = run_cli(ch, 'services performance-monitoring profiles cfm two-way-delay-measurement DM_PROF_CLI thresholds success-rate ?', 5)
print(out)

print("\nSTEP 2c: Try delay-rtt-max")
out = run_cli(ch, 'services performance-monitoring profiles cfm two-way-delay-measurement DM_PROF_CLI thresholds delay-rtt-max 1', 5)
print(out)

out = run_cli(ch, 'commit', 8)
print("COMMIT:", out)

out = run_cli(ch, 'end', 3)

print("\nSTEP 3: Verify config")
out = run_cli(ch, 'show config services performance-monitoring profiles cfm two-way-delay-measurement DM_PROF_CLI | no-more', 8)
print(out)

print("\nSTEP 4: Confirm inform-test-results is still disabled in detail view")
out = run_cli(ch, 'show services performance-monitoring cfm tests proactive two-way-delay session-name DM_CLI_TAB_mep2 detail | no-more', 12)
print(out)

ch.close()
c.close()
