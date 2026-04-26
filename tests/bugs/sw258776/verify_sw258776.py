#!/usr/bin/env python3
"""
Verify SW-258776: uRPF strict | Per-AFI Config-to-Operational State Mismatch

Bug: Per-AFI uRPF config is accepted/committed but show interfaces shows uRPF disabled.
Verify the fix on WKY1C7VD00008P2 by:
  A) Checking existing per-AFI uRPF interface (ge400-0/0/3.100) operational state
  B) Configuring per-AFI uRPF on a clean interface (ge400-0/0/7) and verifying
"""

import paramiko
import time
import re
import sys

HOST = "WKY1C7VD00008P2"
USER = "dnroot"
PASS = "dnroot"

def clean_output(output):
    output = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', output)
    output = re.sub(r'\r', '', output)
    output = re.sub(r'-- More -- \(Press q to quit\)\s*', '', output)
    return output

def send_cmd(shell, cmd, wait=5):
    shell.send(cmd + "\n")
    time.sleep(wait)
    output = ""
    retries = 10
    while retries > 0:
        if shell.recv_ready():
            output += shell.recv(65535).decode('utf-8', errors='replace')
            retries = 3
        else:
            retries -= 1
            time.sleep(0.5)
    return clean_output(output)

def parse_urpf_oper(output):
    """Parse uRPF operational state from show interfaces output."""
    v4_match = re.search(r'uRPF IPv4 check:\s*(\w+)(?:.*?Mode:\s*(\w+))?', output)
    v6_match = re.search(r'uRPF IPv6 check:\s*(\w+)(?:.*?Mode:\s*(\w+))?', output)
    return {
        "v4_state": v4_match.group(1) if v4_match else "NOT FOUND",
        "v4_mode": v4_match.group(2) if v4_match and v4_match.group(2) else "N/A",
        "v6_state": v6_match.group(1) if v6_match else "NOT FOUND",
        "v6_mode": v6_match.group(2) if v6_match and v6_match.group(2) else "N/A",
    }

def main():
    print(f"{'='*70}")
    print(f"SW-258776 Verification: uRPF Per-AFI Config-to-Operational State")
    print(f"Device: {HOST}")
    print(f"{'='*70}\n")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    print("[1] Connecting to device...")
    client.connect(HOST, username=USER, password=PASS,
                   look_for_keys=False, allow_agent=False, timeout=15)
    shell = client.invoke_shell(width=250, height=5000)
    time.sleep(6)
    while shell.recv_ready():
        shell.recv(65535)
    print("    Connected successfully.")

    results = {}

    # ===== TEST A: Check existing per-AFI uRPF on ge400-0/0/3.100 =====
    test_intf_a = "ge400-0/0/3.100"
    print(f"\n{'='*70}")
    print(f"TEST A: Verify existing per-AFI uRPF on {test_intf_a}")
    print(f"  (Config has: ipv4 strict + ipv6 strict, both enabled)")
    print(f"{'='*70}")

    print(f"\n  [A1] show config interfaces {test_intf_a}...")
    config_a = send_cmd(shell, f"show config interfaces {test_intf_a} | no-more", wait=6)
    print(config_a)

    print(f"\n  [A2] show interfaces {test_intf_a}...")
    oper_a = send_cmd(shell, f"show interfaces {test_intf_a} | no-more", wait=8)
    print(oper_a)

    urpf_a = parse_urpf_oper(oper_a)
    print(f"\n  Operational uRPF IPv4: {urpf_a['v4_state']}, Mode: {urpf_a['v4_mode']}")
    print(f"  Operational uRPF IPv6: {urpf_a['v6_state']}, Mode: {urpf_a['v6_mode']}")

    a_v4_pass = urpf_a["v4_state"] == "enabled" and urpf_a["v4_mode"].lower() == "strict"
    a_v6_pass = urpf_a["v6_state"] == "enabled" and urpf_a["v6_mode"].lower() == "strict"
    results["A_v4"] = a_v4_pass
    results["A_v6"] = a_v6_pass

    # ===== TEST B: Configure fresh per-AFI uRPF on ge400-0/0/7 =====
    test_intf_b = "ge400-0/0/7"
    print(f"\n{'='*70}")
    print(f"TEST B: Configure fresh per-AFI uRPF on {test_intf_b}")
    print(f"  (Will configure: ipv4 strict + ipv6 loose)")
    print(f"{'='*70}")

    # Add IP addresses and configure uRPF
    print(f"\n  [B1] Adding IP addresses and per-AFI uRPF config...")
    send_cmd(shell, "configure", wait=3)
    send_cmd(shell, f"interfaces {test_intf_b} ipv4-address 77.77.77.1/24", wait=3)
    send_cmd(shell, f"interfaces {test_intf_b} ipv6-address 77:77:77::1/64", wait=3)
    send_cmd(shell, f"interfaces {test_intf_b} urpf address-family ipv4 admin-state enabled", wait=3)
    send_cmd(shell, f"interfaces {test_intf_b} urpf address-family ipv4 mode strict", wait=3)
    send_cmd(shell, f"interfaces {test_intf_b} urpf address-family ipv6 admin-state enabled", wait=3)
    send_cmd(shell, f"interfaces {test_intf_b} urpf address-family ipv6 mode loose", wait=3)
    print("    Committing...")
    commit_out = send_cmd(shell, "commit", wait=15)
    if "Error" in commit_out or "error" in commit_out.lower():
        print(f"    COMMIT ISSUE: {commit_out}")
    else:
        print("    Commit successful.")

    # Verify config
    print(f"\n  [B2] show config interfaces {test_intf_b}...")
    config_b = send_cmd(shell, f"show config interfaces {test_intf_b} | no-more", wait=6)
    print(config_b)

    config_b_v4 = bool(re.search(r'address-family\s+ipv4', config_b) and
                        re.search(r'admin-state\s+enabled', config_b) and
                        re.search(r'mode\s+strict', config_b))
    config_b_v6 = bool(re.search(r'address-family\s+ipv6', config_b) and
                        re.search(r'mode\s+loose', config_b))

    # Check operational state
    print(f"\n  [B3] show interfaces {test_intf_b} (operational state)...")
    send_cmd(shell, "top", wait=2)
    send_cmd(shell, "exit", wait=2)
    oper_b = send_cmd(shell, f"show interfaces {test_intf_b} | no-more", wait=8)
    print(oper_b)

    urpf_b = parse_urpf_oper(oper_b)
    print(f"\n  Operational uRPF IPv4: {urpf_b['v4_state']}, Mode: {urpf_b['v4_mode']}")
    print(f"  Operational uRPF IPv6: {urpf_b['v6_state']}, Mode: {urpf_b['v6_mode']}")

    b_v4_pass = urpf_b["v4_state"] == "enabled" and urpf_b["v4_mode"].lower() == "strict"
    b_v6_pass = urpf_b["v6_state"] == "enabled" and urpf_b["v6_mode"].lower() == "loose"
    results["B_config_v4"] = config_b_v4
    results["B_config_v6"] = config_b_v6
    results["B_oper_v4"] = b_v4_pass
    results["B_oper_v6"] = b_v6_pass

    # ===== TEST C: Also check ge400-0/0/5 which has global + per-AFI uRPF =====
    test_intf_c = "ge400-0/0/5"
    print(f"\n{'='*70}")
    print(f"TEST C: Verify mixed global+per-AFI uRPF on {test_intf_c}")
    print(f"  (Config has global admin-state enabled + per-AFI ipv4 enabled)")
    print(f"{'='*70}")

    print(f"\n  [C1] show config interfaces {test_intf_c}...")
    config_c = send_cmd(shell, f"show config interfaces {test_intf_c} | no-more", wait=6)
    print(config_c)

    print(f"\n  [C2] show interfaces {test_intf_c}...")
    oper_c = send_cmd(shell, f"show interfaces {test_intf_c} | no-more", wait=8)
    print(oper_c)

    urpf_c = parse_urpf_oper(oper_c)
    print(f"\n  Operational uRPF IPv4: {urpf_c['v4_state']}, Mode: {urpf_c['v4_mode']}")
    print(f"  Operational uRPF IPv6: {urpf_c['v6_state']}, Mode: {urpf_c['v6_mode']}")

    c_v4_pass = urpf_c["v4_state"] == "enabled"
    results["C_v4"] = c_v4_pass

    # ===== CLEANUP: Remove config from ge400-0/0/7 =====
    print(f"\n{'='*70}")
    print(f"CLEANUP: Removing test config from {test_intf_b}")
    print(f"{'='*70}")

    send_cmd(shell, "configure", wait=3)
    send_cmd(shell, f"interfaces {test_intf_b} no ipv4-address 77.77.77.1/24", wait=3)
    send_cmd(shell, f"interfaces {test_intf_b} no ipv6-address 77:77:77::1/64", wait=3)
    send_cmd(shell, f"interfaces {test_intf_b} no urpf", wait=3)
    cleanup_out = send_cmd(shell, "commit", wait=10)
    print(f"  Cleanup commit done.")

    send_cmd(shell, "top", wait=2)
    send_cmd(shell, "exit", wait=2)

    # Verify cleanup
    oper_cleanup = send_cmd(shell, f"show interfaces {test_intf_b} | no-more", wait=8)
    urpf_cleanup = parse_urpf_oper(oper_cleanup)
    print(f"  Post-cleanup uRPF IPv4: {urpf_cleanup['v4_state']}, IPv6: {urpf_cleanup['v6_state']}")

    send_cmd(shell, "exit", wait=2)
    client.close()

    # ===== FINAL REPORT =====
    print(f"\n\n{'='*70}")
    print("FINAL VERIFICATION REPORT - SW-258776")
    print(f"{'='*70}")
    print(f"Device: {HOST} | DNOS 26.2.0")
    print(f"Bug: Per-AFI uRPF config accepted but operational state shows disabled")
    print()

    print(f"TEST A: Existing per-AFI uRPF on {test_intf_a}")
    print(f"  IPv4 (expect enabled/strict):  {'PASS' if results['A_v4'] else 'FAIL'} - got {urpf_a['v4_state']}/{urpf_a['v4_mode']}")
    print(f"  IPv6 (expect enabled/strict):  {'PASS' if results['A_v6'] else 'FAIL'} - got {urpf_a['v6_state']}/{urpf_a['v6_mode']}")

    print(f"\nTEST B: Fresh per-AFI uRPF on {test_intf_b}")
    print(f"  Config IPv4 strict committed:  {'PASS' if results['B_config_v4'] else 'FAIL'}")
    print(f"  Config IPv6 loose committed:   {'PASS' if results['B_config_v6'] else 'FAIL'}")
    print(f"  Oper IPv4 (expect enabled/strict): {'PASS' if results['B_oper_v4'] else 'FAIL'} - got {urpf_b['v4_state']}/{urpf_b['v4_mode']}")
    print(f"  Oper IPv6 (expect enabled/loose):  {'PASS' if results['B_oper_v6'] else 'FAIL'} - got {urpf_b['v6_state']}/{urpf_b['v6_mode']}")

    print(f"\nTEST C: Mixed global+per-AFI uRPF on {test_intf_c}")
    print(f"  IPv4 (expect enabled):         {'PASS' if results['C_v4'] else 'FAIL'} - got {urpf_c['v4_state']}/{urpf_c['v4_mode']}")

    all_pass = all(results.values())
    print(f"\n{'='*70}")
    if all_pass:
        print("OVERALL RESULT: PASS - Bug SW-258776 is FIXED")
        print("Per-AFI uRPF config-to-operational state mismatch is resolved.")
    else:
        failed = [k for k, v in results.items() if not v]
        print(f"OVERALL RESULT: FAIL - Bug SW-258776 is NOT FIXED")
        print(f"Failed checks: {', '.join(failed)}")
    print(f"{'='*70}")

    return 0 if all_pass else 1

if __name__ == "__main__":
    sys.exit(main())
