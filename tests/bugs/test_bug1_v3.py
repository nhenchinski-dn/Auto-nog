#!/usr/bin/env python3
"""BUG-1 reproduction: Add delay-rtt-max 1 to DM profile (delay is 16+ usec),
wait for cycles, then check traps and restore."""
import paramiko, time, re, sys

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

print("=" * 70)
print("STEP 1: Add delay-rtt-max 1 (current delay ~16 usec => always violated)")
print("=" * 70)
out = run_cli(ch, 'config', 3)
print(out)
out = run_cli(ch, 'services performance-monitoring profiles cfm two-way-delay-measurement DM_PROF_CLI', 3)
print(out)
out = run_cli(ch, 'thresholds', 3)
print(out)
out = run_cli(ch, 'delay-rtt-max 1', 3)
print(out)
out = run_cli(ch, 'top', 3)
print(out)
out = run_cli(ch, 'commit', 10)
print("COMMIT:", out)
out = run_cli(ch, 'end', 3)
print(out)

print("=" * 70)
print("STEP 2: Verify config - inform-test-results must still be disabled")
print("=" * 70)
out = run_cli(ch, 'show config services performance-monitoring profiles cfm two-way-delay-measurement DM_PROF_CLI | no-more', 8)
print(out)

print("=" * 70)
print("STEP 3: Wait 90s for ~6 DM test cycles")
print("=" * 70)
sys.stdout.flush()
for i in range(18):
    time.sleep(5)
    print(f"  ... {(i+1)*5}s", flush=True)

print("=" * 70)
print("STEP 4: Show DM detail to confirm violations occurring")
print("=" * 70)
out = run_cli(ch, 'show services performance-monitoring cfm tests proactive two-way-delay session-name DM_CLI_TAB_mep2 detail | no-more', 15)
print(out)

print("=" * 70)
print("STEP 5: Restore - remove delay-rtt-max")
print("=" * 70)
out = run_cli(ch, 'config', 3)
print(out)
out = run_cli(ch, 'services performance-monitoring profiles cfm two-way-delay-measurement DM_PROF_CLI', 3)
print(out)
out = run_cli(ch, 'thresholds', 3)
print(out)
out = run_cli(ch, 'no delay-rtt-max', 3)
print(out)
out = run_cli(ch, 'top', 3)
print(out)
out = run_cli(ch, 'commit', 10)
print("COMMIT:", out)
out = run_cli(ch, 'end', 3)
print(out)

print("=" * 70)
print("STEP 6: Verify restored config")
print("=" * 70)
out = run_cli(ch, 'show config services performance-monitoring profiles cfm two-way-delay-measurement DM_PROF_CLI | no-more', 8)
print(out)

ch.close()
c.close()

print()
print("=" * 70)
print("TEST COMPLETE - CHECK TERMINAL 1 FOR DM TRAPS")
print("  Look for: .1.13.0 = INTEGER: 1  (session type = DM)")
print("  If present => BUG-1 CONFIRMED")
print("  If absent  => BUG-1 NOT reproduced")
print("=" * 70)
