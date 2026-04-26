#!/usr/bin/env python3
"""SW-258863 Full Test: Breakout + uRPF on ge400-0/0/3 breakout ports with Spirent traffic.
Runs all steps, pauses for user traffic, then cleans up.
"""

import paramiko
import time
import re
import json
import sys
import functools
import os

_orig_print = print
print = functools.partial(_orig_print, flush=True)

DEVICE_IP = "100.64.6.73"
USERNAME = "dnroot"
PASSWORD = "dnroot"
PARENT_PORT = "ge400-0/0/3"
PORT_A = "ge100-0/0/3/0"
PORT_B = "ge100-0/0/3/1"
PORT_C = "ge100-0/0/3/2"
PORT_D = "ge100-0/0/3/3"
VLAN_SUBIF = "ge100-0/0/3/0.100"

SIGNAL_FILE = "/tmp/sw258863_traffic_done"

ANSI_RE = re.compile(r'\x1b\[[0-9;]*[A-Za-z]|\x1b\].*?\x07|\x1b[()][A-B012]|\x0f')

results = []

def clean(text):
    return ANSI_RE.sub('', text)

def connect():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(DEVICE_IP, username=USERNAME, password=PASSWORD,
                timeout=30, look_for_keys=False, allow_agent=False)
    chan = ssh.invoke_shell(width=300)
    time.sleep(5)
    chan.recv(65535)
    return ssh, chan

def run(chan, cmd, wait=8):
    chan.send(cmd + '\n')
    time.sleep(wait)
    out = b''
    while chan.recv_ready():
        out += chan.recv(65535)
    return clean(out.decode(errors='replace'))

def run_show(chan, cmd):
    return run(chan, cmd + ' | no-more')

def commit(chan, label=""):
    out = run(chan, 'commit', wait=20)
    ok = 'Commit succeeded' in out
    tag = f" [{label}]" if label else ""
    print(f">>> COMMIT{tag}")
    print(f"  Result: {'OK' if ok else 'FAILED'}")
    for line in out.splitlines():
        s = line.strip()
        if any(kw in s.lower() for kw in ['commit', 'error', 'fail', 'notice']):
            print(f"    {s}")
    if not ok:
        run(chan, 'rollback 0', wait=5)
        print("  (rolled back)")
    return ok, out

def enter_config(chan):
    run(chan, 'configure')

def exit_config(chan):
    out = run(chan, 'exit')
    if 'uncommitted' in out.lower():
        chan.send('no\n')
        time.sleep(3)
        if chan.recv_ready():
            chan.recv(65535)

def pf(text):
    for line in text.splitlines():
        s = line.strip()
        if s and 'show ' not in s and 'NCP3' not in s:
            print(f"  {s}")

def record(step_name, passed, command, output, expected, analysis):
    results.append({
        'step': step_name,
        'passed': passed,
        'command': command,
        'output': output,
        'expected': expected,
        'analysis': analysis,
    })
    status = "PASS" if passed else "FAIL"
    print(f"  [{status}] {step_name}: {analysis}\n")


def main():
    ts = time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())
    print(f"SW-258863 Full Breakout+uRPF Test — {ts}")
    print(f"Device: {DEVICE_IP} (NCP3-nog / WKY1C7VD00008P2)")
    print(f"Parent port: {PARENT_PORT}")
    print(f"Spirent Port A: {PORT_A}, Port B: {PORT_B}")
    print("=" * 60)

    if os.path.exists(SIGNAL_FILE):
        os.remove(SIGNAL_FILE)

    print("Connecting...")
    ssh, chan = connect()
    print("Connected.\n")

    # ================================================================
    # CLEANUP: Remove any leftover config
    # ================================================================
    enter_config(chan)
    run(chan, f'interfaces {PORT_A} no urpf')
    run(chan, f'interfaces {PORT_A} no ipv4-address')
    run(chan, f'interfaces {PORT_B} no urpf')
    run(chan, f'interfaces {PORT_B} no ipv4-address')
    run(chan, f'no interfaces {VLAN_SUBIF}')
    commit(chan, "pre-cleanup")
    exit_config(chan)

    # ================================================================
    # STEP 1: Verify breakout on ge400-0/0/3
    # ================================================================
    print("=" * 60)
    print("  STEP 1: Verify breakout 100g-4x on ge400-0/0/3")
    print("=" * 60)

    out_bo = run_show(chan, 'show interfaces breakout')
    print("\n--- show interfaces breakout (ge400-0/0/3) ---")
    for line in out_bo.splitlines():
        if 'ge400-0/0/3 ' in line or 'ge400-0/0/3|' in line or ('Port' in line and 'Breakout' in line) or ('---' in line and '---' in line):
            print(f"  {line}")

    has_breakout = 'b100g-4x' in out_bo and 'ge100-0/0/3/0' in out_bo
    record("Step 1: Breakout ge400-0/0/3 verified (100g-4x)",
           has_breakout,
           "show interfaces breakout",
           out_bo,
           "ge400-0/0/3 shows b100g-4x with ge100-0/0/3/0-3",
           f"Breakout active: {'Yes' if has_breakout else 'No'}")

    # Neg-1: parent port urpf after breakout
    out_purpf = run_show(chan, f'show config interfaces {PARENT_PORT} urpf')
    config_body = out_purpf.split('config-start')[-1].split('config-end')[0] if 'config-start' in out_purpf else ''
    parent_clean = 'urpf' not in config_body
    print(f"\n--- show config interfaces {PARENT_PORT} urpf ---")
    pf(out_purpf)
    record("Neg-1: Parent port urpf after breakout",
           parent_clean,
           f"show config interfaces {PARENT_PORT} urpf",
           out_purpf,
           "Empty — parent config erased on breakout",
           f"Parent urpf {'empty as expected' if parent_clean else 'NOT empty'}")

    # ================================================================
    # STEP 2: Global uRPF strict on ge100-0/0/3/0
    # ================================================================
    print("\n" + "=" * 60)
    print(f"  STEP 2: Global uRPF strict on {PORT_A}")
    print("=" * 60)

    enter_config(chan)
    run(chan, f'interfaces {PORT_A} ipv4-address 10.0.30.1/24')
    run(chan, f'interfaces {PORT_A} urpf admin-state enabled')
    run(chan, f'interfaces {PORT_A} urpf mode strict')
    run(chan, f'interfaces {PORT_A} urpf allow-default disabled')
    ok2, _ = commit(chan, "uRPF strict + IP on Port A")
    exit_config(chan)

    out_cfg2 = run_show(chan, f'show config interfaces {PORT_A} urpf')
    print(f"\n--- show config interfaces {PORT_A} urpf ---")
    pf(out_cfg2)

    has_urpf = 'admin-state enabled' in out_cfg2 and 'strict' in out_cfg2
    record("Step 2: Global uRPF strict on ge100-0/0/3/0",
           ok2 and has_urpf,
           f"interfaces {PORT_A} urpf admin-state enabled / mode strict / allow-default disabled",
           out_cfg2,
           "show config displays urpf with admin-state enabled, mode strict, allow-default disabled",
           f"Commit={'OK' if ok2 else 'FAIL'}, config reflected={'Yes' if has_urpf else 'No'}")

    # ================================================================
    # STEP 3: Per-AFI uRPF
    # ================================================================
    print("=" * 60)
    print(f"  STEP 3: Per-AFI uRPF on {PORT_A}")
    print("=" * 60)

    enter_config(chan)
    run(chan, f'interfaces {PORT_A} urpf address-family ipv4 admin-state enabled')
    run(chan, f'interfaces {PORT_A} urpf address-family ipv4 mode strict')
    run(chan, f'interfaces {PORT_A} urpf address-family ipv6 admin-state enabled')
    run(chan, f'interfaces {PORT_A} urpf address-family ipv6 mode loose')
    ok3, _ = commit(chan, "per-AFI uRPF")
    exit_config(chan)

    out_cfg3 = run_show(chan, f'show config interfaces {PORT_A} urpf')
    print(f"\n--- show config interfaces {PORT_A} urpf ---")
    pf(out_cfg3)

    has_ipv4 = 'address-family ipv4' in out_cfg3 and 'strict' in out_cfg3
    has_ipv6 = 'address-family ipv6' in out_cfg3 and 'loose' in out_cfg3
    record("Step 3: Per-AFI uRPF (ipv4 strict, ipv6 loose)",
           ok3 and has_ipv4 and has_ipv6,
           "urpf address-family ipv4 mode strict / ipv6 mode loose",
           out_cfg3,
           "show config reflects global + per-AFI sections",
           f"Commit={'OK' if ok3 else 'FAIL'}, ipv4-strict={'Yes' if has_ipv4 else 'No'}, ipv6-loose={'Yes' if has_ipv6 else 'No'}")

    # ================================================================
    # STEP 4: show interfaces / detail uRPF lines
    # ================================================================
    print("=" * 60)
    print(f"  STEP 4: show interfaces / detail uRPF lines ({PORT_A} is UP)")
    print("=" * 60)

    out_intf = run_show(chan, f'show interfaces {PORT_A}')
    print(f"\n--- show interfaces {PORT_A} (uRPF + state) ---")
    for line in out_intf.splitlines():
        if 'urpf' in line.lower() or 'Admin state' in line:
            print(f"  {line.strip()}")

    out_detail = run_show(chan, f'show interfaces detail {PORT_A}')
    print(f"\n--- show interfaces detail {PORT_A} (uRPF) ---")
    for line in out_detail.splitlines():
        if 'urpf' in line.lower():
            print(f"  {line.strip()}")

    urpf_show = 'urpf' in out_intf.lower()
    urpf_detail = 'urpf' in out_detail.lower()
    record("Step 4a: show interfaces uRPF lines (live UP port)",
           urpf_show,
           f"show interfaces {PORT_A}",
           out_intf,
           "uRPF IPv4/IPv6 check lines displayed",
           f"uRPF lines present: {'Yes' if urpf_show else 'No'}")
    record("Step 4b: show interfaces detail uRPF lines",
           urpf_detail,
           f"show interfaces detail {PORT_A}",
           out_detail,
           "uRPF check lines with correct mode",
           f"uRPF lines in detail: {'Yes' if urpf_detail else 'No'}")

    # ================================================================
    # STEP 5: Independent uRPF loose on Port B
    # ================================================================
    print("=" * 60)
    print(f"  STEP 5: Independent uRPF loose on {PORT_B}")
    print("=" * 60)

    enter_config(chan)
    run(chan, f'interfaces {PORT_B} ipv4-address 10.0.31.1/24')
    run(chan, f'interfaces {PORT_B} urpf admin-state enabled')
    run(chan, f'interfaces {PORT_B} urpf mode loose')
    run(chan, f'interfaces {PORT_B} urpf allow-default disabled')
    ok5, _ = commit(chan, "uRPF loose on Port B")
    exit_config(chan)

    out_pa = run_show(chan, f'show config interfaces {PORT_A} urpf')
    out_pb = run_show(chan, f'show config interfaces {PORT_B} urpf')
    print(f"\n--- {PORT_A} urpf ---")
    pf(out_pa)
    print(f"\n--- {PORT_B} urpf ---")
    pf(out_pb)

    pa_strict = 'strict' in out_pa
    pb_loose = 'loose' in out_pb
    record("Step 5: Independent uRPF on two breakout ports",
           ok5 and pa_strict and pb_loose,
           f"{PORT_A}=strict, {PORT_B}=loose",
           f"PortA:\n{out_pa}\nPortB:\n{out_pb}",
           "Two breakout ports from same parent have independent uRPF modes",
           f"Commit={'OK' if ok5 else 'FAIL'}, /0=strict={'Yes' if pa_strict else 'No'}, /1=loose={'Yes' if pb_loose else 'No'}")

    out_sa = run_show(chan, f'show interfaces {PORT_A}')
    out_sb = run_show(chan, f'show interfaces {PORT_B}')
    sa_strict = 'Mode: strict' in out_sa
    sb_loose = 'Mode: loose' in out_sb
    print(f"\n  show interfaces /0 uRPF:")
    for l in out_sa.splitlines():
        if 'urpf' in l.lower(): print(f"    {l.strip()}")
    print(f"  show interfaces /1 uRPF:")
    for l in out_sb.splitlines():
        if 'urpf' in l.lower(): print(f"    {l.strip()}")
    record("Step 5b: show interfaces confirms independent modes",
           sa_strict and sb_loose,
           f"show interfaces {PORT_A} / {PORT_B}",
           "",
           "/0 Mode: strict, /1 Mode: loose",
           f"/0 strict={'Yes' if sa_strict else 'No'}, /1 loose={'Yes' if sb_loose else 'No'}")

    # Remove uRPF from Port B before traffic test (so valid traffic can exit)
    enter_config(chan)
    run(chan, f'interfaces {PORT_B} no urpf')
    commit(chan, "remove uRPF from Port B for traffic test")
    exit_config(chan)

    # ================================================================
    # STEP 6: VLAN sub-interface with uRPF
    # ================================================================
    print("\n" + "=" * 60)
    print(f"  STEP 6: VLAN sub-interface {VLAN_SUBIF} with uRPF")
    print("=" * 60)

    enter_config(chan)
    run(chan, f'interfaces {VLAN_SUBIF} vlan-id 100')
    run(chan, f'interfaces {VLAN_SUBIF} urpf admin-state enabled')
    run(chan, f'interfaces {VLAN_SUBIF} urpf mode strict')
    run(chan, f'interfaces {VLAN_SUBIF} urpf allow-default disabled')
    ok6, _ = commit(chan, "uRPF on VLAN sub-if")
    exit_config(chan)

    out_subif = run_show(chan, f'show config interfaces {VLAN_SUBIF}')
    print(f"\n--- show config interfaces {VLAN_SUBIF} ---")
    pf(out_subif)

    subif_ok = 'urpf' in out_subif and 'admin-state enabled' in out_subif
    record("Step 6: uRPF on VLAN sub-interface of breakout port",
           ok6 and subif_ok,
           f"interfaces {VLAN_SUBIF} urpf admin-state enabled / mode strict",
           out_subif,
           "uRPF on sub-interface of breakout port works same as regular port",
           f"Commit={'OK' if ok6 else 'FAIL'}, urpf on subif={'Yes' if subif_ok else 'No'}")

    # Remove sub-if before traffic (would interfere with untagged traffic)
    enter_config(chan)
    run(chan, f'no interfaces {VLAN_SUBIF}')
    commit(chan, "remove sub-if for traffic test")
    exit_config(chan)

    # ================================================================
    # COUNTERS: show interfaces counters (uRPF lines present)
    # ================================================================
    print("\n" + "=" * 60)
    print(f"  COUNTERS BASELINE on {PORT_A}")
    print("=" * 60)

    out_ctr = run_show(chan, f'show interfaces counters {PORT_A}')
    ctr_lines = []
    for line in out_ctr.splitlines():
        if 'urpf' in line.lower():
            ctr_lines.append(line.strip())
            print(f"  {line.strip()}")

    ctr_present = len(ctr_lines) > 0
    record("Counters: uRPF counter lines present on breakout port",
           ctr_present,
           f"show interfaces counters {PORT_A}",
           '\n'.join(ctr_lines),
           "uRPF IPv4/IPv6 drop counter lines displayed",
           f"Counter lines present: {'Yes' if ctr_present else 'No'}")

    # Extract baseline drop count
    baseline_drops = 0
    for l in ctr_lines:
        if 'Ipv4' in l:
            nums = re.findall(r'(\d+)', l)
            if nums:
                baseline_drops = int(nums[0])
    print(f"  Baseline uRPF IPv4 drops: {baseline_drops}")

    # ================================================================
    # TRAFFIC PAUSE
    # ================================================================
    print("\n" + "=" * 60)
    print("  >>> READY FOR SPIRENT TRAFFIC <<<")
    print("=" * 60)
    print(f"\n  uRPF strict active on {PORT_A} (10.0.30.1/24)")
    print(f"  Destination: {PORT_B} (10.0.31.1/24)")
    print(f"\n  Spirent Port A (on {PORT_A}): IP 10.0.30.100/24, GW 10.0.30.1")
    print(f"  Spirent Port B (on {PORT_B}): IP 10.0.31.100/24, GW 10.0.31.1")
    print(f"\n  Traffic streams (Port A → Port B):")
    print(f"    VALID:   src=10.0.30.100  dst=10.0.31.100")
    print(f"    SPOOFED: src=10.99.99.99  dst=10.0.31.100")
    print(f"\n  Waiting for signal file: {SIGNAL_FILE}")
    print(f"  When done, run:  touch {SIGNAL_FILE}")

    while not os.path.exists(SIGNAL_FILE):
        time.sleep(5)

    print("\n  Signal received! Capturing post-traffic counters...\n")
    time.sleep(3)

    out_ctr_after = run_show(chan, f'show interfaces counters {PORT_A}')
    ctr_after = []
    for line in out_ctr_after.splitlines():
        if 'urpf' in line.lower():
            ctr_after.append(line.strip())
            print(f"  {line.strip()}")

    post_drops = 0
    for l in ctr_after:
        if 'Ipv4' in l:
            nums = re.findall(r'(\d+)', l)
            if nums:
                post_drops = int(nums[0])

    drops_increased = post_drops > baseline_drops
    delta = post_drops - baseline_drops
    print(f"\n  uRPF IPv4 drops: {baseline_drops} → {post_drops} (delta: {delta})")

    # RX on Port A
    for line in out_ctr_after.splitlines():
        if 'RX unicast' in line:
            print(f"  {line.strip()}")

    # TX on Port B
    out_ctr_b = run_show(chan, f'show interfaces counters {PORT_B}')
    for line in out_ctr_b.splitlines():
        if 'TX unicast' in line:
            print(f"  Port B {line.strip()}")

    record("Counters AFTER traffic: uRPF drops on breakout port",
           drops_increased,
           f"show interfaces counters {PORT_A}",
           '\n'.join(ctr_after),
           "uRPF IPv4 drops counter increased after spoofed traffic",
           f"Drops {baseline_drops} → {post_drops} (delta={delta}), increased={'Yes' if drops_increased else 'No'}")

    if os.path.exists(SIGNAL_FILE):
        os.remove(SIGNAL_FILE)

    # ================================================================
    # NEG-2: no breakout while ports admin-state up
    # ================================================================
    print("\n" + "=" * 60)
    print("  NEG-2: no breakout while ports admin-state up")
    print("=" * 60)

    enter_config(chan)
    run(chan, f'interfaces {PARENT_PORT}')
    run(chan, 'no breakout')
    ok_neg2, cout_neg2 = commit(chan, "no breakout while ports up — expect fail")
    run(chan, 'exit')  # exit interface context
    exit_config(chan)
    print(f"  Commit: {'OK (unexpected!)' if ok_neg2 else 'FAILED (expected)'}")
    record("Neg-2: no breakout while breakout ports admin-state up",
           not ok_neg2,
           f"no breakout (while child ports admin-state enabled)",
           cout_neg2,
           "Commit validation failure expected",
           f"Commit rejected as expected: {'Yes' if not ok_neg2 else 'No'}")

    # ================================================================
    # STEP 7: Cleanup
    # ================================================================
    print("\n" + "=" * 60)
    print("  STEP 7: Cleanup")
    print("=" * 60)

    enter_config(chan)
    run(chan, f'interfaces {PORT_A} no urpf')
    run(chan, f'interfaces {PORT_A} no ipv4-address')
    run(chan, f'interfaces {PORT_B} no ipv4-address')
    run(chan, f'interfaces {PORT_A} admin-state disabled')
    run(chan, f'interfaces {PORT_B} admin-state disabled')
    run(chan, f'interfaces {PORT_C} admin-state disabled')
    run(chan, f'interfaces {PORT_D} admin-state disabled')
    ok7a, _ = commit(chan, "remove config + admin-state down")

    run(chan, f'interfaces {PARENT_PORT}')
    run(chan, 'no breakout')
    ok7b, _ = commit(chan, "no breakout")
    run(chan, 'exit')  # exit interface context

    run(chan, f'interfaces {PARENT_PORT} admin-state enabled')
    ok7c, _ = commit(chan, "restore parent")
    exit_config(chan)

    out_bo_final = run_show(chan, 'show interfaces breakout')
    bo_none = False
    for line in out_bo_final.splitlines():
        if 'ge400-0/0/3 ' in line and 'none' in line:
            bo_none = True
            print(f"  {line.strip()}")

    out_purpf_final = run_show(chan, f'show config interfaces {PARENT_PORT} urpf')
    cfg_body = out_purpf_final.split('config-start')[-1].split('config-end')[0] if 'config-start' in out_purpf_final else ''
    no_stale = 'urpf' not in cfg_body

    record("Step 7: Cleanup — parent port restored",
           ok7a and ok7b and bo_none and no_stale,
           "no urpf, admin-state disabled, no breakout, admin-state enabled",
           "",
           "Parent port returns to normal, no stale uRPF config",
           f"Commits OK={ok7a and ok7b}, breakout=none={'Yes' if bo_none else 'No'}, no stale urpf={'Yes' if no_stale else 'No'}")

    # ================================================================
    # SUMMARY
    # ================================================================
    print("\n" + "=" * 60)
    print("  SUMMARY — SW-258863 Full Breakout + uRPF Test")
    print("=" * 60)

    all_pass = True
    for r in results:
        status = "PASS" if r['passed'] else "FAIL"
        if not r['passed']:
            all_pass = False
        print(f"  {status}  {r['step']}")

    overall = "PASS" if all_pass else "PARTIAL"
    print(f"\n  Overall: {overall}")
    print(f"  Device: NCP3-nog (WKY1C7VD00008P2)")
    print(f"  Version: DNOS 26.2.0 build 28_priv")
    print(f"  Execution: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}")

    output_path = "output/sw258863_full_results.json"
    with open(output_path, 'w') as f:
        json.dump({
            'ticket': 'SW-258863',
            'device': 'NCP3-nog (WKY1C7VD00008P2)',
            'version': 'DNOS 26.2.0 build 28_priv',
            'overall': overall,
            'results': results,
        }, f, indent=2)
    print(f"  Results saved to {output_path}")
    print("\n=== Done ===")

    ssh.close()
    return 0 if all_pass else 1


if __name__ == '__main__':
    sys.exit(main())
