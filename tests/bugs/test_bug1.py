#!/usr/bin/env python3
"""BUG-1 reproduction test:
1. Lower DM threshold to guarantee violations every cycle
2. Keep inform-test-results disabled
3. Wait for several test cycles
4. Restore original threshold
"""
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
print("STEP 1: Show current DM profile config (baseline)")
print("=" * 70)
out = run_cli(ch, 'show config services performance-monitoring profiles cfm two-way-delay-measurement DM_PROF_CLI | no-more', 8)
print(out)

print("=" * 70)
print("STEP 2: Enter config mode and add frame-delay-two-way-max 1")
print("  (current delay is ~16 usec, so threshold of 1 usec will ALWAYS")
print("   be exceeded, forcing dnCfmProactiveTestFailure on every cycle)")
print("  inform-test-results remains DISABLED")
print("=" * 70)
out = run_cli(ch, 'config', 3)
print(out)
out = run_cli(ch, 'services performance-monitoring profiles cfm two-way-delay-measurement DM_PROF_CLI thresholds frame-delay-two-way-max 1', 5)
print(out)
out = run_cli(ch, 'commit', 8)
print(out)
out = run_cli(ch, 'end', 3)
print(out)

print("=" * 70)
print("STEP 3: Verify new config")
print("=" * 70)
out = run_cli(ch, 'show config services performance-monitoring profiles cfm two-way-delay-measurement DM_PROF_CLI | no-more', 8)
print(out)

print("=" * 70)
print("STEP 4: Show current time and wait for 4 DM test cycles (~60s)")
print("  DM cycle = 5 probes x 1s + 10s repeat = ~15s per cycle")
print("  Waiting 75s to capture ~5 cycles worth of potential traps...")
print("=" * 70)
out = run_cli(ch, 'show system clock | no-more', 5)
print("Start time:", out.strip().split('\n')[-2] if out.strip() else "unknown")
sys.stdout.flush()

for i in range(15):
    time.sleep(5)
    elapsed = (i + 1) * 5
    print(f"  ... {elapsed}s elapsed", flush=True)

out = run_cli(ch, 'show system clock | no-more', 5)
print("End time:", out.strip().split('\n')[-2] if out.strip() else "unknown")

print()
print("=" * 70)
print("STEP 5: Show DM detail to confirm threshold IS being violated")
print("=" * 70)
out = run_cli(ch, 'show services performance-monitoring cfm tests proactive two-way-delay session-name DM_CLI_TAB_mep2 detail | no-more', 15)
print(out)

print("=" * 70)
print("STEP 6: Restore original threshold (remove frame-delay-two-way-max)")
print("=" * 70)
out = run_cli(ch, 'config', 3)
print(out)
out = run_cli(ch, 'no services performance-monitoring profiles cfm two-way-delay-measurement DM_PROF_CLI thresholds frame-delay-two-way-max', 5)
print(out)
out = run_cli(ch, 'commit', 8)
print(out)
out = run_cli(ch, 'end', 3)
print(out)

print("=" * 70)
print("STEP 7: Verify restored config")
print("=" * 70)
out = run_cli(ch, 'show config services performance-monitoring profiles cfm two-way-delay-measurement DM_PROF_CLI | no-more', 8)
print(out)

ch.close()
c.close()

print()
print("=" * 70)
print("TEST COMPLETE")
print("Now check snmptrapd output in terminal 1 for traps with:")
print("  .1.13.0 = INTEGER: 1  (session type = DM)")
print("If DM traps appeared => BUG-1 CONFIRMED (inform-test-results ignored)")
print("If NO DM traps       => BUG-1 NOT REPRODUCED (inform-test-results works)")
print("=" * 70)
