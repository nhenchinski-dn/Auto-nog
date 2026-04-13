#!/usr/bin/env python3
"""SW-244107: Re-run Phase C (SH-BFD) and Phase D (post-cleanup)."""

import paramiko
import time
import re

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
    print(f"   DEBUG: could not parse counters from output")
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


def main():
    print("SW-244107: Phase C (SH-BFD) + Phase D (post-cleanup)")
    print(f"Device: {HOST}\n")

    ssh, chan = connect()

    try:
        # Verify we're starting clean
        out = run(chan, "show config protocols segment-routing | no-more", wait=6)
        print("Current SR config:")
        for line in out.split('\n'):
            stripped = line.strip()
            if stripped and 'NCP3' not in stripped and 'config-start' not in stripped and 'config-end' not in stripped:
                print(f"  {stripped}")
        if 'policy' not in out:
            print("  (empty — no SR policies)\n")

        # Phase C: Configure SH-BFD
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
        commit_out = run(chan, "commit", wait=10)
        if 'ERROR' in commit_out.upper():
            print(f"   COMMIT FAILED: {commit_out}")
            run(chan, "rollback", wait=5)
            run(chan, "exit", wait=2)
            ssh.close()
            return
        print("   commit: OK")
        run(chan, "exit", wait=2)
        time.sleep(5)

        # Verify SH-BFD config applied
        out = run(chan, "show config protocols segment-routing | no-more", wait=6)
        print("\nSR config after SH-BFD:")
        for line in out.split('\n'):
            stripped = line.strip()
            if stripped and 'NCP3' not in stripped and 'config-start' not in stripped and 'config-end' not in stripped:
                print(f"  {stripped}")

        # Measure
        result_c = measure_rate(chan, "Phase C: With SH-BFD (SR-MPLS policy)")

        # Remove SH-BFD
        print("\n>> Removing SH-BFD ...")
        run(chan, "configure", wait=3)
        run(chan, "no protocols segment-routing mpls policy test-sbfd", wait=2)
        commit_out = run(chan, "commit", wait=10)
        if 'ERROR' in commit_out.upper():
            print(f"   CLEANUP FAILED: {commit_out}")
            run(chan, "rollback", wait=5)
        else:
            print("   commit: OK")
        run(chan, "top", wait=2)
        run(chan, "exit", wait=2)
        time.sleep(5)

        # Phase D: Post-cleanup
        result_d = measure_rate(chan, "Phase D: Post-cleanup baseline")

        # Summary
        print(f"\n{'='*60}")
        print("  RESULTS (Phase C & D)")
        print(f"{'='*60}")
        for label, r in [("C (SH-BFD)", result_c), ("D (Post-cleanup)", result_d)]:
            if r is not None:
                status = "INCREMENTING" if r > 0 else "NOT incrementing"
                print(f"  Phase {label}: {r:,} pkts ({r/MEASURE_SECONDS:,.0f} pps) — {status}")
            else:
                print(f"  Phase {label}: ERROR")

    finally:
        ssh.close()


if __name__ == "__main__":
    main()
