#!/usr/bin/env python3
"""Configure two sub-interfaces under ge400-0/0/3 for SW-244114 uRPF line-rate test.

ge400-0/0/3.10 (VLAN 10) = ingress, uRPF strict enabled
ge400-0/0/3.20 (VLAN 20) = egress, no uRPF

Traffic flow: Spirent TX (VLAN 10) -> DUT uRPF check -> DUT forwards -> Spirent RX (VLAN 20)
"""

import paramiko
import time
import re

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

    print(run(chan, "configure"))

    # --- Remove old static routes that pointed via parent interface ---
    print("=== Removing old static routes ===")
    print(run(chan, "protocols static address-family ipv4-unicast"))
    print(run(chan, "no route 10.1.13.0/30"))
    print(run(chan, "no route 172.16.0.0/24"))
    print(run(chan, "top"))
    print(run(chan, "protocols static address-family ipv6-unicast"))
    print(run(chan, "no route 2001:db8:13::/64"))
    print(run(chan, "no route 2001:db8:500::/64"))
    print(run(chan, "top"))

    # --- Create ge400-0/0/3.10 (VLAN 10) - ingress with uRPF strict ---
    print("=== Creating ge400-0/0/3.10 (VLAN 10, ingress, uRPF strict) ===")
    print(run(chan, "interfaces ge400-0/0/3.10"))
    print(run(chan, "admin-state enabled"))
    print(run(chan, "encapsulation vlan-id 10"))
    print(run(chan, "ipv4-address 192.168.10.1/24"))
    print(run(chan, "ipv6-address 2001:db8:10::1/64"))
    print(run(chan, "urpf"))
    print(run(chan, "admin-state enabled"))
    print(run(chan, "mode strict"))
    print(run(chan, "address-family ipv4"))
    print(run(chan, "admin-state enabled"))
    print(run(chan, "mode strict"))
    print(run(chan, "top"))
    print(run(chan, "interfaces ge400-0/0/3.10 urpf address-family ipv6"))
    print(run(chan, "admin-state enabled"))
    print(run(chan, "mode strict"))
    print(run(chan, "top"))

    # --- Create ge400-0/0/3.20 (VLAN 20) - egress, no uRPF ---
    print("=== Creating ge400-0/0/3.20 (VLAN 20, egress) ===")
    print(run(chan, "interfaces ge400-0/0/3.20"))
    print(run(chan, "admin-state enabled"))
    print(run(chan, "encapsulation vlan-id 20"))
    print(run(chan, "ipv4-address 192.168.20.1/24"))
    print(run(chan, "ipv6-address 2001:db8:20::1/64"))
    print(run(chan, "top"))

    # --- Add new static routes via sub-interfaces ---
    print("=== Adding static routes via sub-interfaces ===")

    # IPv4: reverse-path for source prefix via ingress sub-if
    print(run(chan, "protocols static address-family ipv4-unicast"))
    print(run(chan, "route 10.1.13.0/30"))
    print(run(chan, "next-hop 192.168.10.2"))
    print(run(chan, "top"))

    # IPv4: destination route via egress sub-if
    print(run(chan, "protocols static address-family ipv4-unicast"))
    print(run(chan, "route 172.16.0.0/24"))
    print(run(chan, "next-hop 192.168.20.2"))
    print(run(chan, "top"))

    # IPv6: reverse-path for source prefix via ingress sub-if
    print(run(chan, "protocols static address-family ipv6-unicast"))
    print(run(chan, "route 2001:db8:13::/64"))
    print(run(chan, "next-hop 2001:db8:10::2"))
    print(run(chan, "top"))

    # IPv6: destination route via egress sub-if
    print(run(chan, "protocols static address-family ipv6-unicast"))
    print(run(chan, "route 2001:db8:500::/64"))
    print(run(chan, "next-hop 2001:db8:20::2"))
    print(run(chan, "top"))

    # --- Commit ---
    print("=== Committing ===")
    result = run(chan, "commit", wait=20)
    print(result)

    if "ERROR" in result or "error" in result.lower():
        print("!!! COMMIT FAILED !!!")
        print(run(chan, "rollback"))
        print(run(chan, "exit"))
        chan.close()
        ssh.close()
        return

    print("=== Commit successful ===")
    print(run(chan, "exit"))

    # --- Verify ---
    print("=== Verifying sub-interfaces ===")
    print(run(chan, "show interfaces detail ge400-0/0/3.10 | no-more", wait=10))
    print(run(chan, "show interfaces detail ge400-0/0/3.20 | no-more", wait=10))

    print("=== Verifying routes ===")
    print(run(chan, "show route 10.1.13.0/30 | no-more"))
    print(run(chan, "show route 172.16.0.0/24 | no-more"))
    print(run(chan, "show route 2001:db8:13::/64 | no-more"))
    print(run(chan, "show route 2001:db8:500::/64 | no-more"))

    print(run(chan, "exit"))
    chan.close()
    ssh.close()
    print("=== Done ===")

if __name__ == "__main__":
    main()
