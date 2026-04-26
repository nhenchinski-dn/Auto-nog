#!/usr/bin/env python3
"""SW-244113: Fix Steps 1-3 — Rollback then redo with correct vlan-id syntax."""

import paramiko
import time
import re
import json

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

def main():
    print(f"Connecting to {HOST} (NCP3-nog)...")
    ssh, chan = connect()

    # First rollback any pending config
    print("\n--- Rolling back pending config ---")
    run_and_print(chan, "rollback", 10)

    # =========================================================================
    # STEP 1: Create VRF, sub-interfaces with correct vlan-id, assign IPs
    # =========================================================================
    print("\n" + "="*70)
    print("STEP 1: Create VRF, sub-interfaces, assign IPv4/IPv6 addresses")
    print("="*70)

    config_cmds = [
        ("configure", 5),
        # Create ge400-0/0/3.100 with vlan-id 100
        ("interfaces ge400-0/0/3.100 vlan-id 100", 5),
        ("top", 3),
        ("interfaces ge400-0/0/3.100 ipv4-address 10.100.1.1/24", 5),
        ("top", 3),
        ("interfaces ge400-0/0/3.100 ipv6-address 2001:db8:100::1/64", 5),
        ("top", 3),
        # Create bundle-10.100 with vlan-id 100
        ("interfaces bundle-10.100 vlan-id 100", 5),
        ("top", 3),
        ("interfaces bundle-10.100 ipv4-address 10.100.2.1/24", 5),
        ("top", 3),
        ("interfaces bundle-10.100 ipv6-address 2001:db8:200::1/64", 5),
        ("top", 3),
        # Create VRF urpf-vrf and attach both sub-interfaces
        ("network-services vrf instance urpf-vrf", 5),
        ("interface ge400-0/0/3.100", 5),
        ("interface bundle-10.100", 5),
        ("top", 3),
        ("commit", 20),
    ]

    commit_ok = True
    for cmd, wait in config_cmds:
        output = run_and_print(chan, cmd, wait)
        if "ERROR" in output:
            print(f"  *** ERROR in: {cmd} ***")
            if cmd == "commit":
                commit_ok = False

    # Verify Step 1
    print("\n--- Step 1 Verification ---")
    v1_vrf = run_and_print(chan, "show network-services vrf | no-more", 10)
    run_and_print(chan, "end", 3)
    v1_ge_sub = run_and_print(chan, "show interfaces detail ge400-0/0/3.100 | no-more", 12)
    v1_bun_sub = run_and_print(chan, "show interfaces detail bundle-10.100 | no-more", 12)
    v1_ge_if = run_and_print(chan, "show interfaces ge400-0/0/3.100 | no-more", 10)
    v1_bun_if = run_and_print(chan, "show interfaces bundle-10.100 | no-more", 10)
    v1_vrf_cfg = run_and_print(chan, "show config network-services vrf instance urpf-vrf | no-more", 10)

    RESULTS["step1"] = {
        "vrf_list": v1_vrf,
        "ge_sub_detail": v1_ge_sub,
        "bundle_sub_detail": v1_bun_sub,
        "ge_sub_brief": v1_ge_if,
        "bundle_sub_brief": v1_bun_if,
        "vrf_config": v1_vrf_cfg,
    }

    step1_pass = (commit_ok and "urpf-vrf" in v1_vrf and "10.100.1.1" in v1_ge_sub)
    print(f"\n>>> STEP 1 RESULT: {'PASS' if step1_pass else 'FAIL'}")
    RESULTS["step1"]["result"] = "PASS" if step1_pass else "FAIL"

    # =========================================================================
    # STEP 2: Configure uRPF strict on ge400-0/0/3.100 within VRF
    # =========================================================================
    print("\n" + "="*70)
    print("STEP 2: Configure uRPF strict on ge400-0/0/3.100")
    print("="*70)

    step2_cmds = [
        ("configure", 5),
        ("interfaces ge400-0/0/3.100 urpf admin-state enabled", 5),
        ("top", 3),
        ("interfaces ge400-0/0/3.100 urpf mode strict", 5),
        ("top", 3),
        ("commit", 15),
    ]

    commit_ok2 = True
    for cmd, wait in step2_cmds:
        output = run_and_print(chan, cmd, wait)
        if "ERROR" in output:
            print(f"  *** ERROR in: {cmd} ***")
            if cmd == "commit":
                commit_ok2 = False

    # Verify Step 2
    print("\n--- Step 2 Verification ---")
    run_and_print(chan, "end", 3)
    v2_detail = run_and_print(chan, "show interfaces detail ge400-0/0/3.100 | no-more", 12)
    v2_cfg = run_and_print(chan, "show config interfaces ge400-0/0/3.100 urpf | no-more", 10)

    RESULTS["step2"] = {
        "detail": v2_detail,
        "config": v2_cfg,
    }

    step2_pass = (commit_ok2 and
                  "uRPF IPv4 check: enabled" in v2_detail and
                  "Mode: strict" in v2_detail)
    print(f"\n>>> STEP 2 RESULT: {'PASS' if step2_pass else 'FAIL'}")
    RESULTS["step2"]["result"] = "PASS" if step2_pass else "FAIL"

    # =========================================================================
    # STEP 3: Install static reverse-path routes in VRF
    # =========================================================================
    print("\n" + "="*70)
    print("STEP 3: Install static IPv4/IPv6 reverse-path routes in VRF")
    print("="*70)

    step3_cmds = [
        ("configure", 5),
        # IPv4 reverse-path for ge sub-if source prefix
        ("network-services vrf instance urpf-vrf protocols static address-family ipv4-unicast", 5),
        ("route 10.100.10.0/24 next-hop 10.100.1.2 interface ge400-0/0/3.100", 5),
        ("top", 3),
        # IPv6 reverse-path for ge sub-if source prefix
        ("network-services vrf instance urpf-vrf protocols static address-family ipv6-unicast", 5),
        ("route 2001:db8:10::/64 next-hop 2001:db8:100::2 interface ge400-0/0/3.100", 5),
        ("top", 3),
        # IPv4 reverse-path for bundle sub-if source prefix
        ("network-services vrf instance urpf-vrf protocols static address-family ipv4-unicast", 5),
        ("route 10.100.20.0/24 next-hop 10.100.2.2 interface bundle-10.100", 5),
        ("top", 3),
        # IPv6 reverse-path for bundle sub-if
        ("network-services vrf instance urpf-vrf protocols static address-family ipv6-unicast", 5),
        ("route 2001:db8:20::/64 next-hop 2001:db8:200::2 interface bundle-10.100", 5),
        ("top", 3),
        # Default route in VRF for allow-default testing (step 11)
        ("network-services vrf instance urpf-vrf protocols static address-family ipv4-unicast", 5),
        ("route 0.0.0.0/0 next-hop 10.100.1.2 interface ge400-0/0/3.100", 5),
        ("top", 3),
        ("network-services vrf instance urpf-vrf protocols static address-family ipv6-unicast", 5),
        ("route ::/0 next-hop 2001:db8:100::2 interface ge400-0/0/3.100", 5),
        ("top", 3),
        ("commit", 20),
    ]

    commit_ok3 = True
    for cmd, wait in step3_cmds:
        output = run_and_print(chan, cmd, wait)
        if "ERROR" in output:
            print(f"  *** ERROR in: {cmd} ***")
            if cmd == "commit":
                commit_ok3 = False

    # Verify Step 3
    print("\n--- Step 3 Verification ---")
    run_and_print(chan, "end", 3)
    v3_v4 = run_and_print(chan, "show route vrf urpf-vrf 10.100.10.0/24 | no-more", 10)
    v3_v6 = run_and_print(chan, "show route vrf urpf-vrf 2001:db8:10::/64 | no-more", 10)
    v3_full = run_and_print(chan, "show route vrf urpf-vrf | no-more", 12)
    v3_cfg = run_and_print(chan, "show config network-services vrf instance urpf-vrf protocols static | no-more", 10)

    RESULTS["step3"] = {
        "route_v4_specific": v3_v4,
        "route_v6_specific": v3_v6,
        "route_full": v3_full,
        "static_config": v3_cfg,
    }

    step3_pass = (commit_ok3 and "10.100.10.0/24" in v3_full)
    print(f"\n>>> STEP 3 RESULT: {'PASS' if step3_pass else 'FAIL'}")
    RESULTS["step3"]["result"] = "PASS" if step3_pass else "FAIL"

    # Baseline counters
    print("\n--- Baseline counters ---")
    v_cnt_ge = run_and_print(chan, "show interfaces counters ge400-0/0/3.100 | no-more", 10)
    v_cnt_ge_parent = run_and_print(chan, "show interfaces counters ge400-0/0/3 | no-more", 10)
    RESULTS["baseline_counters"] = {
        "ge_sub": v_cnt_ge,
        "ge_parent": v_cnt_ge_parent,
    }

    chan.close()
    ssh.close()

    with open("/home/dn/output/sw244113_steps1_3.json", "w") as f:
        json.dump(RESULTS, f, indent=2)

    print("\n" + "="*70)
    print("SUMMARY — Steps 1-3")
    print("="*70)
    for step in ["step1", "step2", "step3"]:
        print(f"  {step}: {RESULTS[step]['result']}")
    print(f"\nResults saved to /home/dn/output/sw244113_steps1_3.json")

if __name__ == "__main__":
    main()
