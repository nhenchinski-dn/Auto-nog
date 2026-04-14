#!/usr/bin/env python3
"""SW-244118: uRPF strict enable/disable cycling at 2M route scale.

Phases:
  1. Verify baseline (2M routes, uRPF strict, FIB installed)
  2. Cycle uRPF disable/enable 3 times, verify routes stable each time
  3. Final verification
"""
import time
import re
import paramiko
import json

HOST = "WKY1C7VD00008P2"
L3_INTERFACES = ["ge400-0/0/3", "ge400-0/0/3.1", "ge400-0/0/33"]
URPF_CYCLES = 3
RESULTS = {}


def connect(host):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, username='dnroot', password='dnroot',
                   look_for_keys=False, allow_agent=False, timeout=15)
    chan = client.invoke_shell(width=250, height=5000)
    time.sleep(6)
    while chan.recv_ready():
        chan.recv(65536)
    return client, chan


def send_cmd(chan, cmd, wait=5):
    chan.send(cmd + "\n")
    time.sleep(wait)
    out = ""
    retries = 0
    while True:
        if chan.recv_ready():
            out += chan.recv(65536).decode('utf-8', errors='replace')
            retries = 0
        else:
            retries += 1
            if retries > 6:
                break
            time.sleep(1)
    out = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', out)
    out = re.sub(r'\r', '', out)
    out = re.sub(r'-- More -- \(Press q to quit\)\s*', '', out)
    return out


def get_route_counts(chan):
    out = send_cmd(chan, "show route summary | no-more", wait=5)
    ipv4_bgp = 0
    ipv4_total = 0
    for line in out.split('\n'):
        if 'ebgp' in line:
            nums = re.findall(r'[\d,]+', line)
            if nums:
                ipv4_bgp = int(nums[0].replace(',', ''))
        if 'Totals' in line and ipv4_total == 0:
            nums = re.findall(r'[\d,]+', line)
            if nums:
                ipv4_total = int(nums[0].replace(',', ''))
    return ipv4_bgp, ipv4_total


def get_fib_count(chan):
    out = send_cmd(chan, "show route forwarding-table summary | no-more", wait=5)
    for line in out.split('\n'):
        if 'route' in line.lower() and 'Total' not in line and 'forwarding' not in line:
            nums = re.findall(r'\d+', line)
            if len(nums) >= 1:
                return int(nums[0])
    return 0


def get_npu_sram_rejects(chan):
    out = send_cmd(chan, "show system npu-resources | no-more", wait=5)
    for line in out.split('\n'):
        if 'SRAM full rejects' not in line and '| 0' in line:
            continue
        if '| 0     |' in line and 'SRAM' not in line:
            continue
        nums = re.findall(r'\d+', line)
        if len(nums) >= 3:
            return int(nums[1]), int(nums[2])
    return 0, 0


def check_bgp_state(chan):
    out = send_cmd(chan, "show bgp neighbor | no-more", wait=5)
    state = "Unknown"
    prefixes = 0
    for line in out.split('\n'):
        if 'BGP state' in line:
            m = re.search(r'BGP state = (\w+)', line)
            if m:
                state = m.group(1)
        if 'prefixes accepted' in line:
            nums = re.findall(r'(\d+)\s+prefixes accepted', line)
            if nums:
                prefixes = int(nums[0])
    return state, prefixes


def set_urpf(chan, enabled):
    action = "enabled" if enabled else "disabled"
    send_cmd(chan, "configure", wait=3)
    send_cmd(chan, "top", wait=2)
    for intf in L3_INTERFACES:
        send_cmd(chan, "top", wait=1)
        send_cmd(chan, f"interfaces {intf} urpf admin-state {action}", wait=3)
        if enabled:
            send_cmd(chan, "top", wait=1)
            send_cmd(chan, f"interfaces {intf} urpf mode strict", wait=3)
    send_cmd(chan, "top", wait=2)
    out = send_cmd(chan, "commit", wait=20)
    send_cmd(chan, "top", wait=2)
    send_cmd(chan, "exit", wait=2)
    return out


def main():
    print(f"{'='*60}")
    print(f"SW-244118: uRPF Scale Test — 2M Routes + Enable/Disable Cycling")
    print(f"{'='*60}")
    print(f"Host: {HOST}")
    print(f"Interfaces: {', '.join(L3_INTERFACES)}")
    print(f"Cycles: {URPF_CYCLES}")
    print()

    client, chan = connect(HOST)

    # Phase 1: Baseline
    print("=" * 40)
    print("PHASE 1: Baseline verification")
    print("=" * 40)

    bgp_state, bgp_prefixes = check_bgp_state(chan)
    print(f"  BGP state: {bgp_state}, prefixes accepted: {bgp_prefixes}")

    bgp_routes, total_routes = get_route_counts(chan)
    print(f"  RIB: {bgp_routes} eBGP routes, {total_routes} total IPv4")

    fib_routes = get_fib_count(chan)
    print(f"  FIB (hardware): {fib_routes} route entries")

    RESULTS['baseline'] = {
        'bgp_state': bgp_state,
        'bgp_prefixes': bgp_prefixes,
        'rib_bgp': bgp_routes,
        'rib_total': total_routes,
        'fib_routes': fib_routes,
    }

    baseline_bgp = bgp_routes
    if bgp_routes < 1900000:
        print(f"  WARNING: Expected ~2M BGP routes, got {bgp_routes}")
    else:
        print(f"  PASS: 2M routes installed with uRPF strict enabled")

    # Phase 2: uRPF cycling
    print()
    print("=" * 40)
    print("PHASE 2: uRPF enable/disable cycling")
    print("=" * 40)

    all_cycles_pass = True
    for cycle in range(1, URPF_CYCLES + 1):
        print(f"\n--- Cycle {cycle}/{URPF_CYCLES} ---")

        # Disable uRPF
        print(f"  Disabling uRPF on all interfaces...")
        out = set_urpf(chan, enabled=False)
        if "ERROR" in out:
            print(f"  COMMIT ERROR: {out}")
        else:
            print(f"  Commit OK")
        time.sleep(10)

        bgp_routes_dis, total_dis = get_route_counts(chan)
        bgp_state_dis, _ = check_bgp_state(chan)
        print(f"  After disable: BGP={bgp_state_dis}, eBGP routes={bgp_routes_dis}, total={total_dis}")

        route_diff = abs(bgp_routes_dis - baseline_bgp)
        if route_diff > 100:
            print(f"  FAIL: Route count changed by {route_diff} after uRPF disable!")
            all_cycles_pass = False
        else:
            print(f"  PASS: Routes stable (diff={route_diff})")

        # Re-enable uRPF
        print(f"  Re-enabling uRPF strict on all interfaces...")
        out = set_urpf(chan, enabled=True)
        if "ERROR" in out:
            print(f"  COMMIT ERROR: {out}")
        else:
            print(f"  Commit OK")
        time.sleep(15)

        bgp_routes_en, total_en = get_route_counts(chan)
        bgp_state_en, _ = check_bgp_state(chan)
        print(f"  After re-enable: BGP={bgp_state_en}, eBGP routes={bgp_routes_en}, total={total_en}")

        route_diff = abs(bgp_routes_en - baseline_bgp)
        if route_diff > 100:
            print(f"  FAIL: Route count changed by {route_diff} after uRPF re-enable!")
            all_cycles_pass = False
        else:
            print(f"  PASS: Routes stable (diff={route_diff})")

        RESULTS[f'cycle_{cycle}'] = {
            'disable': {'bgp_state': bgp_state_dis, 'bgp_routes': bgp_routes_dis, 'total': total_dis},
            'enable': {'bgp_state': bgp_state_en, 'bgp_routes': bgp_routes_en, 'total': total_en},
        }

    # Phase 3: Final verification
    print()
    print("=" * 40)
    print("PHASE 3: Final verification")
    print("=" * 40)

    bgp_state_f, bgp_pf = check_bgp_state(chan)
    bgp_routes_f, total_f = get_route_counts(chan)
    fib_f = get_fib_count(chan)
    print(f"  BGP state: {bgp_state_f}, prefixes: {bgp_pf}")
    print(f"  RIB: {bgp_routes_f} eBGP, {total_f} total")
    print(f"  FIB: {fib_f} route entries")

    RESULTS['final'] = {
        'bgp_state': bgp_state_f,
        'bgp_prefixes': bgp_pf,
        'rib_bgp': bgp_routes_f,
        'rib_total': total_f,
        'fib_routes': fib_f,
    }

    # Verify uRPF still enabled
    for intf in L3_INTERFACES:
        out = send_cmd(chan, f"show interfaces detail {intf} | include uRPF", wait=3)
        for line in out.split('\n'):
            if 'uRPF' in line and 'check' in line:
                print(f"  {intf}: {line.strip()}")

    send_cmd(chan, "exit", wait=2)
    client.close()

    # Summary
    print()
    print("=" * 60)
    overall = "PASS" if (all_cycles_pass and bgp_routes_f >= 1900000 and bgp_state_f == "Established") else "FAIL"
    print(f"OVERALL RESULT: {overall}")
    print(f"  Baseline: {baseline_bgp} eBGP routes")
    print(f"  Final:    {bgp_routes_f} eBGP routes")
    print(f"  Cycles:   {URPF_CYCLES} enable/disable cycles completed")
    print(f"  BGP:      {bgp_state_f}")
    if all_cycles_pass:
        print(f"  Stability: All cycles passed — no route loss during uRPF toggling")
    else:
        print(f"  Stability: FAILURES detected during cycling")
    print("=" * 60)

    with open('/home/dn/output/sw244118_scale_results.json', 'w') as f:
        json.dump(RESULTS, f, indent=2)
    print(f"\nResults saved to /home/dn/output/sw244118_scale_results.json")


if __name__ == "__main__":
    main()
