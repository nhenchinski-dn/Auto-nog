#!/usr/bin/env python3
"""SW-258863: CLI | Breakout interface — uRPF on breakout ports
Device: NCP3-nog (WKY1C7VD00008P2) @ 100.64.6.73
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
PARENT_PORT = "ge400-0/0/26"
BO_PORT_0 = "ge100-0/0/26/0"
BO_PORT_1 = "ge100-0/0/26/1"
BO_PORT_2 = "ge100-0/0/26/2"
BO_PORT_3 = "ge100-0/0/26/3"
VLAN_SUBIF = "ge100-0/0/26/0.100"

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
    out = run(chan, 'commit', wait=15)
    ok = 'Commit succeeded' in out
    tag = f" [{label}]" if label else ""
    print(f">>> COMMIT{tag}")
    print(f"  Result: {'OK' if ok else 'FAILED'}")
    for line in out.splitlines():
        if 'commit' in line.lower() or 'error' in line.lower() or 'fail' in line.lower() or 'notice' in line.lower():
            print(f"    {line.strip()}")
    if not ok:
        run(chan, 'rollback 0', wait=5)
        print("  (rolled back)")
    return ok, out

def enter_config(chan):
    run(chan, 'configure')

def exit_config(chan):
    run(chan, 'exit')
    time.sleep(2)
    if chan.recv_ready():
        pending = chan.recv(65535).decode(errors='replace')
        if 'uncommitted' in pending.lower():
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


def main():
    print(f"SW-258863 Breakout+uRPF Test — {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}")
    print(f"Device: {DEVICE_IP} (NCP3-nog / WKY1C7VD00008P2)")
    print("=" * 60)

    print("Connecting...")
    ssh, chan = connect()
    print("Connected.\n")

    # ================================================================
    # PRE-STEP: Clear any pending config, remove LLDP
    # ================================================================
    enter_config(chan)
    run(chan, 'rollback 0', wait=5)
    exit_config(chan)

    # ================================================================
    # STEP 1: Break out parent port
    # ================================================================
    print("=" * 60)
    print("  STEP 1: Break out ge400-0/0/26 into 4x100GE")
    print("=" * 60)

    enter_config(chan)
    run(chan, f'no protocols lldp interface {PARENT_PORT}')
    run(chan, f'interfaces {PARENT_PORT} admin-state disabled')
    ok, cout = commit(chan, "remove LLDP + admin-state disabled")

    run(chan, f'interfaces {PARENT_PORT} breakout 100g-4x')
    ok_bo, cout_bo = commit(chan, "breakout 100g-4x")
    exit_config(chan)

    out_bo = run_show(chan, 'show interfaces breakout')
    print(f"\n--- show interfaces breakout (filtered) ---")
    for line in out_bo.splitlines():
        if '26' in line or 'Port' in line or '---' in line:
            print(f"  {line}")

    has_breakout = 'b100g-4x' in out_bo and 'ge100-0/0/26' in out_bo
    record("Step 1: Breakout ge400-0/0/26 → 4x100GE",
           ok_bo and has_breakout,
           f"interfaces {PARENT_PORT} breakout 100g-4x",
           out_bo,
           "Commit succeeds, show interfaces breakout shows b100g-4x with ge100-0/0/26/0-3",
           f"Commit={'OK' if ok_bo else 'FAIL'}, breakout visible={'Yes' if has_breakout else 'No'}")

    # Neg-1: parent port urpf after breakout should be empty
    print("\n--- Neg-1: show config parent port urpf after breakout ---")
    out_parent_urpf = run_show(chan, f'show config interfaces {PARENT_PORT} urpf')
    print(f"  {out_parent_urpf.strip()}")
    parent_urpf_empty = 'urpf' not in out_parent_urpf.split('config-start')[-1].split('config-end')[0] if 'config-start' in out_parent_urpf else True
    record("Neg-1: Parent port urpf after breakout",
           parent_urpf_empty,
           f"show config interfaces {PARENT_PORT} urpf",
           out_parent_urpf,
           "Empty — parent config erased on breakout",
           f"Parent urpf config {'empty as expected' if parent_urpf_empty else 'NOT empty — unexpected'}")

    # ================================================================
    # STEP 2: Global uRPF strict on first breakout port
    # ================================================================
    print("\n" + "=" * 60)
    print("  STEP 2: Configure global uRPF strict on ge100-0/0/26/0")
    print("=" * 60)

    enter_config(chan)
    run(chan, f'interfaces {BO_PORT_0} admin-state enabled')
    run(chan, f'interfaces {BO_PORT_0} urpf admin-state enabled')
    run(chan, f'interfaces {BO_PORT_0} urpf mode strict')
    run(chan, f'interfaces {BO_PORT_0} urpf allow-default disabled')
    ok2, cout2 = commit(chan, "uRPF strict on BO port 0")
    exit_config(chan)

    out_cfg = run_show(chan, f'show config interfaces {BO_PORT_0} urpf')
    print(f"\n--- show config interfaces {BO_PORT_0} urpf ---")
    print(f"  {out_cfg.strip()}")

    has_urpf = 'admin-state enabled' in out_cfg and 'strict' in out_cfg
    record("Step 2: Global uRPF strict on ge100-0/0/26/0",
           ok2 and has_urpf,
           f"interfaces {BO_PORT_0} urpf admin-state enabled / mode strict / allow-default disabled",
           out_cfg,
           "show config displays urpf with admin-state enabled, mode strict, allow-default disabled",
           f"Commit={'OK' if ok2 else 'FAIL'}, config reflected={'Yes' if has_urpf else 'No'}")

    out_set = run_show(chan, f'show config interfaces {BO_PORT_0} urpf | display set')
    print(f"\n--- display set ---")
    print(f"  {out_set.strip()}")

    # ================================================================
    # STEP 3: Per-AFI uRPF (ipv4 strict, ipv6 loose)
    # ================================================================
    print("\n" + "=" * 60)
    print("  STEP 3: Add per-AFI uRPF on ge100-0/0/26/0")
    print("=" * 60)

    enter_config(chan)
    run(chan, f'interfaces {BO_PORT_0} urpf address-family ipv4 admin-state enabled')
    run(chan, f'interfaces {BO_PORT_0} urpf address-family ipv4 mode strict')
    run(chan, f'interfaces {BO_PORT_0} urpf address-family ipv6 admin-state enabled')
    run(chan, f'interfaces {BO_PORT_0} urpf address-family ipv6 mode loose')
    ok3, cout3 = commit(chan, "per-AFI uRPF")
    exit_config(chan)

    out_cfg3 = run_show(chan, f'show config interfaces {BO_PORT_0} urpf')
    print(f"\n--- show config interfaces {BO_PORT_0} urpf ---")
    print(f"  {out_cfg3.strip()}")

    has_ipv4 = 'address-family ipv4' in out_cfg3 and 'strict' in out_cfg3
    has_ipv6 = 'address-family ipv6' in out_cfg3 and 'loose' in out_cfg3
    record("Step 3: Per-AFI uRPF (ipv4 strict, ipv6 loose)",
           ok3 and has_ipv4 and has_ipv6,
           "urpf address-family ipv4 mode strict / ipv6 mode loose",
           out_cfg3,
           "show config reflects global + per-AFI sections with correct modes",
           f"Commit={'OK' if ok3 else 'FAIL'}, ipv4-strict={'Yes' if has_ipv4 else 'No'}, ipv6-loose={'Yes' if has_ipv6 else 'No'}")

    # ================================================================
    # STEP 4: show interfaces / show interfaces detail
    # ================================================================
    print("\n" + "=" * 60)
    print("  STEP 4: Verify show interfaces / detail uRPF lines")
    print("=" * 60)

    out_intf = run_show(chan, f'show interfaces {BO_PORT_0}')
    print(f"\n--- show interfaces {BO_PORT_0} ---")
    print(f"  {out_intf.strip()}")

    out_detail = run_show(chan, f'show interfaces detail {BO_PORT_0}')
    print(f"\n--- show interfaces detail {BO_PORT_0} ---")
    print(f"  {out_detail.strip()}")

    urpf_in_show = 'urpf' in out_intf.lower() or 'rpf' in out_intf.lower()
    urpf_in_detail = 'urpf' in out_detail.lower() or 'rpf' in out_detail.lower()
    record("Step 4a: show interfaces uRPF lines",
           urpf_in_show,
           f"show interfaces {BO_PORT_0}",
           out_intf,
           "uRPF IPv4/IPv6 check lines displayed",
           f"uRPF lines in show interfaces: {'Yes' if urpf_in_show else 'No'}")
    record("Step 4b: show interfaces detail uRPF lines",
           urpf_in_detail,
           f"show interfaces detail {BO_PORT_0}",
           out_detail,
           "uRPF IPv4/IPv6 check lines displayed with correct mode and allow-default",
           f"uRPF lines in detail: {'Yes' if urpf_in_detail else 'No'}")

    # ================================================================
    # STEP 5: Independent uRPF on second breakout port (loose)
    # ================================================================
    print("\n" + "=" * 60)
    print("  STEP 5: Configure uRPF loose on ge100-0/0/26/1")
    print("=" * 60)

    enter_config(chan)
    run(chan, f'interfaces {BO_PORT_1} admin-state enabled')
    run(chan, f'interfaces {BO_PORT_1} urpf admin-state enabled')
    run(chan, f'interfaces {BO_PORT_1} urpf mode loose')
    run(chan, f'interfaces {BO_PORT_1} urpf allow-default disabled')
    ok5, cout5 = commit(chan, "uRPF loose on BO port 1")
    exit_config(chan)

    out_p0 = run_show(chan, f'show config interfaces {BO_PORT_0} urpf')
    out_p1 = run_show(chan, f'show config interfaces {BO_PORT_1} urpf')
    print(f"\n--- {BO_PORT_0} urpf ---")
    print(f"  {out_p0.strip()}")
    print(f"\n--- {BO_PORT_1} urpf ---")
    print(f"  {out_p1.strip()}")

    p0_strict = 'strict' in out_p0
    p1_loose = 'loose' in out_p1
    independent = p0_strict and p1_loose
    record("Step 5: Independent uRPF on two breakout ports",
           ok5 and independent,
           f"{BO_PORT_0}=strict, {BO_PORT_1}=loose",
           f"Port0:\n{out_p0}\nPort1:\n{out_p1}",
           "Two breakout ports from same parent have independent uRPF states",
           f"Commit={'OK' if ok5 else 'FAIL'}, port0=strict={'Yes' if p0_strict else 'No'}, port1=loose={'Yes' if p1_loose else 'No'}")

    # ================================================================
    # STEP 6: VLAN sub-interface on breakout port with uRPF
    # ================================================================
    print("\n" + "=" * 60)
    print("  STEP 6: VLAN sub-interface ge100-0/0/26/0.100 with uRPF")
    print("=" * 60)

    enter_config(chan)
    run(chan, f'interfaces {VLAN_SUBIF} vlan-id 100')
    run(chan, f'interfaces {VLAN_SUBIF} urpf admin-state enabled')
    run(chan, f'interfaces {VLAN_SUBIF} urpf mode strict')
    ok6, cout6 = commit(chan, "uRPF on VLAN sub-if")
    exit_config(chan)

    out_subif = run_show(chan, f'show config interfaces {VLAN_SUBIF}')
    print(f"\n--- show config interfaces {VLAN_SUBIF} ---")
    print(f"  {out_subif.strip()}")

    out_subif_urpf = run_show(chan, f'show config interfaces {VLAN_SUBIF} urpf')
    print(f"\n--- show config interfaces {VLAN_SUBIF} urpf ---")
    print(f"  {out_subif_urpf.strip()}")

    subif_urpf = 'urpf' in out_subif and 'admin-state enabled' in out_subif_urpf
    record("Step 6: uRPF on VLAN sub-interface of breakout port",
           ok6 and subif_urpf,
           f"interfaces {VLAN_SUBIF} urpf admin-state enabled / mode strict",
           out_subif_urpf,
           "uRPF on sub-interface of breakout port works same as regular port",
           f"Commit={'OK' if ok6 else 'FAIL'}, urpf on subif={'Yes' if subif_urpf else 'No'}")

    # ================================================================
    # COUNTERS: show interfaces counters for uRPF drop lines
    # ================================================================
    print("\n" + "=" * 60)
    print("  COUNTERS: show interfaces counters for uRPF drop lines")
    print("=" * 60)

    out_ctr = run_show(chan, f'show interfaces counters {BO_PORT_0}')
    print(f"\n--- show interfaces counters {BO_PORT_0} ---")
    print(f"  {out_ctr.strip()}")

    urpf_counters = 'urpf' in out_ctr.lower() or 'rpf' in out_ctr.lower()
    record("Counters: uRPF drop counter lines on breakout port",
           urpf_counters,
           f"show interfaces counters {BO_PORT_0}",
           out_ctr,
           "uRPF IPv4/IPv6 drop counter lines displayed",
           f"uRPF counter lines present: {'Yes' if urpf_counters else 'No'}")

    # ================================================================
    # NEG-2: Attempt no breakout while port admin-state up
    # ================================================================
    print("\n" + "=" * 60)
    print("  NEG-2: Attempt no breakout while port admin-state up")
    print("=" * 60)

    enter_config(chan)
    run(chan, f'interfaces {PARENT_PORT} no breakout')
    ok_neg2, cout_neg2 = commit(chan, "no breakout while ports up — expect fail")
    print(f"  Commit result: {'OK (unexpected!)' if ok_neg2 else 'FAILED (expected)'}")
    exit_config(chan)

    record("Neg-2: no breakout while breakout ports admin-state up",
           not ok_neg2,
           f"interfaces {PARENT_PORT} no breakout (while BO ports up)",
           cout_neg2,
           "Commit validation failure expected",
           f"Commit rejected as expected: {'Yes' if not ok_neg2 else 'No — commit unexpectedly succeeded'}")

    # ================================================================
    # STEP 7: Cleanup
    # ================================================================
    print("\n" + "=" * 60)
    print("  STEP 7: Cleanup — remove uRPF, admin-state down, no breakout")
    print("=" * 60)

    enter_config(chan)
    # Remove sub-interface first
    run(chan, f'no interfaces {VLAN_SUBIF}')
    # Remove uRPF from BO ports
    run(chan, f'interfaces {BO_PORT_0} no urpf')
    run(chan, f'interfaces {BO_PORT_1} no urpf')
    # Admin-state down on all BO ports
    run(chan, f'interfaces {BO_PORT_0} admin-state disabled')
    run(chan, f'interfaces {BO_PORT_1} admin-state disabled')
    run(chan, f'interfaces {BO_PORT_2} admin-state disabled')
    run(chan, f'interfaces {BO_PORT_3} admin-state disabled')
    ok7a, cout7a = commit(chan, "remove uRPF + admin-state down")

    # Remove breakout
    run(chan, f'interfaces {PARENT_PORT} no breakout')
    ok7b, cout7b = commit(chan, "no breakout")

    # Restore parent admin-state and LLDP
    run(chan, f'interfaces {PARENT_PORT} admin-state enabled')
    run(chan, f'protocols lldp interface {PARENT_PORT}')
    ok7c, cout7c = commit(chan, "restore parent admin-state + LLDP")
    exit_config(chan)

    # Verify clean
    out_bo_final = run_show(chan, 'show interfaces breakout')
    print(f"\n--- show interfaces breakout (port 26) ---")
    for line in out_bo_final.splitlines():
        if '26' in line or 'Port' in line or '---' in line:
            print(f"  {line}")

    out_parent_final = run_show(chan, f'show config interfaces {PARENT_PORT}')
    print(f"\n--- show config interfaces {PARENT_PORT} ---")
    print(f"  {out_parent_final.strip()}")

    out_parent_urpf_final = run_show(chan, f'show config interfaces {PARENT_PORT} urpf')
    print(f"\n--- show config interfaces {PARENT_PORT} urpf ---")
    print(f"  {out_parent_urpf_final.strip()}")

    breakout_removed = PARENT_PORT in out_bo_final and 'none' in out_bo_final.split(PARENT_PORT)[-1].split('\n')[0]
    no_stale_urpf = 'urpf' not in out_parent_urpf_final.split('config-start')[-1].split('config-end')[0] if 'config-start' in out_parent_urpf_final else True

    record("Step 7: Cleanup — parent port clean after no breakout",
           ok7a and ok7b and breakout_removed and no_stale_urpf,
           "no urpf, admin-state disabled, no breakout, admin-state enabled",
           f"breakout:\n{out_bo_final}\nparent config:\n{out_parent_final}\nparent urpf:\n{out_parent_urpf_final}",
           "Parent port returns to normal, no stale uRPF config",
           f"Cleanup commits={'OK' if ok7a and ok7b else 'FAIL'}, breakout=none={'Yes' if breakout_removed else 'No'}, no stale urpf={'Yes' if no_stale_urpf else 'No'}")

    # ================================================================
    # SUMMARY
    # ================================================================
    print("\n" + "=" * 60)
    print("  SUMMARY — SW-258863: CLI | Breakout interface + uRPF")
    print("=" * 60)

    all_pass = True
    for r in results:
        status = "PASS" if r['passed'] else "FAIL"
        if not r['passed']:
            all_pass = False
        print(f"  {status}  {r['step']}")

    overall = "PASS" if all_pass else "FAIL"
    print(f"\n  Overall: {overall}")
    print(f"  Device: NCP3-nog (WKY1C7VD00008P2)")
    print(f"  Version: DNOS 26.2.0 build 28_priv")
    print(f"  Execution: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}")

    # Save results to JSON
    output_path = f"output/sw258863_breakout_urpf_results.json"
    with open(output_path, 'w') as f:
        json.dump({
            'ticket': 'SW-258863',
            'device': 'NCP3-nog (WKY1C7VD00008P2)',
            'version': 'DNOS 26.2.0 build 28_priv',
            'overall': overall,
            'results': results,
        }, f, indent=2)
    print(f"\n  Results saved to {output_path}")
    print("\n=== Done ===")

    ssh.close()
    return 0 if all_pass else 1


if __name__ == '__main__':
    sys.exit(main())
