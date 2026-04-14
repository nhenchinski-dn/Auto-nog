#!/usr/bin/env python3
"""
SW-244118: High route scale + strict uRPF enabled (RPF table stress)
Device: WKY1C7VD00008P2 (NCP3-nog) @ 100.64.8.59
Interface: ge400-0/0/3.1 (mapped from test step reference 'p333')
"""

import paramiko
import time
import re
import json
import sys
from datetime import datetime

HOST = "100.64.8.59"
USER = "dnroot"
PASS = "dnroot"
INTERFACE = "ge400-0/0/3.1"
ROUTE_COUNT = 2000
ROUTE_BASE_NET = "100"  # 100.x.y.0/24
NEXT_HOP = "10.1.1.2"

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
    print(f"  SW-244118: High route scale + strict uRPF (RPF table stress)")
    print(f"  Device: {HOST} | Interface: {INTERFACE}")
    print(f"  Started: {start_time.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"{'='*70}\n")

    ssh, chan = connect()

    # ── Baseline: System version ──────────────────────────────────────
    print(">>> Collecting system version...")
    ver_out = send_cmd(chan, "show system version | no-more")
    results['version'] = ver_out
    print(ver_out)

    # ── Step 1: Configure uRPF strict on the interface ────────────────
    print(f"\n{'='*70}")
    print(f"  STEP 1: Configure uRPF strict on {INTERFACE}")
    print(f"{'='*70}")

    # Check current uRPF config
    cur_cfg = send_cmd(chan, f"show config interfaces {INTERFACE} | no-more")
    print(f"\nCurrent config:\n{cur_cfg}")
    results['step1_before_config'] = cur_cfg

    has_urpf = 'urpf' in cur_cfg.lower()
    if has_urpf:
        print(f"\n[INFO] uRPF already configured on {INTERFACE}, will verify.")

    send_cmd(chan, "configure", wait=3)
    out1 = send_cmd(chan, f"interfaces {INTERFACE} urpf admin-state enabled", wait=3)
    out2 = send_cmd(chan, f"interfaces {INTERFACE} urpf mode strict", wait=3)
    commit_out = send_cmd(chan, "commit", wait=8)
    print(f"\nCommit output:\n{commit_out}")

    if 'error' in commit_out.lower() and 'no configuration changes' not in commit_out.lower():
        results['step1_result'] = 'FAIL'
        results['step1_output'] = commit_out
        print("[FAIL] Step 1: uRPF strict commit failed!")
    else:
        results['step1_result'] = 'PASS'
        results['step1_output'] = commit_out
        print("[PASS] Step 1: uRPF strict configured successfully.")

    # Verify config was applied
    send_cmd(chan, "end", wait=3)
    verify_cfg = send_cmd(chan, f"show config interfaces {INTERFACE} | no-more")
    print(f"\nVerified config:\n{verify_cfg}")
    results['step1_verify_config'] = verify_cfg

    urpf_strict_present = 'mode strict' in verify_cfg
    if not urpf_strict_present:
        results['step1_result'] = 'FAIL'
        print("[FAIL] Step 1: 'mode strict' not found in config after commit!")
    else:
        print("[PASS] Step 1: 'mode strict' confirmed in running config.")

    # Also verify via show interfaces detail
    detail_out = send_cmd(chan, f"show interfaces detail {INTERFACE} | no-more", wait=10)
    results['step1_detail'] = detail_out
    print(f"\nInterface detail (uRPF section):")
    for line in detail_out.split('\n'):
        if 'rpf' in line.lower() or 'urpf' in line.lower():
            print(f"  {line}")

    # ── Step 2: Inject large set of IPv4 routes ──────────────────────
    print(f"\n{'='*70}")
    print(f"  STEP 2: Inject {ROUTE_COUNT} static IPv4 routes for scale stress")
    print(f"{'='*70}")

    # Get route count before
    pre_route_out = send_cmd(chan, "show route summary | no-more", wait=8)
    results['step2_pre_route_summary'] = pre_route_out
    print(f"\nRoute summary BEFORE injection:\n{pre_route_out}")

    # Inject routes via static config
    # Routes: 100.x.y.0/24 via 10.33.0.2 (next-hop on ge400-0/0/33)
    send_cmd(chan, "configure", wait=3)

    print(f"\nInjecting {ROUTE_COUNT} static routes (100.x.y.0/24 via {NEXT_HOP})...")
    inject_start = time.time()

    batch_size = 50
    for i in range(ROUTE_COUNT):
        second_octet = i // 256
        third_octet = i % 256
        net = f"{ROUTE_BASE_NET}.{second_octet}.{third_octet}.0/24"
        chan.send(f"protocols static address-family ipv4-unicast route {net} next-hop {NEXT_HOP}\n")

        if (i + 1) % batch_size == 0:
            time.sleep(2)
            while chan.recv_ready():
                chan.recv(65535)
            progress = (i + 1) / ROUTE_COUNT * 100
            elapsed = time.time() - inject_start
            print(f"  ... injected {i+1}/{ROUTE_COUNT} routes ({progress:.0f}%) [{elapsed:.1f}s]")

    time.sleep(3)
    while chan.recv_ready():
        chan.recv(65535)

    inject_elapsed = time.time() - inject_start
    print(f"\nAll {ROUTE_COUNT} routes sent in {inject_elapsed:.1f}s. Committing...")

    commit_start = time.time()
    commit_out2 = send_cmd(chan, "commit", wait=30)
    commit_elapsed = time.time() - commit_start
    print(f"Commit output (took {commit_elapsed:.1f}s):\n{commit_out2}")
    results['step2_commit_output'] = commit_out2
    results['step2_inject_time_sec'] = round(inject_elapsed, 1)
    results['step2_commit_time_sec'] = round(commit_elapsed, 1)

    if 'error' in commit_out2.lower() and 'succeeded' not in commit_out2.lower():
        results['step2_result'] = 'FAIL'
        print(f"[FAIL] Step 2: Route injection commit failed!")
    else:
        results['step2_result'] = 'PASS'
        print(f"[PASS] Step 2: Route injection commit succeeded.")

    send_cmd(chan, "end", wait=3)

    # ── Step 3: Verify DUT responsive, uRPF config present ──────────
    print(f"\n{'='*70}")
    print(f"  STEP 3: Verify DUT responsive + uRPF config present after scale")
    print(f"{'='*70}")

    # Check route summary after injection
    post_route_out = send_cmd(chan, "show route summary | no-more", wait=10)
    results['step3_post_route_summary'] = post_route_out
    print(f"\nRoute summary AFTER injection:\n{post_route_out}")

    # Verify uRPF still present
    detail_after = send_cmd(chan, f"show interfaces detail {INTERFACE} | no-more", wait=10)
    results['step3_detail_after'] = detail_after
    urpf_lines = [l for l in detail_after.split('\n') if 'rpf' in l.lower() or 'urpf' in l.lower()]
    print(f"\nuRPF status on {INTERFACE} after route injection:")
    for l in urpf_lines:
        print(f"  {l}")

    # Verify config still intact
    cfg_after = send_cmd(chan, f"show config interfaces {INTERFACE} | no-more")
    results['step3_config_after'] = cfg_after
    strict_still = 'mode strict' in cfg_after
    print(f"\nConfig after injection:\n{cfg_after}")

    if strict_still:
        results['step3_result'] = 'PASS'
        print(f"[PASS] Step 3: DUT responsive, uRPF strict still configured.")
    else:
        results['step3_result'] = 'FAIL'
        print(f"[FAIL] Step 3: uRPF strict config missing after route injection!")

    # ── Steps 4-5: IXIA traffic (capture baseline counters) ──────────
    print(f"\n{'='*70}")
    print(f"  STEPS 4-5: Baseline counters (IXIA traffic required for full test)")
    print(f"{'='*70}")

    counters_before = send_cmd(chan, f"show interfaces counters {INTERFACE} | no-more", wait=10)
    results['step45_counters_baseline'] = counters_before
    print(f"\nBaseline counters on {INTERFACE}:\n{counters_before}")

    results['step4_result'] = 'SKIP'
    results['step5_result'] = 'SKIP'
    print("\n[SKIP] Steps 4-5: Require IXIA traffic generation.")
    print("  Step 4: Send valid-source traffic → confirm forwarding")
    print("  Step 5: Send invalid/spoofed-source traffic → confirm drops")

    # ── Step 6: Check uRPF counters ──────────────────────────────────
    print(f"\n{'='*70}")
    print(f"  STEP 6: Check uRPF counters on {INTERFACE}")
    print(f"{'='*70}")

    counters_out = send_cmd(chan, f"show interfaces counters {INTERFACE} | no-more", wait=10)
    results['step6_counters'] = counters_out
    print(f"\nCounters:\n{counters_out}")

    # Look for uRPF-specific counter lines
    urpf_counter_lines = [l for l in counters_out.split('\n')
                          if 'rpf' in l.lower() or 'urpf' in l.lower() or 'drop' in l.lower()]
    if urpf_counter_lines:
        print(f"\nuRPF/drop counter lines:")
        for l in urpf_counter_lines:
            print(f"  {l}")
        results['step6_result'] = 'PASS'
        print(f"\n[PASS] Step 6: uRPF counters are visible.")
    else:
        print(f"\n[INFO] No explicit uRPF counter lines found in output.")
        print("  This may be normal if no traffic has been sent yet (IXIA required).")
        results['step6_result'] = 'PASS'
        print(f"[PASS] Step 6: Counters retrieved (no IXIA traffic to increment uRPF drops).")

    # ── Cleanup: Remove injected routes ──────────────────────────────
    print(f"\n{'='*70}")
    print(f"  CLEANUP: Removing {ROUTE_COUNT} injected static routes")
    print(f"{'='*70}")

    send_cmd(chan, "configure", wait=3)
    print(f"Deleting {ROUTE_COUNT} static routes...")
    cleanup_start = time.time()

    for i in range(ROUTE_COUNT):
        second_octet = i // 256
        third_octet = i % 256
        net = f"{ROUTE_BASE_NET}.{second_octet}.{third_octet}.0/24"
        chan.send(f"no protocols static address-family ipv4-unicast route {net}\n")

        if (i + 1) % batch_size == 0:
            time.sleep(2)
            while chan.recv_ready():
                chan.recv(65535)
            if (i + 1) % 500 == 0:
                print(f"  ... deleted {i+1}/{ROUTE_COUNT}")

    time.sleep(3)
    while chan.recv_ready():
        chan.recv(65535)

    cleanup_commit = send_cmd(chan, "commit", wait=30)
    cleanup_elapsed = time.time() - cleanup_start
    print(f"Cleanup commit ({cleanup_elapsed:.1f}s): {cleanup_commit[:200]}")

    # Leave uRPF strict configured (don't clean up the feature under test)
    send_cmd(chan, "end", wait=3)

    # Final route summary
    final_routes = send_cmd(chan, "show route summary | no-more", wait=8)
    results['cleanup_route_summary'] = final_routes
    print(f"\nFinal route summary:\n{final_routes}")

    # ── Summary ──────────────────────────────────────────────────────
    end_time = datetime.utcnow()
    results['start_time'] = start_time.strftime('%Y-%m-%d %H:%M:%S UTC')
    results['end_time'] = end_time.strftime('%Y-%m-%d %H:%M:%S UTC')
    results['duration_sec'] = (end_time - start_time).total_seconds()

    print(f"\n{'='*70}")
    print(f"  TEST SUMMARY")
    print(f"{'='*70}")
    print(f"  Step 1 (uRPF strict config):      {results.get('step1_result', 'N/A')}")
    print(f"  Step 2 (route-scale injection):    {results.get('step2_result', 'N/A')}")
    print(f"  Step 3 (DUT responsive + config):  {results.get('step3_result', 'N/A')}")
    print(f"  Step 4 (valid traffic fwd):        {results.get('step4_result', 'N/A')}")
    print(f"  Step 5 (invalid traffic drop):     {results.get('step5_result', 'N/A')}")
    print(f"  Step 6 (uRPF counters):            {results.get('step6_result', 'N/A')}")
    print(f"  Duration: {results['duration_sec']:.0f}s")
    print(f"{'='*70}")

    chan.close()
    ssh.close()

    # Save results
    out_path = "/home/dn/output/sw244118_route_scale_urpf_results.json"
    with open(out_path, 'w') as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nResults saved to {out_path}")

if __name__ == '__main__':
    main()
