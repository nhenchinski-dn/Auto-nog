#!/usr/bin/env python3
"""Configure DUT for SW-244116: Strict uRPF on customer sub-interface.

Steps:
  1. Remove ge400-0/0/3 from bundle-10
  2. Create sub-interface ge400-0/0/3.100 (VLAN 100) with IPv4/IPv6
  3. Enable uRPF strict on ge400-0/0/3.100
  4. Add static routes for valid (via ingress) and invalid (via other IF) source prefixes
"""

import paramiko
import time
import re
import sys

HOST = "WKY1C7VD00008P2"
USER = "dnroot"
PASS = "dnroot"


def clean(output: str) -> str:
    output = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', output)
    output = re.sub(r'\r', '', output)
    output = re.sub(r'-- More -- \(Press q to quit\)\s*', '', output)
    return output


def send(chan, cmd, wait=6):
    chan.send(cmd + '\n')
    time.sleep(wait)
    buf = b''
    while chan.recv_ready():
        buf += chan.recv(65535)
    text = clean(buf.decode(errors='replace'))
    print(f">>> {cmd}")
    for line in text.strip().split('\n'):
        if line.strip():
            print(f"    {line}")
    return text


def commit_or_fail(chan, label):
    print(f"\n{'=' * 60}")
    print(f"COMMITTING: {label}")
    print("=" * 60)
    out = send(chan, "commit", wait=20)
    if "error" in out.lower() or "fail" in out.lower():
        print(f"\n*** COMMIT FAILED ({label}) — check output above ***")
        send(chan, "rollback")
        send(chan, "exit")
        return False
    print(f"Commit OK: {label}")
    return True


def main():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"Connecting to {HOST}...")
    client.connect(HOST, username=USER, password=PASS,
                   look_for_keys=False, allow_agent=False, timeout=30)

    chan = client.invoke_shell(width=300, height=5000)
    time.sleep(6)
    chan.recv(65535)
    print("Connected.\n")

    # ── Commit 1: Remove ge400-0/0/3 from bundle-10 ──
    print("=" * 60)
    print("COMMIT 1: Remove ge400-0/0/3 from bundle-10")
    print("=" * 60)
    send(chan, "configure")
    send(chan, "interfaces")
    send(chan, "ge400-0/0/3")
    send(chan, "no bundle-id")
    send(chan, "top")

    if not commit_or_fail(chan, "unbundle ge400-0/0/3"):
        client.close()
        sys.exit(1)

    # ── Commit 2: Sub-interface, uRPF, and static routes ──
    print("\n" + "=" * 60)
    print("COMMIT 2: Create sub-interface + uRPF + routes")
    print("=" * 60)

    # Sub-interface ge400-0/0/3.100
    send(chan, "interfaces")
    send(chan, "ge400-0/0/3.100")
    send(chan, "vlan-id 100")
    send(chan, "admin-state enabled")
    send(chan, "ipv4-address 10.3.100.1/24")
    send(chan, "ipv6-address 2001:db8:3:100::1/64")

    # uRPF strict on the sub-interface
    send(chan, "urpf")
    send(chan, "admin-state enabled")
    send(chan, "mode strict")
    send(chan, "top")

    # IPv4 valid: 192.168.50.0/24 -> 10.3.100.2 (via ge400-0/0/3.100 — RPF match)
    send(chan, "protocols")
    send(chan, "static")
    send(chan, "address-family ipv4-unicast")
    send(chan, "route 192.168.50.0/24")
    send(chan, "next-hop 10.3.100.2")
    send(chan, "top")

    # IPv4 invalid: 192.168.60.0/24 -> 10.33.0.2 (via ge400-0/0/33 — RPF mismatch)
    send(chan, "protocols")
    send(chan, "static")
    send(chan, "address-family ipv4-unicast")
    send(chan, "route 192.168.60.0/24")
    send(chan, "next-hop 10.33.0.2")
    send(chan, "top")

    # IPv6 valid: 2001:db8:50::/48 -> 2001:db8:3:100::2 (via ge400-0/0/3.100)
    send(chan, "protocols")
    send(chan, "static")
    send(chan, "address-family ipv6-unicast")
    send(chan, "route 2001:db8:50::/48")
    send(chan, "next-hop 2001:db8:3:100::2")
    send(chan, "top")

    # IPv6 invalid: 2001:db8:60::/48 -> 2001:db8:33::2 (via ge400-0/0/33)
    send(chan, "protocols")
    send(chan, "static")
    send(chan, "address-family ipv6-unicast")
    send(chan, "route 2001:db8:60::/48")
    send(chan, "next-hop 2001:db8:33::2")
    send(chan, "top")

    if not commit_or_fail(chan, "sub-interface + uRPF + routes"):
        client.close()
        sys.exit(1)

    # ── Verification ──
    print("\n" + "=" * 60)
    print("VERIFICATION")
    print("=" * 60)
    send(chan, "exit")
    send(chan, "show config interfaces ge400-0/0/3 | no-more", wait=8)
    send(chan, "show config interfaces ge400-0/0/3.100 | no-more", wait=8)
    send(chan, "show interfaces detail ge400-0/0/3.100 | no-more", wait=10)
    send(chan, "show route 192.168.50.0/24 | no-more", wait=8)
    send(chan, "show route 192.168.60.0/24 | no-more", wait=8)
    send(chan, "show route 2001:db8:50::/48 | no-more", wait=8)
    send(chan, "show route 2001:db8:60::/48 | no-more", wait=8)

    send(chan, "exit")
    client.close()
    print("\nDone. DUT is configured for SW-244116 testing.")


if __name__ == "__main__":
    main()
