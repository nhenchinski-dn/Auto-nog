#!/usr/bin/env python3
"""Configure two sub-interfaces under ge400-0/0/3 for SW-244114.
Uses correct DNOS syntax: vlan-id (not encapsulation vlan-id).
"""

import paramiko
import time
import re
import sys

HOST = "WKY1C7VD00008P2"
USER = "dnroot"
PASS = "dnroot"

def clean(output):
    output = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', output)
    output = re.sub(r'\r', '', output)
    output = re.sub(r'-- More -- \(Press q to quit\)\s*', '', output)
    return output

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

    # Rollback any pending uncommitted changes
    print("=== Rollback pending changes ===")
    send(chan, "configure")
    send(chan, "rollback", wait=5)
    send(chan, "top")

    # Remove old static routes that pointed via parent interface
    print("\n=== Removing old static routes ===")
    send(chan, "protocols static address-family ipv4-unicast")
    send(chan, "no route 10.1.13.0/30")
    send(chan, "no route 172.16.0.0/24")
    send(chan, "top")
    send(chan, "protocols static address-family ipv6-unicast")
    send(chan, "no route 2001:db8:13::/64")
    send(chan, "no route 2001:db8:500::/64")
    send(chan, "top")

    # Create ge400-0/0/3.10 (VLAN 10, ingress, uRPF strict)
    print("\n=== Creating ge400-0/0/3.10 (VLAN 10, ingress, uRPF strict) ===")
    send(chan, "interfaces ge400-0/0/3.10")
    send(chan, "admin-state enabled")
    send(chan, "vlan-id 10")
    send(chan, "l2-service disabled")
    send(chan, "ipv4-address 192.168.10.1/24")
    send(chan, "ipv6-address 2001:db8:10::1/64")
    send(chan, "urpf")
    send(chan, "admin-state enabled")
    send(chan, "mode strict")
    send(chan, "address-family ipv4")
    send(chan, "admin-state enabled")
    send(chan, "mode strict")
    send(chan, "top")
    send(chan, "interfaces ge400-0/0/3.10 urpf address-family ipv6")
    send(chan, "admin-state enabled")
    send(chan, "mode strict")
    send(chan, "top")

    # Create ge400-0/0/3.20 (VLAN 20, egress, no uRPF)
    print("\n=== Creating ge400-0/0/3.20 (VLAN 20, egress) ===")
    send(chan, "interfaces ge400-0/0/3.20")
    send(chan, "admin-state enabled")
    send(chan, "vlan-id 20")
    send(chan, "l2-service disabled")
    send(chan, "ipv4-address 192.168.20.1/24")
    send(chan, "ipv6-address 2001:db8:20::1/64")
    send(chan, "top")

    # Static routes via sub-interfaces
    print("\n=== Adding static routes via sub-interfaces ===")
    # IPv4 reverse-path: source prefix -> ingress sub-if
    send(chan, "protocols static address-family ipv4-unicast")
    send(chan, "route 10.1.13.0/30")
    send(chan, "next-hop 192.168.10.2")
    send(chan, "top")
    # IPv4 destination -> egress sub-if
    send(chan, "protocols static address-family ipv4-unicast")
    send(chan, "route 172.16.0.0/24")
    send(chan, "next-hop 192.168.20.2")
    send(chan, "top")
    # IPv6 reverse-path: source prefix -> ingress sub-if
    send(chan, "protocols static address-family ipv6-unicast")
    send(chan, "route 2001:db8:13::/64")
    send(chan, "next-hop 2001:db8:10::2")
    send(chan, "top")
    # IPv6 destination -> egress sub-if
    send(chan, "protocols static address-family ipv6-unicast")
    send(chan, "route 2001:db8:500::/64")
    send(chan, "next-hop 2001:db8:20::2")
    send(chan, "top")

    # Commit
    print("\n=== Committing ===")
    result = send(chan, "commit", wait=20)

    if "ERROR" in result:
        print("\n!!! COMMIT FAILED !!!")
        send(chan, "rollback", wait=5)
        send(chan, "exit")
        chan.close()
        ssh.close()
        return

    print("\n=== Commit successful ===")
    send(chan, "exit", wait=3)

    # Verify
    print("\n=== Verifying sub-interfaces ===")
    send(chan, "show interfaces detail ge400-0/0/3.10 | no-more", wait=8)
    send(chan, "show interfaces detail ge400-0/0/3.20 | no-more", wait=8)

    print("\n=== Verifying routes ===")
    send(chan, "show route 10.1.13.0/30 | no-more", wait=5)
    send(chan, "show route 172.16.0.0/24 | no-more", wait=5)
    send(chan, "show route 2001:db8:13::/64 | no-more", wait=5)
    send(chan, "show route 2001:db8:500::/64 | no-more", wait=5)

    send(chan, "exit", wait=2)
    chan.close()
    ssh.close()
    print("\n=== Done ===")

if __name__ == "__main__":
    main()
