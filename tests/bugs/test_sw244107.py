#!/usr/bin/env python3
"""SW-244107: Egress ACL + SH-BFD/uBFD | Verify non-support.

Tests whether egress ACL and Seamless BFD can coexist on the same
forwarding path, documenting the failure mode.
"""

import paramiko
import time
import re
import sys

HOST = "WKY1C7VD00008P2"
USER = "dnroot"
PASS = "dnroot"
IF_CUST = "ge400-0/0/3.10"
ACL_NAME = "TEST_EGRESS_ACL_V4"
SR_POLICY = "TEST_SR_POLICY_1"

def clean(o):
    o = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', o)
    o = re.sub(r'\r', '', o)
    o = re.sub(r'-- More -- \(Press q to quit\)\s*', '', o)
    return o

def recv_all(chan, timeout=5):
    end = time.time() + timeout
    out = b''
    while time.time() < end:
        if chan.recv_ready():
            out += chan.recv(65535)
            end = time.time() + 1
        else:
            time.sleep(0.2)
    return clean(out.decode(errors='replace'))

def send(chan, cmd, wait=3):
    sys.stdout.write(f">>> {cmd}\n")
    sys.stdout.flush()
    chan.send(cmd + '\n')
    time.sleep(wait)
    out = recv_all(chan, timeout=2)
    if out.strip():
        print(out.strip())
    return out

def main():
    print("Connecting...")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASS, timeout=30,
                look_for_keys=False, allow_agent=False)
    chan = ssh.invoke_shell(width=300, height=5000)
    time.sleep(6)
    chan.recv(65535)
    print("Connected.\n")

    # ============================================================
    # STEP 1: System version (already captured, but repeat)
    # ============================================================
    print("=" * 60)
    print("STEP 1: System Version")
    print("=" * 60)
    send(chan, "show system | no-more", wait=5)

    # ============================================================
    # BASELINE A: SR-MPLS policy + seamless-bfd ONLY (no ACL)
    # ============================================================
    print("\n" + "=" * 60)
    print("BASELINE A: Configure SR-MPLS policy + seamless-bfd ONLY")
    print("=" * 60)

    send(chan, "configure")

    # Create SR-MPLS policy with seamless-bfd
    send(chan, "protocols segment-routing mpls")
    send(chan, f"policy {SR_POLICY}")
    send(chan, "destination 192.168.20.2")
    send(chan, "color 100")
    send(chan, "seamless-bfd")
    send(chan, "admin-state enabled")
    send(chan, "remote-reflector-discriminator 192.168.20.2")
    send(chan, "reverse-path-label 16001")
    send(chan, "interval 300")
    send(chan, "multiplier 3")
    send(chan, "top")

    print("\n--- Committing Baseline A (BFD only) ---")
    result_a = send(chan, "commit", wait=15)
    print(f"\n*** BASELINE A COMMIT RESULT: {'ERROR' if 'ERROR' in result_a or 'error' in result_a.lower() else 'SUCCESS'} ***")

    send(chan, "exit")

    # Verify BFD
    print("\n--- Verifying BFD sessions ---")
    send(chan, "show bfd sessions | no-more", wait=5)
    send(chan, "show bfd summary | no-more", wait=5)
    send(chan, f"show config protocols segment-routing | no-more", wait=5)

    # ============================================================
    # BASELINE B: Egress ACL ONLY (remove BFD first)
    # ============================================================
    print("\n" + "=" * 60)
    print("BASELINE B: Remove BFD, configure egress ACL ONLY")
    print("=" * 60)

    send(chan, "configure")

    # Remove SR policy
    send(chan, "protocols segment-routing mpls")
    send(chan, f"no policy {SR_POLICY}")
    send(chan, "top")

    # Create ACL
    send(chan, "access-lists")
    send(chan, f"ipv4 {ACL_NAME}")
    send(chan, "rule 100 allow src-ip any dest-ip any")
    send(chan, "top")

    # Attach egress ACL on customer interface
    send(chan, f"interfaces {IF_CUST}")
    send(chan, f"access-list ipv4 {ACL_NAME} direction out")
    send(chan, "top")

    print("\n--- Committing Baseline B (egress ACL only) ---")
    result_b = send(chan, "commit", wait=15)
    print(f"\n*** BASELINE B COMMIT RESULT: {'ERROR' if 'ERROR' in result_b or 'error' in result_b.lower() else 'SUCCESS'} ***")

    send(chan, "exit")

    # Verify ACL
    print("\n--- Verifying egress ACL ---")
    send(chan, f"show config interfaces {IF_CUST} | no-more", wait=5)
    send(chan, f"show access-lists ipv4 {ACL_NAME} | no-more", wait=5)

    # ============================================================
    # COMBINATION 1: ACL already on, add BFD
    # ============================================================
    print("\n" + "=" * 60)
    print("COMBINATION ATTEMPT 1: Egress ACL present, ADD SH-BFD")
    print("=" * 60)

    send(chan, "configure")

    # Add SR-MPLS policy with seamless-bfd
    send(chan, "protocols segment-routing mpls")
    send(chan, f"policy {SR_POLICY}")
    send(chan, "destination 192.168.20.2")
    send(chan, "color 100")
    send(chan, "seamless-bfd")
    send(chan, "admin-state enabled")
    send(chan, "remote-reflector-discriminator 192.168.20.2")
    send(chan, "reverse-path-label 16001")
    send(chan, "interval 300")
    send(chan, "multiplier 3")
    send(chan, "top")

    print("\n--- Committing Combination 1 (ACL present + add BFD) ---")
    result_c1 = send(chan, "commit", wait=15)
    print(f"\n*** COMBINATION 1 COMMIT RESULT: {'ERROR' if 'ERROR' in result_c1 or 'error' in result_c1.lower() else 'SUCCESS'} ***")

    if "ERROR" in result_c1 or "error" in result_c1.lower():
        print("--- Rolling back ---")
        send(chan, "rollback", wait=5)

    send(chan, "exit")

    # Capture state
    send(chan, "show bfd sessions | no-more", wait=5)
    send(chan, f"show config interfaces {IF_CUST} | no-more", wait=5)

    # ============================================================
    # CLEANUP for Combination 2 setup
    # ============================================================
    print("\n" + "=" * 60)
    print("CLEANUP: Remove ACL, set up BFD first for Combination 2")
    print("=" * 60)

    send(chan, "configure")

    # Remove ACL from interface
    send(chan, f"interfaces {IF_CUST}")
    send(chan, "no access-list ipv4")
    send(chan, "top")

    # Remove ACL definition
    send(chan, "access-lists")
    send(chan, f"no ipv4 {ACL_NAME}")
    send(chan, "top")

    # Remove SR policy if it was rolled back
    send(chan, "protocols segment-routing mpls")
    send(chan, f"no policy {SR_POLICY}")
    send(chan, "top")

    send(chan, "commit", wait=15)
    send(chan, "top")

    # Now set up BFD FIRST for Combination 2
    send(chan, "protocols segment-routing mpls")
    send(chan, f"policy {SR_POLICY}")
    send(chan, "destination 192.168.20.2")
    send(chan, "color 100")
    send(chan, "seamless-bfd")
    send(chan, "admin-state enabled")
    send(chan, "remote-reflector-discriminator 192.168.20.2")
    send(chan, "reverse-path-label 16001")
    send(chan, "interval 300")
    send(chan, "multiplier 3")
    send(chan, "top")

    send(chan, "commit", wait=15)
    send(chan, "exit")

    # ============================================================
    # COMBINATION 2: BFD already on, add egress ACL
    # ============================================================
    print("\n" + "=" * 60)
    print("COMBINATION ATTEMPT 2: SH-BFD present, ADD egress ACL")
    print("=" * 60)

    send(chan, "configure")

    # Create ACL
    send(chan, "access-lists")
    send(chan, f"ipv4 {ACL_NAME}")
    send(chan, "rule 100 allow src-ip any dest-ip any")
    send(chan, "top")

    # Attach egress ACL
    send(chan, f"interfaces {IF_CUST}")
    send(chan, f"access-list ipv4 {ACL_NAME} direction out")
    send(chan, "top")

    print("\n--- Committing Combination 2 (BFD present + add egress ACL) ---")
    result_c2 = send(chan, "commit", wait=15)
    print(f"\n*** COMBINATION 2 COMMIT RESULT: {'ERROR' if 'ERROR' in result_c2 or 'error' in result_c2.lower() else 'SUCCESS'} ***")

    if "ERROR" in result_c2 or "error" in result_c2.lower():
        print("--- Rolling back ---")
        send(chan, "rollback", wait=5)

    send(chan, "exit")

    # Capture final state
    print("\n--- Final state ---")
    send(chan, "show bfd sessions | no-more", wait=5)
    send(chan, f"show config interfaces {IF_CUST} | no-more", wait=5)
    send(chan, f"show access-lists ipv4 {ACL_NAME} | no-more", wait=5)

    # ============================================================
    # FULL CLEANUP
    # ============================================================
    print("\n" + "=" * 60)
    print("FULL CLEANUP")
    print("=" * 60)

    send(chan, "configure")
    send(chan, f"interfaces {IF_CUST}")
    send(chan, "no access-list ipv4")
    send(chan, "top")
    send(chan, "access-lists")
    send(chan, f"no ipv4 {ACL_NAME}")
    send(chan, "top")
    send(chan, "protocols segment-routing mpls")
    send(chan, f"no policy {SR_POLICY}")
    send(chan, "top")
    result_cleanup = send(chan, "commit", wait=15)
    print(f"\n*** CLEANUP COMMIT: {'ERROR' if 'ERROR' in result_cleanup or 'error' in result_cleanup.lower() else 'SUCCESS'} ***")
    send(chan, "exit")

    send(chan, "exit")
    chan.close()
    ssh.close()

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Baseline A (BFD only):              {'ERROR' if 'ERROR' in result_a else 'COMMIT OK'}")
    print(f"Baseline B (egress ACL only):        {'ERROR' if 'ERROR' in result_b else 'COMMIT OK'}")
    print(f"Combination 1 (ACL + add BFD):       {'ERROR/REJECTED' if 'ERROR' in result_c1 or 'error' in result_c1.lower() else 'COMMIT OK'}")
    print(f"Combination 2 (BFD + add ACL):       {'ERROR/REJECTED' if 'ERROR' in result_c2 or 'error' in result_c2.lower() else 'COMMIT OK'}")
    print("\n=== Done ===")

if __name__ == "__main__":
    main()
