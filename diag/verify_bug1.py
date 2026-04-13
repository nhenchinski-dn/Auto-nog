#!/usr/bin/env python3
"""Verify BUG-1: Check DM config and capture live traps to see if DM sessions
generate dnCfmProactiveTestFailure despite inform-test-results disabled."""
import paramiko, time, re, sys, socket, struct, threading

ANSI = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')
DEVICE = 'WKY1C7VD00008P2'

def run_cli(ch, cmd, wait=12):
    ch.send(cmd + '\n')
    time.sleep(wait)
    out = ''
    while ch.recv_ready():
        out += ch.recv(65536).decode(errors='ignore')
        time.sleep(0.3)
    return ANSI.sub('', out)

def capture_traps(port, duration, results):
    """Listen for SNMP traps on UDP port for duration seconds."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('0.0.0.0', port))
    sock.settimeout(2)
    end_time = time.time() + duration
    while time.time() < end_time:
        try:
            data, addr = sock.recvfrom(65536)
            results.append((time.strftime('%H:%M:%S'), addr, data.hex()))
        except socket.timeout:
            pass
    sock.close()

print("=" * 70)
print("STEP 1: Confirm DM profile has inform-test-results disabled")
print("=" * 70)
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(DEVICE, username='dnroot', password='dnroot', timeout=15,
          banner_timeout=15, auth_timeout=15)
ch = c.invoke_shell(width=250)
ch.settimeout(30)
time.sleep(3)
while ch.recv_ready():
    ch.recv(65536)

out = run_cli(ch, 'show config services performance-monitoring | no-more', 10)
print(out)

print()
print("=" * 70)
print("STEP 2: Confirm proactive sessions are running")
print("=" * 70)
out = run_cli(ch, 'show services performance-monitoring cfm tests proactive | no-more', 10)
print(out)

print()
print("=" * 70)
print("STEP 3: Show DM detail to confirm session type")
print("=" * 70)
out = run_cli(ch, 'show services performance-monitoring cfm tests proactive two-way-delay session-name DM_CLI_TAB_mep2 detail | no-more', 15)
print(out)

print()
print("=" * 70)
print("STEP 4: Show SLM detail to confirm session type")
print("=" * 70)
out = run_cli(ch, 'show services performance-monitoring cfm tests proactive two-way-synthetic-loss session-name SLM_CLI_TAB_mep2 detail | no-more', 15)
print(out)

ch.close()
c.close()

print()
print("=" * 70)
print("STEP 5: Show SNMP trap config")
print("=" * 70)
c2 = paramiko.SSHClient()
c2.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c2.connect(DEVICE, username='dnroot', password='dnroot', timeout=15,
           banner_timeout=15, auth_timeout=15)
ch2 = c2.invoke_shell(width=250)
ch2.settimeout(30)
time.sleep(3)
while ch2.recv_ready():
    ch2.recv(65536)
out = run_cli(ch2, 'show config system snmp | no-more', 8)
print(out)
ch2.close()
c2.close()

print()
print("=" * 70)
print("DONE - Check snmptrapd output for DM traps (.1.13.0 = INTEGER: 1)")
print("Session type enum: 1 = two-way-delay-measurement (DM)")
print("                   2 = two-way-synthetic-loss-measurement (SLM)")
print("=" * 70)
