#!/usr/bin/env python3
"""SW-244107: Egress ACL + SH-BFD/uBFD coexistence test.

Measures egress ACL counter rate on bundle-10 across four phases:
  A) Baseline (no BFD)
  B) With uBFD on bundle-10
  C) With SH-BFD (SR-MPLS policy)
  D) Cleanup — back to baseline
"""

import paramiko
import time
import re
import sys

HOST = "WKY1C7VD00008P2"
USER = "dnroot"
PASS = "dnroot"
MEASURE_SECONDS = 15

ANSI_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')
CR_RE = re.compile(r'\r')
MORE_RE = re.compile(r'-- More -- \(Press q to quit\)\s*')


def clean(text):
    text = ANSI_RE.sub('', text)
    text = CR_RE.sub('', text)
    text = MORE_RE.sub('', text)
    return text


def connect():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASS,
                look_for_keys=False, allow_agent=False, timeout=15)
    chan = ssh.invoke_shell(width=250, height=5000)
    time.sleep(6)
    chan.recv(65535)
    return ssh, chan


def run(chan, cmd, wait=5):
    chan.send(cmd + "\n")
    time.sleep(wait)
    out = b""
    while chan.recv_ready():
        out += chan.recv(65535)
        time.sleep(0.3)
    return clean(out.decode(errors='replace'))


def get_acl_matches(chan):
    out = run(chan, "show access-lists counters bundle-10 | no-more", wait=6)
    for line in out.split('\n'):
        if 'egress-bfd' in line and 'allow' in line:
            nums = re.findall(r'(\d{5,})', line)
            if nums:
                return int(nums[-1])
    return None


def measure_rate(chan, label):
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    c1 = get_acl_matches(chan)
    if c1 is None:
        print("  ERROR: Could not read ACL counters")
        return None
    print(f"  Counter start: {c1:,}")
    print(f"  Waiting {MEASURE_SECONDS}s ...")
    time.sleep(MEASURE_SECONDS)
    c2 = get_acl_matches(chan)
    if c2 is None:
        print("  ERROR: Could not read ACL counters (2nd read)")
        return None
    delta = c2 - c1
    pps = delta / MEASURE_SECONDS
    print(f"  Counter end:   {c2:,}")
    print(f"  Delta:         {delta:,} packets in {MEASURE_SECONDS}s")
    print(f"  Rate:          {pps:,.0f} pps")
    if delta > 0:
        print(f"  RESULT:        ACL counters ARE incrementing")
    else:
        print(f"  RESULT:        ACL counters NOT incrementing")
    return delta


def configure_ubfd(chan):
    print("\n>> Configuring uBFD on bundle-10 ...")
    run(chan, "configure", wait=3)
    run(chan, "protocols bfd interface bundle-10", wait=2)
    run(chan, "admin-state enabled", wait=2)
    run(chan, "desired-minimum-tx-interval 300", wait=2)
    run(chan, "required-minimum-receive 300", wait=2)
    run(chan, "detection-multiplier 3", wait=2)
    out = run(chan, "commit", wait=10)
    print(f"   commit: {'OK' if 'ERROR' not in out.upper() else out}")
    run(chan, "top", wait=2)
    run(chan, "exit", wait=2)
    time.sleep(5)


def remove_ubfd(chan):
    print("\n>> Removing uBFD from bundle-10 ...")
    run(chan, "configure", wait=3)
    run(chan, "no protocols bfd interface bundle-10", wait=2)
    out = run(chan, "commit", wait=10)
    print(f"   commit: {'OK' if 'ERROR' not in out.upper() else out}")
    run(chan, "top", wait=2)
    run(chan, "exit", wait=2)
    time.sleep(5)


def configure_shbfd(chan):
    print("\n>> Configuring SH-BFD (SR-MPLS policy) ...")
    run(chan, "configure", wait=3)
    run(chan, "protocols segment-routing mpls policy test-sbfd", wait=2)
    run(chan, "destination 20.0.0.2 strict-spf", wait=2)
    run(chan, "color 100", wait=2)
    run(chan, "seamless-bfd", wait=2)
    run(chan, "admin-state enabled", wait=2)
    run(chan, "remote-reflector-discriminator 100", wait=2)
    run(chan, "interval 300", wait=2)
    run(chan, "multiplier 3", wait=2)
    run(chan, "top", wait=2)
    out = run(chan, "commit", wait=10)
    print(f"   commit: {'OK' if 'ERROR' not in out.upper() else out}")
    run(chan, "top", wait=2)
    run(chan, "exit", wait=2)
    time.sleep(5)


def remove_shbfd(chan):
    print("\n>> Removing SH-BFD (SR-MPLS policy) ...")
    run(chan, "configure", wait=3)
    run(chan, "no protocols segment-routing mpls policy test-sbfd", wait=2)
    out = run(chan, "commit", wait=10)
    print(f"   commit: {'OK' if 'ERROR' not in out.upper() else out}")
    run(chan, "top", wait=2)
    run(chan, "exit", wait=2)
    time.sleep(5)


def main():
    print("SW-244107: Egress ACL + SH-BFD/uBFD coexistence test")
    print(f"Device: {HOST}")
    print(f"Measurement window: {MEASURE_SECONDS}s per phase\n")

    ssh, chan = connect()
    results = {}

    try:
        # Phase A: Baseline
        results['A'] = measure_rate(chan, "Phase A: Baseline (no BFD)")

        # Phase B: uBFD
        configure_ubfd(chan)
        results['B'] = measure_rate(chan, "Phase B: With uBFD on bundle-10")
        remove_ubfd(chan)

        # Phase C: SH-BFD
        configure_shbfd(chan)
        results['C'] = measure_rate(chan, "Phase C: With SH-BFD (SR-MPLS policy)")
        remove_shbfd(chan)

        # Phase D: Post-cleanup baseline
        results['D'] = measure_rate(chan, "Phase D: Post-cleanup baseline")

    finally:
        ssh.close()

    print(f"\n{'='*60}")
    print("  SUMMARY")
    print(f"{'='*60}")
    for phase, label in [('A', 'Baseline (no BFD)'),
                         ('B', 'With uBFD'),
                         ('C', 'With SH-BFD'),
                         ('D', 'Post-cleanup')]:
        d = results.get(phase)
        if d is not None:
            status = "INCREMENTING" if d > 0 else "NOT incrementing"
            print(f"  Phase {phase} ({label}): {d:,} pkts ({d/MEASURE_SECONDS:,.0f} pps) — {status}")
        else:
            print(f"  Phase {phase} ({label}): ERROR reading counters")

    print()
    a = results.get('A', 0) or 0
    b = results.get('B', 0) or 0
    c = results.get('C', 0) or 0
    if a > 0 and b == 0:
        print("  >> uBFD BLOCKS egress ACL counters")
    elif a > 0 and b > 0:
        print("  >> uBFD does NOT block egress ACL counters")
    if a > 0 and c == 0:
        print("  >> SH-BFD BLOCKS egress ACL counters")
    elif a > 0 and c > 0:
        print("  >> SH-BFD does NOT block egress ACL counters")


if __name__ == "__main__":
    main()
