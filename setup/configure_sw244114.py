#!/usr/bin/env python3
"""Configure DUT for SW-244114: Strict uRPF line-rate forwarding test."""

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

def run(chan, cmd, wait=8):
    chan.send(cmd + '\n')
    time.sleep(wait)
    out = b''
    while chan.recv_ready():
        out += chan.recv(65535)
    return clean(out.decode(errors='replace'))

def main():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASS, timeout=30,
                look_for_keys=False, allow_agent=False)
    chan = ssh.invoke_shell(width=300, height=5000)
    time.sleep(6)
    chan.recv(65535)

    print("=== Entering config mode ===")
    print(run(chan, "configure"))

    # IPv4 static routes
    print("=== Adding IPv4 static routes ===")
    print(run(chan, "protocols static address-family ipv4-unicast"))

    # Reverse-path route for source prefix 10.1.13.0/30
    print(run(chan, "route 10.1.13.0/30"))
    print(run(chan, "next-hop 10.0.0.2"))
    print(run(chan, "top"))

    # Hairpin destination route 172.16.0.0/24
    print(run(chan, "protocols static address-family ipv4-unicast"))
    print(run(chan, "route 172.16.0.0/24"))
    print(run(chan, "next-hop 10.0.0.2"))
    print(run(chan, "top"))

    # IPv6 static routes
    print("=== Adding IPv6 static routes ===")
    print(run(chan, "protocols static address-family ipv6-unicast"))

    # Reverse-path route for source prefix 2001:db8:13::/64
    print(run(chan, "route 2001:db8:13::/64"))
    print(run(chan, "next-hop 2001:db8:3::2"))
    print(run(chan, "top"))

    # Hairpin destination route 2001:db8:500::/64
    print(run(chan, "protocols static address-family ipv6-unicast"))
    print(run(chan, "route 2001:db8:500::/64"))
    print(run(chan, "next-hop 2001:db8:3::2"))
    print(run(chan, "top"))

    # Commit
    print("=== Committing ===")
    result = run(chan, "commit", wait=15)
    print(result)

    if "ERROR" in result or "error" in result.lower():
        print("!!! COMMIT FAILED !!!")
        print(run(chan, "rollback"))
    else:
        print("=== Commit successful ===")

    print(run(chan, "exit"))

    # Verify routes
    print("=== Verifying routes ===")
    print(run(chan, "show route 10.1.13.0/30 | no-more"))
    print(run(chan, "show route 172.16.0.0/24 | no-more"))
    print(run(chan, "show route 2001:db8:13::/64 | no-more"))
    print(run(chan, "show route 2001:db8:500::/64 | no-more"))

    # Verify uRPF state
    print("=== Verifying uRPF on ge400-0/0/3 ===")
    print(run(chan, "show interfaces detail ge400-0/0/3 | no-more", wait=10))

    print(run(chan, "exit"))
    chan.close()
    ssh.close()
    print("=== Done ===")

if __name__ == "__main__":
    main()
