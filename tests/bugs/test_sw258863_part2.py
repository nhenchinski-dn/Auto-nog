#!/usr/bin/env python3
"""SW-258863 Part 2: Steps 5-7, Counters, Neg-2, Cleanup
Continues from where Part 1 left off (breakout active, uRPF on port 0).
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


def main():
    print(f"SW-258863 Part 2 — {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}")
    print(f"Device: {DEVICE_IP} (NCP3-nog)")
    print("=" * 60)
    print("Connecting...")
    ssh, chan = connect()
    print("Connected.\n")

    # ================================================================
    # STEP 5: Independent uRPF on second breakout port (loose)
    # allow-default must match port 0 (disabled) per VRF constraint
    # ================================================================
    print("=" * 60)
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
    for line in out_p0.splitlines():
        if line.strip() and not line.strip().startswith('show ') and not line.strip().startswith('NCP'):
            print(f"  {line}")
    print(f"\n--- {BO_PORT_1} urpf ---")
    for line in out_p1.splitlines():
        if line.strip() and not line.strip().startswith('show ') and not line.strip().startswith('NCP'):
            print(f"  {line}")

    p0_strict = 'strict' in out_p0
    p1_loose = 'loose' in out_p1
    independent = p0_strict and p1_loose
    record("Step 5: Independent uRPF on two breakout ports",
           ok5 and independent,
           f"{BO_PORT_0}=strict, {BO_PORT_1}=loose",
           f"Port0:\n{out_p0}\nPort1:\n{out_p1}",
           "Two breakout ports from same parent have independent uRPF modes",
           f"Commit={'OK' if ok5 else 'FAIL'}, port0=strict={'Yes' if p0_strict else 'No'}, port1=loose={'Yes' if p1_loose else 'No'}")

    # Also verify via show interfaces that modes differ
    out_show_p0 = run_show(chan, f'show interfaces {BO_PORT_0}')
    out_show_p1 = run_show(chan, f'show interfaces {BO_PORT_1}')

    p0_show_strict = 'Mode: strict' in out_show_p0
    p1_show_loose = 'Mode: loose' in out_show_p1
    print(f"  show interfaces port0 uRPF: ", end="")
    for line in out_show_p0.splitlines():
        if 'urpf' in line.lower():
            print(f"    {line.strip()}")
    print(f"  show interfaces port1 uRPF: ", end="")
    for line in out_show_p1.splitlines():
        if 'urpf' in line.lower():
            print(f"    {line.strip()}")

    record("Step 5b: show interfaces confirms independent modes",
           p0_show_strict and p1_show_loose,
           f"show interfaces {BO_PORT_0} / {BO_PORT_1}",
           "",
           "port0 shows Mode: strict, port1 shows Mode: loose",
           f"port0 strict in show={'Yes' if p0_show_strict else 'No'}, port1 loose in show={'Yes' if p1_show_loose else 'No'}")

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
    run(chan, f'interfaces {VLAN_SUBIF} urpf allow-default disabled')
    ok6, cout6 = commit(chan, "uRPF on VLAN sub-if")
    exit_config(chan)

    out_subif = run_show(chan, f'show config interfaces {VLAN_SUBIF}')
    print(f"\n--- show config interfaces {VLAN_SUBIF} ---")
    for line in out_subif.splitlines():
        if line.strip() and not line.strip().startswith('show ') and not line.strip().startswith('NCP'):
            print(f"  {line}")

    out_subif_urpf = run_show(chan, f'show config interfaces {VLAN_SUBIF} urpf')
    print(f"\n--- show config interfaces {VLAN_SUBIF} urpf ---")
    for line in out_subif_urpf.splitlines():
        if line.strip() and not line.strip().startswith('show ') and not line.strip().startswith('NCP'):
            print(f"  {line}")

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
    for line in out_ctr.splitlines():
        if 'urpf' in line.lower() or 'rpf' in line.lower():
            print(f"  {line}")
    if not any('rpf' in l.lower() for l in out_ctr.splitlines()):
        print("  (no uRPF counter lines found)")

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
    run(chan, f'no interfaces {VLAN_SUBIF}')
    run(chan, f'interfaces {BO_PORT_0} no urpf')
    run(chan, f'interfaces {BO_PORT_1} no urpf')
    run(chan, f'interfaces {BO_PORT_0} admin-state disabled')
    run(chan, f'interfaces {BO_PORT_1} admin-state disabled')
    run(chan, f'interfaces {BO_PORT_2} admin-state disabled')
    run(chan, f'interfaces {BO_PORT_3} admin-state disabled')
    ok7a, cout7a = commit(chan, "remove uRPF + admin-state down")

    run(chan, f'interfaces {PARENT_PORT} no breakout')
    ok7b, cout7b = commit(chan, "no breakout")

    run(chan, f'interfaces {PARENT_PORT} admin-state enabled')
    run(chan, f'protocols lldp interface {PARENT_PORT}')
    ok7c, cout7c = commit(chan, "restore parent admin-state + LLDP")
    exit_config(chan)

    out_bo_final = run_show(chan, 'show interfaces breakout')
    print(f"\n--- show interfaces breakout (port 26) ---")
    for line in out_bo_final.splitlines():
        if '26' in line or 'Port' in line:
            print(f"  {line}")

    out_parent_final = run_show(chan, f'show config interfaces {PARENT_PORT}')
    print(f"\n--- show config interfaces {PARENT_PORT} ---")
    for line in out_parent_final.splitlines():
        if line.strip() and not line.strip().startswith('show ') and not line.strip().startswith('NCP'):
            print(f"  {line}")

    out_parent_urpf_final = run_show(chan, f'show config interfaces {PARENT_PORT} urpf')
    print(f"\n--- show config interfaces {PARENT_PORT} urpf ---")
    for line in out_parent_urpf_final.splitlines():
        if line.strip() and not line.strip().startswith('show ') and not line.strip().startswith('NCP'):
            print(f"  {line}")

    breakout_removed = PARENT_PORT in out_bo_final and 'none' in out_bo_final.split(PARENT_PORT)[-1].split('\n')[0]
    config_body = out_parent_urpf_final.split('config-start')[-1].split('config-end')[0] if 'config-start' in out_parent_urpf_final else ''
    no_stale_urpf = 'urpf' not in config_body

    record("Step 7: Cleanup — parent port clean after no breakout",
           ok7a and ok7b and breakout_removed and no_stale_urpf,
           "no urpf, admin-state disabled, no breakout, admin-state enabled",
           f"parent urpf:\n{out_parent_urpf_final}",
           "Parent port returns to normal, no stale uRPF config",
           f"Cleanup commits={'OK' if ok7a and ok7b else 'FAIL'}, breakout=none={'Yes' if breakout_removed else 'No'}, no stale urpf={'Yes' if no_stale_urpf else 'No'}")

    # ================================================================
    # SUMMARY
    # ================================================================
    print("\n" + "=" * 60)
    print("  SUMMARY — SW-258863 Part 2: Steps 5-7 + Counters + Neg-2")
    print("=" * 60)

    all_pass = True
    for r in results:
        status = "PASS" if r['passed'] else "FAIL"
        if not r['passed']:
            all_pass = False
        print(f"  {status}  {r['step']}")

    overall = "PASS" if all_pass else "PARTIAL"
    print(f"\n  Part 2 Overall: {overall}")
    print(f"  Execution: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}")

    output_path = "output/sw258863_breakout_urpf_part2_results.json"
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
