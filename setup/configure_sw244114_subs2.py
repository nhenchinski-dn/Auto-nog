#!/usr/bin/env python3
"""Configure two sub-interfaces under ge400-0/0/3 for SW-244114."""

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
    print("Connected.")

    config_cmds = [
        "configure",
        # Remove old static routes
        "protocols static address-family ipv4-unicast",
        "no route 10.1.13.0/30",
        "no route 172.16.0.0/24",
        "top",
        "protocols static address-family ipv6-unicast",
        "no route 2001:db8:13::/64",
        "no route 2001:db8:500::/64",
        "top",
        # Create ge400-0/0/3.10 (VLAN 10, ingress, uRPF strict)
        "interfaces ge400-0/0/3.10",
        "admin-state enabled",
        "encapsulation vlan-id 10",
        "ipv4-address 192.168.10.1/24",
        "ipv6-address 2001:db8:10::1/64",
        "urpf",
        "admin-state enabled",
        "mode strict",
        "address-family ipv4",
        "admin-state enabled",
        "mode strict",
        "top",
        "interfaces ge400-0/0/3.10 urpf address-family ipv6",
        "admin-state enabled",
        "mode strict",
        "top",
        # Create ge400-0/0/3.20 (VLAN 20, egress)
        "interfaces ge400-0/0/3.20",
        "admin-state enabled",
        "encapsulation vlan-id 20",
        "ipv4-address 192.168.20.1/24",
        "ipv6-address 2001:db8:20::1/64",
        "top",
        # Static routes via sub-interfaces
        "protocols static address-family ipv4-unicast",
        "route 10.1.13.0/30",
        "next-hop 192.168.10.2",
        "top",
        "protocols static address-family ipv4-unicast",
        "route 172.16.0.0/24",
        "next-hop 192.168.20.2",
        "top",
        "protocols static address-family ipv6-unicast",
        "route 2001:db8:13::/64",
        "next-hop 2001:db8:10::2",
        "top",
        "protocols static address-family ipv6-unicast",
        "route 2001:db8:500::/64",
        "next-hop 2001:db8:20::2",
        "top",
    ]

    for cmd in config_cmds:
        send(chan, cmd, wait=2)

    print("\n=== Committing ===")
    send(chan, "commit", wait=20)

    send(chan, "exit", wait=3)

    # Verify
    print("\n=== Verification ===")
    verify_cmds = [
        "show interfaces detail ge400-0/0/3.10 | no-more",
        "show interfaces detail ge400-0/0/3.20 | no-more",
        "show route 10.1.13.0/30 | no-more",
        "show route 172.16.0.0/24 | no-more",
        "show route 2001:db8:13::/64 | no-more",
        "show route 2001:db8:500::/64 | no-more",
    ]
    for cmd in verify_cmds:
        send(chan, cmd, wait=5)

    send(chan, "exit", wait=2)
    chan.close()
    ssh.close()
    print("\n=== Done ===")

if __name__ == "__main__":
    main()
