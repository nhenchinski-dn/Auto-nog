#!/usr/bin/env python3
"""SW-258863: Reconfigure uRPF on breakout ports, pause for user traffic."""

import paramiko
import time
import re
import sys
import functools

_orig_print = print
print = functools.partial(_orig_print, flush=True)

DEVICE_IP = "100.64.6.73"
USERNAME = "dnroot"
PASSWORD = "dnroot"

BO_PORT_0 = "ge100-0/0/13/0"
TRAFFIC_PORT = "ge400-0/0/3"

ANSI_RE = re.compile(r'\x1b\[[0-9;]*[A-Za-z]|\x1b\].*?\x07|\x1b[()][A-B012]|\x0f')

def clean(text):
    return ANSI_RE.sub('', text)

def connect():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(DEVICE_IP, username=USERNAME, password=PASSWORD,
                timeout=30, look_for_keys=False, allow_agent=False)
    chan = ssh.invoke_shell(width=300)
    time.sleep(5)
    chan.recv(65535)
    return ssh, chan

def run(chan, cmd, wait=8):
    chan.send(cmd + '\n')
    time.sleep(wait)
    out = b''
    while chan.recv_ready():
        out += chan.recv(65535)
    return clean(out.decode(errors='replace'))

def run_show(chan, cmd):
    return run(chan, cmd + ' | no-more')

def commit(chan, label=""):
    out = run(chan, 'commit', wait=20)
    ok = 'Commit succeeded' in out
    tag = f" [{label}]" if label else ""
    print(f">>> COMMIT{tag}")
    print(f"  Result: {'OK' if ok else 'FAILED'}")
    for line in out.splitlines():
        s = line.strip()
        if any(kw in s.lower() for kw in ['commit', 'error', 'fail', 'notice']):
            print(f"    {s}")
    if not ok:
        run(chan, 'rollback 0', wait=5)
        print("  (rolled back)")
    return ok, out


def main():
    print(f"Reconfiguring uRPF on {BO_PORT_0} for traffic test...")
    print("=" * 60)
    print("Connecting...")
    ssh, chan = connect()
    print("Connected.\n")

    # Configure
    run(chan, 'configure')
    run(chan, f'interfaces {TRAFFIC_PORT} ipv4-address 192.168.3.1/24')
    run(chan, f'interfaces {BO_PORT_0} ipv4-address 192.168.130.1/24')
    run(chan, f'interfaces {BO_PORT_0} urpf admin-state enabled')
    run(chan, f'interfaces {BO_PORT_0} urpf mode strict')
    run(chan, f'interfaces {BO_PORT_0} urpf allow-default disabled')
    ok, _ = commit(chan, "IPs + uRPF strict")
    run(chan, 'exit')
    if not ok:
        ssh.close()
        return 1

    # Verify config is in place
    out = run_show(chan, f'show config interfaces {BO_PORT_0} urpf')
    print(f"\n--- show config interfaces {BO_PORT_0} urpf ---")
    for line in out.splitlines():
        s = line.strip()
        if s and 'show ' not in s and 'NCP3' not in s:
            print(f"  {s}")

    # Baseline counters
    out_ctr = run_show(chan, f'show interfaces counters {BO_PORT_0}')
    print(f"\n--- BASELINE counters ---")
    for line in out_ctr.splitlines():
        if 'urpf' in line.lower():
            print(f"  {line.strip()}")

    print("\n" + "=" * 60)
    print("  CONFIG READY — uRPF strict active on ge100-0/0/13/0")
    print("  Traffic port ge400-0/0/3 has IP 192.168.3.1/24")
    print("  Breakout port ge100-0/0/13/0 has IP 192.168.130.1/24")
    print("=" * 60)
    print("\n  Send traffic INTO ge100-0/0/13/0:")
    print("    VALID:   src=192.168.130.100  dst=192.168.3.1")
    print("    SPOOFED: src=10.99.99.99      dst=192.168.3.1")
    print("\n  >>> WAITING FOR USER — press Enter when done <<<")

    input()

    print("\nCapturing post-traffic counters...")
    out_ctr_after = run_show(chan, f'show interfaces counters {BO_PORT_0}')
    print(f"\n--- POST-TRAFFIC counters ---")
    for line in out_ctr_after.splitlines():
        if 'urpf' in line.lower():
            print(f"  {line.strip()}")

    print("\nDone. Leaving config in place for now.")
    ssh.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
