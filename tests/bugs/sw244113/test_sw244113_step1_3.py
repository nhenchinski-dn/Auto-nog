#!/usr/bin/env python3
"""SW-244113: Steps 1-3 — Create VRF, sub-interfaces, uRPF strict, static routes."""

import paramiko
import time
import re
import json
import sys

HOST = "100.64.8.59"
USER = "dnroot"
PASS = "dnroot"
RESULTS = {}

def clean(text):
    text = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)
    text = re.sub(r'\r', '', text)
    text = re.sub(r'-- More -- \(Press q to quit\)\s*', '', text)
    return text.strip()

def connect():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASS, timeout=30,
                look_for_keys=False, allow_agent=False)
    chan = ssh.invoke_shell(width=300, height=5000)
    time.sleep(6)
    chan.recv(65535)
    return ssh, chan

def run(chan, cmd, wait=8):
    chan.send(cmd + '\n')
    time.sleep(wait)
    out = b''
    while chan.recv_ready():
        out += chan.recv(65535)
        time.sleep(0.5)
    return clean(out.decode(errors='replace'))

def run_and_print(chan, cmd, wait=8):
    output = run(chan, cmd, wait)
    print(f"  [{cmd}]")
    for line in output.split('\n'):
        print(f"    {line}")
    return output

def save(filename):
    with open(filename, 'w') as f:
        json.dump(RESULTS, f, indent=2)

def main():
    print(f"Connecting to {HOST} (wky1c7vd00008p2 / NCP3-nog)...")
    ssh, chan = connect()

    # =========================================================================
    # STEP 1: Create VRF urpf-vrf, create sub-interfaces, assign IPs
    # =========================================================================
    print("\n" + "="*70)
    print("STEP 1: Create VRF, sub-interfaces, assign IPv4/IPv6 addresses")
    print("="*70)

    config_cmds_step1 = [
        "configure",
        # Create ge400-0/0/3.100 sub-interface with VLAN 100
        "interfaces ge400-0/0/3.100 encapsulation dot1q vlan-id 100",
        "top",
        "interfaces ge400-0/0/3.100 ipv4-address 10.100.1.1/24",
        "top",
        "interfaces ge400-0/0/3.100 ipv6-address 2001:db8:100::1/64",
        "top",
        # Create bundle-10.100 sub-interface with VLAN 100
        "interfaces bundle-10.100 encapsulation dot1q vlan-id 100",
        "top",
        "interfaces bundle-10.100 ipv4-address 10.100.2.1/24",
        "top",
        "interfaces bundle-10.100 ipv6-address 2001:db8:200::1/64",
        "top",
        # Create VRF urpf-vrf and attach both sub-interfaces
        "network-services vrf instance urpf-vrf",
        "interface ge400-0/0/3.100",
        "interface bundle-10.100",
        "top",
        "commit",
    ]

    for cmd in config_cmds_step1:
        output = run_and_print(chan, cmd, wait=6 if cmd != "commit" else 15)
        if "ERROR" in output or "error" in output.lower():
            if "already" not in output.lower():
                print(f"  *** POTENTIAL ERROR in: {cmd} ***")

    # Verify Step 1
    print("\n--- Step 1 Verification ---")
    v1_vrf = run_and_print(chan, "show network-services vrf | no-more", 10)
    v1_ge_sub = run_and_print(chan, "show interfaces detail ge400-0/0/3.100 | no-more", 12)
    v1_bun_sub = run_and_print(chan, "show interfaces detail bundle-10.100 | no-more", 12)
    v1_vrf_cfg = run_and_print(chan, "show config network-services vrf instance urpf-vrf | no-more", 10)

    RESULTS["step1"] = {
        "vrf_list": v1_vrf,
        "ge_sub_detail": v1_ge_sub,
        "bundle_sub_detail": v1_bun_sub,
        "vrf_config": v1_vrf_cfg,
    }

    step1_pass = ("urpf-vrf" in v1_vrf and
                  "ge400-0/0/3.100" in v1_ge_sub and
                  "bundle-10.100" in v1_bun_sub)
    print(f"\n>>> STEP 1 RESULT: {'PASS' if step1_pass else 'FAIL'}")
    RESULTS["step1"]["result"] = "PASS" if step1_pass else "FAIL"

    # =========================================================================
    # STEP 2: Configure uRPF strict on ge400-0/0/3.100 within VRF
    # =========================================================================
    print("\n" + "="*70)
    print("STEP 2: Configure uRPF strict on ge400-0/0/3.100")
    print("="*70)

    config_cmds_step2 = [
        "configure",
        "interfaces ge400-0/0/3.100 urpf admin-state enabled",
        "top",
        "interfaces ge400-0/0/3.100 urpf mode strict",
        "top",
        "commit",
    ]

    for cmd in config_cmds_step2:
        output = run_and_print(chan, cmd, wait=6 if cmd != "commit" else 15)
        if "ERROR" in output or "error" in output.lower():
            if "already" not in output.lower():
                print(f"  *** POTENTIAL ERROR in: {cmd} ***")

    # Verify Step 2
    print("\n--- Step 2 Verification ---")
    v2_detail = run_and_print(chan, "show interfaces detail ge400-0/0/3.100 | no-more", 12)
    v2_cfg = run_and_print(chan, "show config interfaces ge400-0/0/3.100 urpf | no-more", 10)

    RESULTS["step2"] = {
        "detail": v2_detail,
        "config": v2_cfg,
    }

    step2_pass = ("strict" in v2_detail.lower() and
                  "enabled" in v2_detail.lower() and
                  "uRPF IPv4 check: enabled" in v2_detail and
                  "uRPF IPv6 check: enabled" in v2_detail)
    print(f"\n>>> STEP 2 RESULT: {'PASS' if step2_pass else 'FAIL'}")
    RESULTS["step2"]["result"] = "PASS" if step2_pass else "FAIL"

    # =========================================================================
    # STEP 3: Install static reverse-path routes in VRF
    # =========================================================================
    print("\n" + "="*70)
    print("STEP 3: Install static IPv4/IPv6 reverse-path routes in VRF")
    print("="*70)

    # Routes: valid source 10.100.10.0/24 via ge400-0/0/3.100 (NH: 10.100.1.2)
    #         valid source 2001:db8:10::/64 via ge400-0/0/3.100 (NH: 2001:db8:100::2)
    # Also add a default route in VRF for allow-default testing later (step 11)
    config_cmds_step3 = [
        "configure",
        "network-services vrf instance urpf-vrf protocols static",
        "address-family ipv4-unicast",
        "route 10.100.10.0/24 next-hop 10.100.1.2 interface ge400-0/0/3.100",
        "top",
        "network-services vrf instance urpf-vrf protocols static",
        "address-family ipv6-unicast",
        "route 2001:db8:10::/64 next-hop 2001:db8:100::2 interface ge400-0/0/3.100",
        "top",
        # Also add reverse-path routes for bundle sub-interface (step 7)
        "network-services vrf instance urpf-vrf protocols static",
        "address-family ipv4-unicast",
        "route 10.100.20.0/24 next-hop 10.100.2.2 interface bundle-10.100",
        "top",
        "network-services vrf instance urpf-vrf protocols static",
        "address-family ipv6-unicast",
        "route 2001:db8:20::/64 next-hop 2001:db8:200::2 interface bundle-10.100",
        "top",
        # Default route in VRF via ge sub-if for allow-default testing (step 11)
        "network-services vrf instance urpf-vrf protocols static",
        "address-family ipv4-unicast",
        "route 0.0.0.0/0 next-hop 10.100.1.2 interface ge400-0/0/3.100",
        "top",
        "network-services vrf instance urpf-vrf protocols static",
        "address-family ipv6-unicast",
        "route ::/0 next-hop 2001:db8:100::2 interface ge400-0/0/3.100",
        "top",
        "commit",
    ]

    for cmd in config_cmds_step3:
        output = run_and_print(chan, cmd, wait=6 if cmd != "commit" else 15)
        if "ERROR" in output or "error" in output.lower():
            if "already" not in output.lower():
                print(f"  *** POTENTIAL ERROR in: {cmd} ***")

    # Verify Step 3
    print("\n--- Step 3 Verification ---")
    v3_v4 = run_and_print(chan, "show route vrf urpf-vrf 10.100.10.0/24 | no-more", 10)
    v3_v6 = run_and_print(chan, "show route vrf urpf-vrf 2001:db8:10::/64 | no-more", 10)
    v3_full = run_and_print(chan, "show route vrf urpf-vrf | no-more", 10)
    v3_cfg = run_and_print(chan, "show config network-services vrf instance urpf-vrf protocols static | no-more", 10)

    RESULTS["step3"] = {
        "route_v4": v3_v4,
        "route_v6": v3_v6,
        "route_full": v3_full,
        "static_config": v3_cfg,
    }

    step3_pass = ("10.100.10.0/24" in v3_full and "2001:db8:10::" in v3_full)
    print(f"\n>>> STEP 3 RESULT: {'PASS' if step3_pass else 'FAIL'}")
    RESULTS["step3"]["result"] = "PASS" if step3_pass else "FAIL"

    # Also grab baseline counters for ge400-0/0/3.100
    print("\n--- Baseline counters (ge400-0/0/3.100) ---")
    v_counters = run_and_print(chan, "show interfaces counters ge400-0/0/3.100 | no-more", 10)
    RESULTS["baseline_counters_ge_sub"] = v_counters

    # Exit config mode cleanly
    run(chan, "end", 3)

    chan.close()
    ssh.close()

    save("/home/dn/output/sw244113_steps1_3.json")
    print("\n" + "="*70)
    print("Steps 1-3 complete. Results saved to /home/dn/output/sw244113_steps1_3.json")
    print("="*70)

    for step in ["step1", "step2", "step3"]:
        print(f"  {step}: {RESULTS[step]['result']}")

if __name__ == "__main__":
    main()
