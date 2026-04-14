#!/usr/bin/env python3
"""
SW-244118: High route scale + strict uRPF enabled (RPF table stress)
Revised test: route scale + uRPF enable/disable cycling.

Device: WKY1C7VD00008P2 (NCP3-nog) @ 100.64.8.59

Focus:
  - Inject large route set (static, max lab allows)
  - Verify route count stable with uRPF strict enabled
  - Cycle uRPF enable/disable, verify routes remain intact
  - No route withdrawals, no partial programming, no crashes
"""

import paramiko
import time
import re
import json
from datetime import datetime

HOST = "100.64.8.59"
USER = "dnroot"
PASS = "dnroot"
NEXT_HOP = "10.1.1.2"
ROUTE_COUNT = 2000
L3_INTERFACES = ["ge400-0/0/3", "ge400-0/0/3.1", "ge400-0/0/33"]
URPF_CYCLE_COUNT = 3

results = {}

def clean(text):
    text = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)
    text = re.sub(r'\r', '', text)
    text = re.sub(r'-- More -- \(Press q to quit\)\s*', '', text)
    return text.strip()

def send_cmd(chan, cmd, wait=8, max_retries=5):
    chan.send(cmd + '\n')
    time.sleep(wait)
    out = b''
    for _ in range(max_retries):
        while chan.recv_ready():
            out += chan.recv(65535)
        time.sleep(1)
        if not chan.recv_ready():
            break
    return clean(out.decode(errors='replace'))

def get_route_count(output):
    """Extract IPv4 and IPv6 totals from show route summary output."""
    ipv4 = ipv6 = 0
    for line in output.split('\n'):
        if 'Totals' in line:
            nums = re.findall(r'\d+', line)
            if len(nums) >= 2:
                ipv4, ipv6 = int(nums[0]), int(nums[1])
                return ipv4, ipv6
            elif len(nums) == 1:
                ipv4 = int(nums[0])
    return ipv4, ipv6

def connect():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASS,
                timeout=30, look_for_keys=False, allow_agent=False)
    chan = ssh.invoke_shell(width=300, height=5000)
    time.sleep(6)
    chan.recv(65535)
    return ssh, chan

def main():
    start_time = datetime.utcnow()
    print(f"{'='*70}")
    print(f"  SW-244118 v2: Route scale + uRPF enable/disable cycling")
    print(f"  Device: {HOST} | Routes: {ROUTE_COUNT}")
    print(f"  L3 interfaces: {', '.join(L3_INTERFACES)}")
    print(f"  Started: {start_time.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"{'='*70}\n")

    ssh, chan = connect()

    # ── System version ────────────────────────────────────────────────
    ver_out = send_cmd(chan, "show system version | no-more")
    results['version'] = ver_out
    print(f"Version:\n{ver_out}\n")

    # ── STEP 1: Baseline route count (before injection) ───────────────
    print(f"{'='*70}")
    print(f"  STEP 1: Baseline route count")
    print(f"{'='*70}")

    baseline_out = send_cmd(chan, "show route summary | no-more", wait=10)
    results['step1_baseline'] = baseline_out
    ipv4_base, ipv6_base = get_route_count(baseline_out)
    print(f"Baseline: {ipv4_base} IPv4, {ipv6_base} IPv6")
    results['step1_ipv4_baseline'] = ipv4_base
    results['step1_ipv6_baseline'] = ipv6_base

    # ── STEP 2: Inject routes at scale ────────────────────────────────
    print(f"\n{'='*70}")
    print(f"  STEP 2: Inject {ROUTE_COUNT} static IPv4 routes")
    print(f"{'='*70}")

    send_cmd(chan, "configure", wait=3)
    inject_start = time.time()

    for i in range(ROUTE_COUNT):
        second_octet = i // 256
        third_octet = i % 256
        net = f"100.{second_octet}.{third_octet}.0/24"
        chan.send(f"protocols static address-family ipv4-unicast route {net} next-hop {NEXT_HOP}\n")
        if (i + 1) % 50 == 0:
            time.sleep(2)
            while chan.recv_ready():
                chan.recv(65535)
            if (i + 1) % 500 == 0:
                print(f"  ... injected {i+1}/{ROUTE_COUNT}")

    time.sleep(3)
    while chan.recv_ready():
        chan.recv(65535)

    inject_elapsed = time.time() - inject_start
    print(f"All {ROUTE_COUNT} routes sent in {inject_elapsed:.1f}s. Committing...")

    commit_out = send_cmd(chan, "commit", wait=30)
    commit_elapsed = time.time() - inject_start
    results['step2_commit'] = commit_out
    results['step2_time'] = round(commit_elapsed, 1)

    if 'succeeded' in commit_out.lower() or 'no configuration changes' in commit_out.lower():
        print(f"[PASS] Commit succeeded ({commit_elapsed:.1f}s)")
        results['step2_result'] = 'PASS'
    else:
        print(f"[FAIL] Commit issue: {commit_out[:200]}")
        results['step2_result'] = 'FAIL'

    send_cmd(chan, "end", wait=3)

    # Verify post-injection count
    post_inject = send_cmd(chan, "show route summary | no-more", wait=10)
    ipv4_post, ipv6_post = get_route_count(post_inject)
    results['step2_ipv4_after'] = ipv4_post
    results['step2_ipv6_after'] = ipv6_post
    expected_ipv4 = ipv4_base + ROUTE_COUNT
    print(f"After injection: {ipv4_post} IPv4 (expected ~{expected_ipv4}), {ipv6_post} IPv6")

    if ipv4_post >= expected_ipv4 - 5:
        print(f"[PASS] Route count matches expected")
    else:
        print(f"[FAIL] Route count mismatch: got {ipv4_post}, expected ~{expected_ipv4}")
        results['step2_result'] = 'FAIL'

    # ── STEP 3: Enable uRPF strict on ALL L3 interfaces ──────────────
    print(f"\n{'='*70}")
    print(f"  STEP 3: Enable uRPF strict on all L3 interfaces, verify routes")
    print(f"{'='*70}")

    send_cmd(chan, "configure", wait=3)
    for intf in L3_INTERFACES:
        out = send_cmd(chan, f"interfaces {intf} urpf admin-state enabled", wait=3)
        out = send_cmd(chan, f"interfaces {intf} urpf mode strict", wait=3)
        print(f"  Configured uRPF strict on {intf}")

    commit_urpf = send_cmd(chan, "commit", wait=15)
    results['step3_commit'] = commit_urpf
    print(f"  Commit: {commit_urpf.split(chr(10))[-2] if chr(10) in commit_urpf else commit_urpf[:100]}")
    send_cmd(chan, "end", wait=3)

    # Verify route count unchanged
    post_urpf = send_cmd(chan, "show route summary | no-more", wait=10)
    ipv4_after_urpf, _ = get_route_count(post_urpf)
    results['step3_ipv4_after_enable'] = ipv4_after_urpf
    print(f"  Routes after uRPF enable: {ipv4_after_urpf} IPv4 (was {ipv4_post})")

    if ipv4_after_urpf == ipv4_post:
        print(f"[PASS] Route count unchanged after enabling uRPF strict")
        results['step3_result'] = 'PASS'
    else:
        print(f"[FAIL] Route count changed: {ipv4_post} -> {ipv4_after_urpf}")
        results['step3_result'] = 'FAIL'

    # Verify uRPF is shown on each interface
    for intf in L3_INTERFACES:
        detail = send_cmd(chan, f"show interfaces detail {intf} | no-more", wait=10)
        urpf_lines = [l.strip() for l in detail.split('\n') if 'rpf' in l.lower()]
        print(f"  {intf}: {'; '.join(urpf_lines) if urpf_lines else 'no uRPF lines found'}")

    # ── STEP 4: Disable uRPF, verify routes intact ───────────────────
    print(f"\n{'='*70}")
    print(f"  STEP 4: Disable uRPF on all interfaces, verify routes intact")
    print(f"{'='*70}")

    send_cmd(chan, "configure", wait=3)
    for intf in L3_INTERFACES:
        send_cmd(chan, f"interfaces {intf} urpf admin-state disabled", wait=3)
        print(f"  Disabled uRPF on {intf}")

    commit_dis = send_cmd(chan, "commit", wait=15)
    results['step4_commit'] = commit_dis
    print(f"  Commit: {commit_dis.split(chr(10))[-2] if chr(10) in commit_dis else commit_dis[:100]}")
    send_cmd(chan, "end", wait=3)

    post_disable = send_cmd(chan, "show route summary | no-more", wait=10)
    ipv4_after_disable, _ = get_route_count(post_disable)
    results['step4_ipv4_after_disable'] = ipv4_after_disable
    print(f"  Routes after uRPF disable: {ipv4_after_disable} IPv4 (was {ipv4_post})")

    if ipv4_after_disable == ipv4_post:
        print(f"[PASS] Route count unchanged after disabling uRPF")
        results['step4_result'] = 'PASS'
    else:
        print(f"[FAIL] Route count changed: {ipv4_post} -> {ipv4_after_disable}")
        results['step4_result'] = 'FAIL'

    # ── STEP 5: Cycle uRPF enable/disable N times ────────────────────
    print(f"\n{'='*70}")
    print(f"  STEP 5: Cycle uRPF enable/disable x{URPF_CYCLE_COUNT}")
    print(f"{'='*70}")

    cycle_pass = True
    for cycle in range(1, URPF_CYCLE_COUNT + 1):
        print(f"\n  --- Cycle {cycle}/{URPF_CYCLE_COUNT} ---")

        # Enable
        send_cmd(chan, "configure", wait=3)
        for intf in L3_INTERFACES:
            chan.send(f"interfaces {intf} urpf admin-state enabled\n")
            chan.send(f"interfaces {intf} urpf mode strict\n")
            time.sleep(1)
        while chan.recv_ready():
            chan.recv(65535)
        commit_on = send_cmd(chan, "commit", wait=15)
        send_cmd(chan, "end", wait=3)

        rt_on = send_cmd(chan, "show route summary | no-more", wait=10)
        ipv4_on, _ = get_route_count(rt_on)
        on_ok = ipv4_on == ipv4_post
        print(f"  Enable:  {ipv4_on} IPv4 {'(/) OK' if on_ok else '(x) MISMATCH'}")
        if not on_ok:
            cycle_pass = False

        # Disable
        send_cmd(chan, "configure", wait=3)
        for intf in L3_INTERFACES:
            chan.send(f"interfaces {intf} urpf admin-state disabled\n")
            time.sleep(1)
        while chan.recv_ready():
            chan.recv(65535)
        commit_off = send_cmd(chan, "commit", wait=15)
        send_cmd(chan, "end", wait=3)

        rt_off = send_cmd(chan, "show route summary | no-more", wait=10)
        ipv4_off, _ = get_route_count(rt_off)
        off_ok = ipv4_off == ipv4_post
        print(f"  Disable: {ipv4_off} IPv4 {'(/) OK' if off_ok else '(x) MISMATCH'}")
        if not off_ok:
            cycle_pass = False

    results['step5_result'] = 'PASS' if cycle_pass else 'FAIL'
    print(f"\n[{'PASS' if cycle_pass else 'FAIL'}] Step 5: uRPF cycling x{URPF_CYCLE_COUNT}")

    # ── STEP 6: Re-enable uRPF strict (leave in final state) ─────────
    print(f"\n{'='*70}")
    print(f"  STEP 6: Re-enable uRPF strict (restore original state)")
    print(f"{'='*70}")

    send_cmd(chan, "configure", wait=3)
    for intf in L3_INTERFACES:
        chan.send(f"interfaces {intf} urpf admin-state enabled\n")
        chan.send(f"interfaces {intf} urpf mode strict\n")
        time.sleep(1)
    while chan.recv_ready():
        chan.recv(65535)
    send_cmd(chan, "commit", wait=15)
    send_cmd(chan, "end", wait=3)

    final_routes = send_cmd(chan, "show route summary | no-more", wait=10)
    ipv4_final, ipv6_final = get_route_count(final_routes)
    results['step6_ipv4_final'] = ipv4_final
    print(f"  Final state: {ipv4_final} IPv4, {ipv6_final} IPv6, uRPF strict on all L3 intfs")

    # Verify uRPF counters are accessible
    for intf in L3_INTERFACES:
        ctr = send_cmd(chan, f"show interfaces counters {intf} | no-more", wait=10)
        urpf_lines = [l.strip() for l in ctr.split('\n') if 'urpf' in l.lower() or 'uRPF' in l]
        if urpf_lines:
            print(f"  {intf} counters: {'; '.join(urpf_lines[:2])}")

    # ── CLEANUP: Remove injected routes ──────────────────────────────
    print(f"\n{'='*70}")
    print(f"  CLEANUP: Removing {ROUTE_COUNT} injected routes")
    print(f"{'='*70}")

    send_cmd(chan, "configure", wait=3)
    for i in range(ROUTE_COUNT):
        second_octet = i // 256
        third_octet = i % 256
        net = f"100.{second_octet}.{third_octet}.0/24"
        chan.send(f"no protocols static address-family ipv4-unicast route {net}\n")
        if (i + 1) % 50 == 0:
            time.sleep(2)
            while chan.recv_ready():
                chan.recv(65535)
            if (i + 1) % 500 == 0:
                print(f"  ... deleted {i+1}/{ROUTE_COUNT}")

    time.sleep(3)
    while chan.recv_ready():
        chan.recv(65535)

    cleanup_commit = send_cmd(chan, "commit", wait=30)
    send_cmd(chan, "end", wait=3)
    print(f"  Cleanup commit: {cleanup_commit.split(chr(10))[-2] if chr(10) in cleanup_commit else cleanup_commit[:100]}")

    final_clean = send_cmd(chan, "show route summary | no-more", wait=10)
    ipv4_clean, _ = get_route_count(final_clean)
    print(f"  Routes after cleanup: {ipv4_clean} IPv4 (baseline was {ipv4_base})")

    # ── SUMMARY ──────────────────────────────────────────────────────
    end_time = datetime.utcnow()
    results['start_time'] = start_time.strftime('%Y-%m-%d %H:%M:%S UTC')
    results['end_time'] = end_time.strftime('%Y-%m-%d %H:%M:%S UTC')
    results['duration_sec'] = (end_time - start_time).total_seconds()

    print(f"\n{'='*70}")
    print(f"  TEST SUMMARY")
    print(f"{'='*70}")
    print(f"  Step 1 (baseline count):             {ipv4_base} IPv4")
    print(f"  Step 2 (route injection):             {results.get('step2_result', 'N/A')} ({ipv4_post} IPv4)")
    print(f"  Step 3 (enable uRPF, routes stable):  {results.get('step3_result', 'N/A')}")
    print(f"  Step 4 (disable uRPF, routes stable): {results.get('step4_result', 'N/A')}")
    print(f"  Step 5 (cycle x{URPF_CYCLE_COUNT}, routes stable):     {results.get('step5_result', 'N/A')}")
    print(f"  Duration: {results['duration_sec']:.0f}s")
    print(f"{'='*70}")

    overall = 'PASS' if all(results.get(f'step{i}_result') == 'PASS' for i in range(2, 6)) else 'FAIL'
    results['overall'] = overall
    print(f"  OVERALL: {overall}")

    chan.close()
    ssh.close()

    out_path = "/home/dn/output/sw244118_scale_v2_results.json"
    with open(out_path, 'w') as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nResults saved to {out_path}")

if __name__ == '__main__':
    main()
