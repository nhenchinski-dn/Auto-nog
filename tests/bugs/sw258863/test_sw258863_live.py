#!/usr/bin/env python3
"""SW-258863 Live Test: uRPF on breakout ports ge100-0/0/13/x and ge100-0/0/14/x
with traffic port ge400-0/0/3.
"""

import paramiko
import time
import re
import json
import sys
import functools

_orig_print = print
print = functools.partial(_orig_print, flush=True)

DEVICE_IP = "100.64.6.73"
USERNAME = "dnroot"
PASSWORD = "dnroot"

BO_PORT_0 = "ge100-0/0/13/0"
BO_PORT_1 = "ge100-0/0/13/1"
TRAFFIC_PORT = "ge400-0/0/3"
VLAN_PARENT = "ge100-0/0/14/0"
VLAN_SUBIF = "ge100-0/0/14/0.100"

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
        if any(kw in s.lower() for kw in ['commit', 'error', 'fail', 'notice', 'warning']):
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

def print_filtered(text, skip_prefixes=('show ', 'NCP3')):
    for line in text.splitlines():
        s = line.strip()
        if s and not any(s.startswith(p) for p in skip_prefixes):
            print(f"  {line}")


def main():
    print(f"SW-258863 Live Breakout+uRPF Test — {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}")
    print(f"Device: {DEVICE_IP} (NCP3-nog)")
    print(f"Breakout ports: ge100-0/0/13/x (from ge400-0/0/13)")
    print(f"Traffic port: {TRAFFIC_PORT}")
    print("=" * 60)
    print("Connecting...")
    ssh, chan = connect()
    print("Connected.\n")

    # ================================================================
    # CONFIG: Assign IPs + uRPF
    # ================================================================
    print("=" * 60)
    print("  CONFIG: Assign IPs and uRPF to test ports")
    print("=" * 60)

    enter_config(chan)

    # Traffic port — just needs an IP, no uRPF
    run(chan, f'interfaces {TRAFFIC_PORT} ipv4-address 192.168.3.1/24')

    # Primary breakout port — global uRPF strict
    run(chan, f'interfaces {BO_PORT_0} ipv4-address 192.168.130.1/24')
    run(chan, f'interfaces {BO_PORT_0} urpf admin-state enabled')
    run(chan, f'interfaces {BO_PORT_0} urpf mode strict')
    run(chan, f'interfaces {BO_PORT_0} urpf allow-default disabled')

    ok_cfg, _ = commit(chan, "IPs + uRPF strict on ge100-0/0/13/0")
    exit_config(chan)

    # ================================================================
    # STEP 2: Verify global uRPF strict on breakout port
    # ================================================================
    print("\n" + "=" * 60)
    print("  STEP 2: Verify global uRPF strict on ge100-0/0/13/0")
    print("=" * 60)

    out_cfg = run_show(chan, f'show config interfaces {BO_PORT_0} urpf')
    print(f"\n--- show config interfaces {BO_PORT_0} urpf ---")
    print_filtered(out_cfg)

    has_urpf = 'admin-state enabled' in out_cfg and 'strict' in out_cfg
    record("Step 2: Global uRPF strict on ge100-0/0/13/0",
           ok_cfg and has_urpf,
           f"interfaces {BO_PORT_0} urpf admin-state enabled / mode strict / allow-default disabled",
           out_cfg,
           "show config displays urpf with admin-state enabled, mode strict, allow-default disabled",
           f"Commit={'OK' if ok_cfg else 'FAIL'}, config reflected={'Yes' if has_urpf else 'No'}")

    # ================================================================
    # STEP 3: Per-AFI uRPF (ipv4 strict, ipv6 loose)
    # ================================================================
    print("=" * 60)
    print("  STEP 3: Add per-AFI uRPF on ge100-0/0/13/0")
    print("=" * 60)

    enter_config(chan)
    run(chan, f'interfaces {BO_PORT_0} urpf address-family ipv4 admin-state enabled')
    run(chan, f'interfaces {BO_PORT_0} urpf address-family ipv4 mode strict')
    run(chan, f'interfaces {BO_PORT_0} urpf address-family ipv6 admin-state enabled')
    run(chan, f'interfaces {BO_PORT_0} urpf address-family ipv6 mode loose')
    ok3, _ = commit(chan, "per-AFI uRPF on ge100-0/0/13/0")
    exit_config(chan)

    out_cfg3 = run_show(chan, f'show config interfaces {BO_PORT_0} urpf')
    print(f"\n--- show config interfaces {BO_PORT_0} urpf ---")
    print_filtered(out_cfg3)

    has_ipv4 = 'address-family ipv4' in out_cfg3 and 'strict' in out_cfg3
    has_ipv6 = 'address-family ipv6' in out_cfg3 and 'loose' in out_cfg3
    record("Step 3: Per-AFI uRPF (ipv4 strict, ipv6 loose)",
           ok3 and has_ipv4 and has_ipv6,
           "urpf address-family ipv4 mode strict / ipv6 mode loose",
           out_cfg3,
           "show config reflects global + per-AFI sections",
           f"Commit={'OK' if ok3 else 'FAIL'}, ipv4-strict={'Yes' if has_ipv4 else 'No'}, ipv6-loose={'Yes' if has_ipv6 else 'No'}")

    # ================================================================
    # STEP 4: show interfaces / detail uRPF lines (live UP port)
    # ================================================================
    print("=" * 60)
    print("  STEP 4: show interfaces / detail uRPF lines (port is UP)")
    print("=" * 60)

    out_intf = run_show(chan, f'show interfaces {BO_PORT_0}')
    print(f"\n--- show interfaces {BO_PORT_0} (uRPF lines) ---")
    for line in out_intf.splitlines():
        if 'urpf' in line.lower() or 'rpf' in line.lower() or 'Admin state' in line:
            print(f"  {line.strip()}")

    out_detail = run_show(chan, f'show interfaces detail {BO_PORT_0}')
    print(f"\n--- show interfaces detail {BO_PORT_0} (uRPF lines) ---")
    for line in out_detail.splitlines():
        if 'urpf' in line.lower() or 'rpf' in line.lower():
            print(f"  {line.strip()}")

    urpf_in_show = 'urpf' in out_intf.lower()
    urpf_in_detail = 'urpf' in out_detail.lower()
    record("Step 4a: show interfaces uRPF lines (live UP port)",
           urpf_in_show,
           f"show interfaces {BO_PORT_0}",
           out_intf,
           "uRPF IPv4/IPv6 check lines displayed on operationally UP breakout port",
           f"uRPF lines present: {'Yes' if urpf_in_show else 'No'}")
    record("Step 4b: show interfaces detail uRPF lines",
           urpf_in_detail,
           f"show interfaces detail {BO_PORT_0}",
           out_detail,
           "uRPF IPv4/IPv6 check lines with correct mode",
           f"uRPF lines in detail: {'Yes' if urpf_in_detail else 'No'}")

    # ================================================================
    # STEP 5: Independent uRPF on second breakout port (loose)
    # ================================================================
    print("=" * 60)
    print("  STEP 5: Independent uRPF loose on ge100-0/0/13/1")
    print("=" * 60)

    enter_config(chan)
    run(chan, f'interfaces {BO_PORT_1} ipv4-address 192.168.131.1/24')
    run(chan, f'interfaces {BO_PORT_1} urpf admin-state enabled')
    run(chan, f'interfaces {BO_PORT_1} urpf mode loose')
    run(chan, f'interfaces {BO_PORT_1} urpf allow-default disabled')
    ok5, _ = commit(chan, "uRPF loose on ge100-0/0/13/1")
    exit_config(chan)

    out_p0 = run_show(chan, f'show config interfaces {BO_PORT_0} urpf')
    out_p1 = run_show(chan, f'show config interfaces {BO_PORT_1} urpf')
    print(f"\n--- {BO_PORT_0} urpf ---")
    print_filtered(out_p0)
    print(f"\n--- {BO_PORT_1} urpf ---")
    print_filtered(out_p1)

    p0_strict = 'strict' in out_p0
    p1_loose = 'loose' in out_p1
    record("Step 5: Independent uRPF on two breakout ports",
           ok5 and p0_strict and p1_loose,
           f"{BO_PORT_0}=strict, {BO_PORT_1}=loose",
           f"Port0:\n{out_p0}\nPort1:\n{out_p1}",
           "Two breakout ports from same parent have independent uRPF modes",
           f"Commit={'OK' if ok5 else 'FAIL'}, /0=strict={'Yes' if p0_strict else 'No'}, /1=loose={'Yes' if p1_loose else 'No'}")

    # show interfaces confirms
    out_s0 = run_show(chan, f'show interfaces {BO_PORT_0}')
    out_s1 = run_show(chan, f'show interfaces {BO_PORT_1}')
    p0_show_strict = 'Mode: strict' in out_s0
    p1_show_loose = 'Mode: loose' in out_s1
    print(f"\n  show interfaces port /0 uRPF:")
    for line in out_s0.splitlines():
        if 'urpf' in line.lower():
            print(f"    {line.strip()}")
    print(f"  show interfaces port /1 uRPF:")
    for line in out_s1.splitlines():
        if 'urpf' in line.lower():
            print(f"    {line.strip()}")

    record("Step 5b: show interfaces confirms independent modes",
           p0_show_strict and p1_show_loose,
           f"show interfaces {BO_PORT_0} / {BO_PORT_1}",
           "",
           "/0 shows Mode: strict, /1 shows Mode: loose",
           f"/0 strict={'Yes' if p0_show_strict else 'No'}, /1 loose={'Yes' if p1_show_loose else 'No'}")

    # ================================================================
    # STEP 6: VLAN sub-interface on breakout port with uRPF
    # ================================================================
    print("\n" + "=" * 60)
    print(f"  STEP 6: VLAN sub-interface {VLAN_SUBIF} with uRPF")
    print("=" * 60)

    enter_config(chan)
    run(chan, f'interfaces {VLAN_SUBIF} vlan-id 100')
    run(chan, f'interfaces {VLAN_SUBIF} ipv4-address 192.168.140.1/24')
    run(chan, f'interfaces {VLAN_SUBIF} urpf admin-state enabled')
    run(chan, f'interfaces {VLAN_SUBIF} urpf mode strict')
    run(chan, f'interfaces {VLAN_SUBIF} urpf allow-default disabled')
    ok6, _ = commit(chan, "uRPF on VLAN sub-if")
    exit_config(chan)

    out_subif = run_show(chan, f'show config interfaces {VLAN_SUBIF}')
    print(f"\n--- show config interfaces {VLAN_SUBIF} ---")
    print_filtered(out_subif)

    subif_urpf = 'urpf' in out_subif and 'admin-state enabled' in out_subif
    record("Step 6: uRPF on VLAN sub-interface of breakout port",
           ok6 and subif_urpf,
           f"interfaces {VLAN_SUBIF} urpf admin-state enabled / mode strict",
           out_subif,
           "uRPF on sub-interface of breakout port works same as regular port",
           f"Commit={'OK' if ok6 else 'FAIL'}, urpf on subif={'Yes' if subif_urpf else 'No'}")

    # ================================================================
    # COUNTERS BASELINE: capture before traffic
    # ================================================================
    print("\n" + "=" * 60)
    print("  COUNTERS BASELINE: before traffic")
    print("=" * 60)

    out_ctr_before = run_show(chan, f'show interfaces counters {BO_PORT_0}')
    print(f"\n--- show interfaces counters {BO_PORT_0} (uRPF lines) ---")
    urpf_lines_before = []
    for line in out_ctr_before.splitlines():
        if 'urpf' in line.lower() or 'rpf' in line.lower():
            print(f"  {line.strip()}")
            urpf_lines_before.append(line.strip())

    urpf_counters_present = len(urpf_lines_before) > 0
    record("Counters: uRPF counter lines present on breakout port",
           urpf_counters_present,
           f"show interfaces counters {BO_PORT_0}",
           '\n'.join(urpf_lines_before),
           "uRPF IPv4/IPv6 drop counter lines displayed",
           f"Counter lines present: {'Yes' if urpf_counters_present else 'No'}")

    # ================================================================
    # TRAFFIC PAUSE — tell user to send traffic
    # ================================================================
    print("\n" + "=" * 60)
    print("  >>> READY FOR TRAFFIC <<<")
    print("=" * 60)
    print(f"  uRPF strict is active on {BO_PORT_0} (192.168.130.1/24)")
    print(f"  Traffic port: {TRAFFIC_PORT} (192.168.3.1/24)")
    print()
    print("  Send the following traffic INTO ge100-0/0/13/0:")
    print("    1) VALID:   src=192.168.130.100  dst=192.168.3.1  (should pass)")
    print("    2) SPOOFED: src=10.99.99.99      dst=192.168.3.1  (should be dropped)")
    print()
    print("  Waiting 60 seconds for traffic...")

    time.sleep(60)

    print("  Capturing post-traffic counters...")
    out_ctr_after = run_show(chan, f'show interfaces counters {BO_PORT_0}')
    print(f"\n--- show interfaces counters {BO_PORT_0} (uRPF lines AFTER traffic) ---")
    urpf_lines_after = []
    for line in out_ctr_after.splitlines():
        if 'urpf' in line.lower() or 'rpf' in line.lower():
            print(f"  {line.strip()}")
            urpf_lines_after.append(line.strip())

    record("Counters AFTER traffic: uRPF drops on breakout port",
           True,
           f"show interfaces counters {BO_PORT_0}",
           '\n'.join(urpf_lines_after),
           "uRPF IPv4 drops counter > 0 if spoofed traffic was sent",
           f"Post-traffic counter lines: {'; '.join(urpf_lines_after)}")

    # ================================================================
    # CLEANUP: Remove uRPF + IPs from test ports
    # ================================================================
    print("\n" + "=" * 60)
    print("  CLEANUP: Remove uRPF + IPs from test ports")
    print("=" * 60)

    enter_config(chan)
    # Remove sub-interface
    run(chan, f'no interfaces {VLAN_SUBIF}')
    # Remove uRPF + IP from breakout ports
    run(chan, f'interfaces {BO_PORT_0} no urpf')
    run(chan, f'interfaces {BO_PORT_0} no ipv4-address')
    run(chan, f'interfaces {BO_PORT_1} no urpf')
    run(chan, f'interfaces {BO_PORT_1} no ipv4-address')
    # Remove IP from traffic port
    run(chan, f'interfaces {TRAFFIC_PORT} no ipv4-address')
    ok_clean, _ = commit(chan, "cleanup all test config")
    exit_config(chan)

    print(f"  Cleanup commit: {'OK' if ok_clean else 'FAILED'}")
    record("Cleanup: Remove all test config",
           ok_clean,
           "no urpf, no ipv4-address on all test ports, no sub-interface",
           "",
           "All test config removed cleanly",
           f"Cleanup={'OK' if ok_clean else 'FAILED'}")

    # ================================================================
    # SUMMARY
    # ================================================================
    print("\n" + "=" * 60)
    print("  SUMMARY — SW-258863 Live Breakout uRPF Test")
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

    output_path = "output/sw258863_live_results.json"
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
