#!/usr/bin/env python3
"""SW-244107: Test Seamless BFD (SH-BFD) + egress ACL coexistence.

Adds an SR-MPLS policy with seamless-bfd on the device that already
has egress ACL 'egress-bfd' on bundle-10.

Test matrix:
  1. Record baseline ACL counters (with uBFD already present)
  2. Add SR-MPLS policy + seamless-bfd → commit
  3. Ping to generate egress traffic → check ACL counters
  4. Reverse order: remove SBFD, remove ACL, add SBFD first, then ACL
  5. Cleanup
"""

import paramiko, time, re, sys
from datetime import datetime, timezone

HOST = "WKY1C7VD00008P2"
USER = "dnroot"
PASS = "dnroot"

BUNDLE = "bundle-10"
ACL_NAME = "egress-bfd"
SR_POLICY = "TEST_SBFD_POLICY"
SR_DEST = "20.0.1.1"
SR_COLOR = "100"

def clean(t):
    t = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', t)
    t = re.sub(r'\r', '', t)
    t = re.sub(r'-- More -- \(Press q to quit\)\s*', '', t)
    return t

def recv_all(chan, timeout=8):
    end = time.time() + timeout
    buf = b''
    while time.time() < end:
        if chan.recv_ready():
            buf += chan.recv(65535)
            end = time.time() + 1
        else:
            time.sleep(0.2)
    return clean(buf.decode(errors='replace'))

def run(chan, cmd, wait=3, timeout=10):
    chan.send(cmd + '\n')
    time.sleep(wait)
    return recv_all(chan, timeout=timeout)

def show(chan, cmd, wait=5):
    full = cmd if '| no-more' in cmd else cmd + ' | no-more'
    out = run(chan, full, wait=wait, timeout=12)
    print(f"\n--- {cmd} ---")
    for line in out.strip().split('\n'):
        s = line.strip()
        if s and not s.endswith('#'):
            print(f"  {line}")
    return out

def commit(chan, label=""):
    print(f"\n>>> COMMIT [{label}]")
    out = run(chan, "commit", wait=25, timeout=30)
    is_err = bool(re.search(r'(?i)error|fail|reject|abort', out))
    status = "ERROR" if is_err else "OK"
    print(f"  Result: {status}")
    for line in out.strip().split('\n'):
        s = line.strip()
        if s and not s.endswith('#'):
            print(f"    {line}")
    return (not is_err, out)

def top(chan):
    run(chan, "top", wait=1)

def banner(text):
    print(f"\n{'='*60}")
    print(f"  {text}")
    print(f"{'='*60}")

def add_sbfd_policy(chan):
    """Configure SR-MPLS policy with seamless-bfd."""
    run(chan, "protocols segment-routing mpls", wait=2)
    run(chan, f"policy {SR_POLICY}", wait=2)
    run(chan, f"destination {SR_DEST} strict-spf", wait=2)
    run(chan, f"color {SR_COLOR}", wait=2)
    run(chan, "seamless-bfd", wait=2)
    run(chan, "admin-state enabled", wait=2)
    run(chan, f"remote-reflector-discriminator {SR_DEST}", wait=2)
    run(chan, "interval 300", wait=2)
    run(chan, "multiplier 3", wait=2)
    top(chan)

def remove_sbfd_policy(chan):
    """Remove the SR-MPLS policy."""
    run(chan, "protocols segment-routing mpls", wait=2)
    run(chan, f"no policy {SR_POLICY}", wait=2)
    top(chan)

def add_egress_acl(chan):
    run(chan, f"interfaces {BUNDLE}", wait=2)
    run(chan, f"access-list ipv4 {ACL_NAME} direction out", wait=2)
    top(chan)

def remove_egress_acl(chan):
    run(chan, f"interfaces {BUNDLE}", wait=2)
    run(chan, f"no access-list ipv4 {ACL_NAME}", wait=2)
    top(chan)

def do_ping(chan, count=10):
    print(f"\n  >>> Pinging 20.0.0.2 ({count} pings)...")
    out = run(chan, f"ping 20.0.0.2 count {count}", wait=15, timeout=20)
    for line in out.strip().split('\n'):
        if 'packet' in line.lower() or 'transmitted' in line.lower() or 'round-trip' in line.lower() or '%' in line:
            print(f"    {line.strip()}")
    return out

def main():
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"SW-244107 SH-BFD Test — {ts}")
    print(f"Device: {HOST}")
    print("=" * 60)

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print("Connecting...")
    ssh.connect(HOST, username=USER, password=PASS, timeout=30,
                look_for_keys=False, allow_agent=False)
    chan = ssh.invoke_shell(width=400, height=5000)
    time.sleep(6)
    chan.recv(65535)
    print("Connected.\n")

    # ============================================================
    # STEP 0: Record system + current state
    # ============================================================
    banner("STEP 0: Current state (uBFD + ACL already active)")
    show(chan, "show system")
    show(chan, "show config interfaces bundle-10")
    show(chan, "show config protocols bfd")
    show(chan, "show bfd sessions")
    show(chan, "show bfd summary")
    show(chan, "show access-lists counters")

    # ============================================================
    # COMBO 1: Egress ACL already present → ADD SH-BFD
    # ============================================================
    banner("COMBO 1: Egress ACL present → ADD SR-MPLS seamless-bfd")
    print("  ACL is already on bundle-10. Adding SR-MPLS policy + seamless-bfd...")

    run(chan, "configure", wait=2)
    add_sbfd_policy(chan)

    ok_c1, out_c1 = commit(chan, "Combo 1: add SH-BFD with ACL present")
    if not ok_c1:
        print("  ** COMMIT REJECTED — documenting as non-support evidence **")
        run(chan, "rollback", wait=5)
    run(chan, "exit", wait=2)

    time.sleep(10)

    show(chan, "show bfd sessions")
    show(chan, "show bfd summary")
    show(chan, f"show segment-routing policy")
    show(chan, "show config protocols segment-routing mpls")

    print("\n  --- ACL counters BEFORE ping ---")
    show(chan, "show access-lists counters")

    do_ping(chan, 10)

    print("\n  --- ACL counters AFTER ping ---")
    show(chan, "show access-lists counters")

    # ============================================================
    # CLEANUP: remove SH-BFD policy + remove ACL for reverse test
    # ============================================================
    banner("SETUP for COMBO 2: Remove everything, start with SH-BFD first")

    run(chan, "configure", wait=2)
    remove_egress_acl(chan)
    remove_sbfd_policy(chan)
    ok, _ = commit(chan, "Remove ACL + SBFD for combo 2")
    if not ok:
        run(chan, "rollback", wait=5)
    run(chan, "exit", wait=2)

    time.sleep(5)

    # Now add SH-BFD FIRST (no ACL)
    run(chan, "configure", wait=2)
    add_sbfd_policy(chan)
    ok_sbfd_only, _ = commit(chan, "Add SH-BFD only (no ACL)")
    if not ok_sbfd_only:
        print("  ** SH-BFD alone failed to commit **")
        run(chan, "rollback", wait=5)
    run(chan, "exit", wait=2)

    time.sleep(10)
    show(chan, "show bfd sessions")
    show(chan, "show bfd summary")

    # ============================================================
    # COMBO 2: SH-BFD already present → ADD egress ACL
    # ============================================================
    banner("COMBO 2: SH-BFD present → ADD egress ACL to bundle-10")

    run(chan, "configure", wait=2)
    add_egress_acl(chan)
    ok_c2, out_c2 = commit(chan, "Combo 2: add ACL with SH-BFD present")
    if not ok_c2:
        print("  ** COMMIT REJECTED — documenting as non-support evidence **")
        run(chan, "rollback", wait=5)
    run(chan, "exit", wait=2)

    time.sleep(5)

    print("\n  --- ACL counters BEFORE ping ---")
    show(chan, "show access-lists counters")

    do_ping(chan, 10)

    print("\n  --- ACL counters AFTER ping ---")
    show(chan, "show access-lists counters")

    # ============================================================
    # RESTORE: keep ACL + uBFD, remove SBFD policy
    # ============================================================
    banner("RESTORE: Remove SR-MPLS policy, keep ACL + uBFD")

    run(chan, "configure", wait=2)
    remove_sbfd_policy(chan)
    # Make sure uBFD is back on bundle-10
    run(chan, "protocols bfd", wait=2)
    run(chan, f"interface {BUNDLE}", wait=2)
    run(chan, "local-address 20.0.0.1", wait=2)
    run(chan, "neighbor 20.0.1.1", wait=2)
    top(chan)
    # Make sure bundle-20 BFD is also there
    run(chan, "protocols bfd", wait=2)
    run(chan, "interface bundle-20", wait=2)
    run(chan, "local-address 20.0.1.1", wait=2)
    run(chan, "neighbor 20.0.0.1", wait=2)
    top(chan)
    ok, _ = commit(chan, "Restore uBFD + ACL, remove SBFD")
    if not ok:
        run(chan, "rollback", wait=5)
    run(chan, "exit", wait=2)

    show(chan, "show config protocols bfd")
    show(chan, f"show config interfaces {BUNDLE}")
    show(chan, "show bfd sessions")

    # ============================================================
    # SUMMARY
    # ============================================================
    banner("SUMMARY — SW-244107: Egress ACL + SH-BFD (Seamless BFD)")
    print(f"  Combo 1 (ACL present → add SH-BFD):   commit={'OK' if ok_c1 else 'REJECTED'}")
    print(f"  Combo 2 (SH-BFD present → add ACL):    commit={'OK' if ok_c2 else 'REJECTED'}")
    print(f"  SH-BFD alone (no ACL):                 commit={'OK' if ok_sbfd_only else 'REJECTED'}")
    print(f"\n  Execution: {ts}")
    print(f"  Device: {HOST} (NCP3-nog)")

    run(chan, "exit", wait=2)
    chan.close()
    ssh.close()
    print("\n=== Done ===")

if __name__ == "__main__":
    main()
