#!/usr/bin/env python3
"""SW-258863: Configure uRPF on breakout ports of ge400-0/0/3 for Spirent traffic test."""

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

PORT_A = "ge100-0/0/3/0"  # Spirent Port A — uRPF ingress
PORT_B = "ge100-0/0/3/1"  # Spirent Port B — destination

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
    print(f"SW-258863 Spirent Traffic Test")
    print(f"Port A (uRPF): {PORT_A} = 10.0.30.1/24")
    print(f"Port B (dest):  {PORT_B} = 10.0.31.1/24")
    print("=" * 60)
    print("Connecting...")
    ssh, chan = connect()
    print("Connected.\n")

    # First remove old uRPF config from ge100-0/0/13/0 if still there
    run(chan, 'configure')
    run(chan, 'interfaces ge100-0/0/13/0 no urpf')
    run(chan, 'interfaces ge100-0/0/13/0 no ipv4-address')
    run(chan, 'interfaces ge100-0/0/13/1 no urpf')
    run(chan, 'interfaces ge100-0/0/13/1 no ipv4-address')
    run(chan, 'interfaces ge400-0/0/3 no ipv4-address')
    commit(chan, "cleanup old config")

    # Configure Port A with IP + uRPF strict
    run(chan, f'interfaces {PORT_A} ipv4-address 10.0.30.1/24')
    run(chan, f'interfaces {PORT_A} urpf admin-state enabled')
    run(chan, f'interfaces {PORT_A} urpf mode strict')
    run(chan, f'interfaces {PORT_A} urpf allow-default disabled')

    # Configure Port B with IP (destination)
    run(chan, f'interfaces {PORT_B} ipv4-address 10.0.31.1/24')

    ok, _ = commit(chan, "IPs + uRPF strict on Port A")
    run(chan, 'exit')

    if not ok:
        print("Config failed, aborting.")
        ssh.close()
        return 1

    # Verify
    print("\n--- Verify config ---")
    out = run_show(chan, f'show config interfaces {PORT_A}')
    print(f"\n{PORT_A} config:")
    for line in out.splitlines():
        s = line.strip()
        if s and 'show ' not in s and 'NCP3' not in s:
            print(f"  {s}")

    out = run_show(chan, f'show config interfaces {PORT_B}')
    print(f"\n{PORT_B} config:")
    for line in out.splitlines():
        s = line.strip()
        if s and 'show ' not in s and 'NCP3' not in s:
            print(f"  {s}")

    # Show uRPF state
    out = run_show(chan, f'show interfaces {PORT_A}')
    print(f"\n{PORT_A} uRPF state:")
    for line in out.splitlines():
        if 'urpf' in line.lower() or 'Admin state' in line:
            print(f"  {line.strip()}")

    # Baseline counters
    out = run_show(chan, f'show interfaces counters {PORT_A}')
    print(f"\nBaseline uRPF counters on {PORT_A}:")
    for line in out.splitlines():
        if 'urpf' in line.lower():
            print(f"  {line.strip()}")

    print("\n" + "=" * 60)
    print("  CONFIG READY")
    print("=" * 60)
    print(f"\n  Router {PORT_A}: 10.0.30.1/24  (uRPF strict)")
    print(f"  Router {PORT_B}: 10.0.31.1/24")
    print(f"\n  Spirent Port A (on {PORT_A}):")
    print(f"    IP: 10.0.30.100/24, Gateway: 10.0.30.1")
    print(f"\n  Spirent Port B (on {PORT_B}):")
    print(f"    IP: 10.0.31.100/24, Gateway: 10.0.31.1")
    print(f"\n  Traffic streams (Port A → Port B):")
    print(f"    1) VALID:   src=10.0.30.100  dst=10.0.31.100")
    print(f"    2) SPOOFED: src=10.99.99.99  dst=10.0.31.100")
    print(f"\n  >>> WAITING — start Spirent traffic, then press Enter <<<")

    input()

    # Post-traffic counters
    print("\nCapturing post-traffic counters...")
    out = run_show(chan, f'show interfaces counters {PORT_A}')
    print(f"\nPost-traffic uRPF counters on {PORT_A}:")
    for line in out.splitlines():
        if 'urpf' in line.lower():
            print(f"  {line.strip()}")

    print(f"\nPost-traffic RX on {PORT_A}:")
    for line in out.splitlines():
        if 'RX octets' in line or 'RX frames' in line or 'RX unicast' in line:
            print(f"  {line.strip()}")

    out_b = run_show(chan, f'show interfaces counters {PORT_B}')
    print(f"\nPost-traffic TX on {PORT_B}:")
    for line in out_b.splitlines():
        if 'TX octets' in line or 'TX frames' in line or 'TX unicast' in line:
            print(f"  {line.strip()}")

    print("\n  Config left in place. Run again or clean up manually.")
    print("=== Done ===")
    ssh.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
