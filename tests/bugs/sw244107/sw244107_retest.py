#!/usr/bin/env python3
"""SW-244107 re-run: Egress ACL (DENY) + uBFD / BGP-BFD — same VRF as bundle-10.

Changes vs. the first attempt
  * Moves ge100-0/0/3/0 into VRF 'test' (where bundle-10 lives) — without this
    the DUT dropped everything with "Destination route unreachable".
  * ACL rule 1 DENY (not allow).
  * Phase C uses BGP BFD (SH-BFD in BGP) instead of SR-MPLS seamless-bfd.
  * Handles the "out-of-sync" commit prompt robustly.

Topology
  Spirent 1/25 -- (untagged 100G) --> ge100-0/0/3/0 (VRF test, 10.10.10.1/24)
     src 10.10.10.2 -> dst 20.0.0.2
  DUT: routes 20.0.0.0/24 -> bundle-10 (egress ACL 'egress-bfd' out) -> ge400-0/0/11
       (loopback) -> ge400-0/0/12 -> bundle-20 (VRF test)
"""
import paramiko, time, re, sys, json
from stcrestclient import stchttp

DUT_HOST = "WKY1C7VD00008P2"
DUT_USER = "dnroot"
DUT_PASS = "dnroot"

ING_IF   = "ge100-0/0/3/0"
ING_MAC  = "e8:c5:7a:d6:30:18"
SRC_IP   = "10.10.10.2"
SRC_MAC  = "00:10:94:00:00:25"
DST_IP   = "20.0.0.2"
VRF      = "test"

ACL_NAME    = "egress-bfd"
BUNDLE_EG   = "bundle-10"

LABSERVER  = "il-auto-containers"
CHASSIS_IP = "100.64.15.236"
SLOT, PORT = 1, 25
SESSION    = "sw244107_retest"

MEASURE_SECONDS = 15
LOAD_FPS        = 1_000_000
FRAME_BYTES     = 128

ANSI = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')
MORE = re.compile(r'-- More -- \(Press q to quit\)\s*')


def clean(t):
    return MORE.sub('', re.sub(r'\r', '', ANSI.sub('', t)))


def recv_all(shell, timeout=6):
    out = b""; end = time.time() + timeout
    while time.time() < end:
        time.sleep(0.3)
        while shell.recv_ready():
            out += shell.recv(65536); end = time.time() + 1.2
    return clean(out.decode(errors='replace'))


def run(shell, cmd, wait=3):
    shell.send(cmd + "\n")
    time.sleep(wait)
    return recv_all(shell, timeout=5)


def commit(shell, wait=12):
    """Commit and handle the out-of-sync prompt. On failure, rollback.

    Returns (success: bool, output: str).
    """
    shell.send("commit\n")
    time.sleep(wait)
    out = recv_all(shell, timeout=8)
    if 'out of sync' in out.lower():
        shell.send("commit\n")
        time.sleep(wait)
        out += recv_all(shell, timeout=8)
    if 'Commit succeeded' in out:
        return True, out
    # Failure: rollback pending changes so exit works cleanly
    shell.send("rollback\n")
    time.sleep(5)
    out += recv_all(shell, timeout=5)
    return False, out


def exit_config(shell):
    run(shell, "top", wait=1)
    shell.send("exit\n")
    time.sleep(2)
    out = recv_all(shell, timeout=4)
    if 'uncommitted' in out.lower():
        shell.send("no\n")
        time.sleep(2)
        out += recv_all(shell, timeout=4)
    return out


def dut_connect():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(DUT_HOST, username=DUT_USER, password=DUT_PASS,
                look_for_keys=False, allow_agent=False, timeout=20)
    shell = ssh.invoke_shell(width=250, height=5000)
    time.sleep(6)
    shell.recv(65535)
    run(shell, "set cli-no-confirm", wait=1)
    return ssh, shell


def get_acl_matches(shell):
    out = run(shell, f"show access-lists counters {BUNDLE_EG} | no-more", wait=5)
    for line in out.split('\n'):
        if ACL_NAME in line and ('deny' in line or 'allow' in line) and 'default' not in line:
            cells = [c.strip() for c in line.split('|')]
            try:
                return int(cells[-2])
            except Exception:
                pass
    return None


def get_if_counters(shell, ifname):
    out = run(shell, f"show interfaces counters {ifname} | no-more", wait=5)
    def find_int(pat):
        m = re.search(pat, out)
        if not m: return None
        return int(re.sub(r'[^\d]', '', m.group(1)))
    return {
        'rx_frames':   find_int(r'RX frames:\s+([\d,]+)'),
        'tx_frames':   find_int(r'TX frames:\s+([\d,]+)'),
        'rx_drops':    find_int(r'RX drops:\s+([\d,]+)'),
        'tx_drops':    find_int(r'TX drops:\s+([\d,]+)'),
        'route_unreach': find_int(r'Destination route unreachable:\s+([\d,]+)'),
    }


def measure(shell, label):
    print(f"\n{'='*72}\n  {label}\n{'='*72}", flush=True)
    acl1 = get_acl_matches(shell)
    bn1  = get_if_counters(shell, BUNDLE_EG)
    ig1  = get_if_counters(shell, ING_IF)
    print(f"  t=0    ACL matches={acl1} | {BUNDLE_EG} tx_frames={bn1['tx_frames']} tx_drops={bn1['tx_drops']}"
          f" | {ING_IF} rx_frames={ig1['rx_frames']}", flush=True)
    time.sleep(MEASURE_SECONDS)
    acl2 = get_acl_matches(shell)
    bn2  = get_if_counters(shell, BUNDLE_EG)
    ig2  = get_if_counters(shell, ING_IF)
    print(f"  t={MEASURE_SECONDS:>2}s   ACL matches={acl2} | {BUNDLE_EG} tx_frames={bn2['tx_frames']} tx_drops={bn2['tx_drops']}"
          f" | {ING_IF} rx_frames={ig2['rx_frames']}", flush=True)
    def d(a, b): return (b - a) if (a is not None and b is not None) else None
    res = {
        'd_acl_matches': d(acl1, acl2),
        'd_egress_tx':   d(bn1['tx_frames'], bn2['tx_frames']),
        'd_egress_tx_drops': d(bn1['tx_drops'], bn2['tx_drops']),
        'd_ingress_rx':  d(ig1['rx_frames'], ig2['rx_frames']),
        'd_ingress_route_unreach': d(ig1['route_unreach'], ig2['route_unreach']),
    }
    pps_acl = (res['d_acl_matches'] or 0) / MEASURE_SECONDS
    pps_rx  = (res['d_ingress_rx'] or 0) / MEASURE_SECONDS
    pps_tx  = (res['d_egress_tx'] or 0) / MEASURE_SECONDS
    print(f"  Δ ACL matches           : {res['d_acl_matches']}  ({pps_acl:,.0f} pps)")
    print(f"  Δ {BUNDLE_EG} tx_frames : {res['d_egress_tx']}  ({pps_tx:,.0f} pps)")
    print(f"  Δ {BUNDLE_EG} tx_drops  : {res['d_egress_tx_drops']}")
    print(f"  Δ {ING_IF} rx_frames  : {res['d_ingress_rx']}  ({pps_rx:,.0f} pps)")
    return res


def dut_setup_topology(shell):
    print("\n>> DUT setup: ge100-0/0/3/0 -> VRF test; ACL rule 1 deny")
    run(shell, "configure", wait=3)
    run(shell, f"network-services vrf instance {VRF} interface {ING_IF}", wait=2)
    run(shell, "top", wait=1)
    run(shell, f"no access-lists ipv4 {ACL_NAME} rule 1", wait=2)
    run(shell, f"access-lists ipv4 {ACL_NAME} rule 1 deny", wait=2)
    ok, out = commit(shell)
    print("   commit:", "OK" if ok else out)
    exit_config(shell)
    if not ok:
        raise RuntimeError("setup commit failed")


def dut_restore_topology(shell):
    print("\n>> DUT cleanup: ACL allow, remove ingress from VRF test")
    run(shell, "configure", wait=3)
    run(shell, f"no access-lists ipv4 {ACL_NAME} rule 1", wait=2)
    run(shell, f"access-lists ipv4 {ACL_NAME} rule 1 allow", wait=2)
    run(shell, f"no network-services vrf instance {VRF} interface {ING_IF}", wait=2)
    # also clean any leftover bfd/bgp from an aborted run
    run(shell, "no protocols bgp", wait=2)
    run(shell, f"no protocols bfd interface {BUNDLE_EG}", wait=2)
    ok, out = commit(shell)
    print("   commit:", "OK" if ok else out)
    exit_config(shell)


def dut_cfg_ubfd(shell):
    print("\n>> DUT: configure uBFD on bundle-10 (neighbor 20.0.0.2)")
    run(shell, "configure", wait=3)
    run(shell, f"protocols bfd interface {BUNDLE_EG}", wait=2)
    run(shell, "min-tx 300", wait=2)
    run(shell, "min-rx 300", wait=2)
    run(shell, "multiplier 3", wait=2)
    run(shell, f"neighbor {DST_IP}", wait=2)
    run(shell, "top", wait=1)
    ok, out = commit(shell)
    print("   commit:", "OK" if ok else out)
    exit_config(shell)


def dut_rm_ubfd(shell):
    print("\n>> DUT: remove uBFD on bundle-10")
    run(shell, "configure", wait=3)
    run(shell, f"no protocols bfd interface {BUNDLE_EG}", wait=2)
    ok, out = commit(shell)
    print("   commit:", "OK" if ok else out)
    exit_config(shell)


def dut_cfg_bgp_bfd(shell):
    print("\n>> DUT: configure BGP neighbor 20.0.0.2 with single-hop BFD (SH-BFD in BGP)")
    run(shell, "configure", wait=3)
    run(shell, "protocols bgp 65001", wait=2)
    run(shell, "router-id 10.100.1.1", wait=2)
    run(shell, f"neighbor {DST_IP}", wait=2)
    run(shell, "remote-as 65002", wait=2)
    run(shell, "admin-state enabled", wait=2)
    run(shell, "address-family ipv4-unicast", wait=2)
    run(shell, "top", wait=1)
    run(shell, f"protocols bgp 65001 neighbor {DST_IP} bfd", wait=2)
    run(shell, "admin-state enabled", wait=2)
    run(shell, "bfd-type single-hop", wait=2)
    run(shell, "min-tx 300", wait=2)
    run(shell, "min-rx 300", wait=2)
    run(shell, "multiplier 3", wait=2)
    run(shell, "top", wait=1)
    ok, out = commit(shell)
    print("   commit:", "OK" if ok else out)
    exit_config(shell)


def dut_rm_bgp_bfd(shell):
    print("\n>> DUT: remove BGP + uBFD")
    run(shell, "configure", wait=3)
    run(shell, "no protocols bgp", wait=2)
    run(shell, f"no protocols bfd interface {BUNDLE_EG}", wait=2)
    ok, out = commit(shell)
    print("   commit:", "OK" if ok else out)
    exit_config(shell)


def spirent_start(stc):
    for s in stc.sessions():
        if SESSION in s:
            try:
                stc.join_session(s); stc.end_session(s)
            except Exception:
                pass
    sid = stc.new_session('dn', SESSION)
    stc.join_session(sid)
    project = stc.get('system1', 'children-project')
    port = stc.create('port', under=project)
    stc.config(port, {'location': f'//{CHASSIS_IP}/{SLOT}/{PORT}'})
    stc.perform('AttachPorts', params={'RevokeOwner': 'true'})
    stc.apply()
    print(f"  Spirent {CHASSIS_IP}/{SLOT}/{PORT} online={stc.get(port, 'Online')}")
    sb = stc.create('streamBlock', under=port)
    stc.config(sb, {
        'Name': 'sw244107_retest', 'FixedFrameLength': str(FRAME_BYTES),
        'LoadUnit': 'FRAMES_PER_SECOND', 'Load': str(LOAD_FPS),
        'InsertSig': 'FALSE',
    })
    eth = stc.get(sb, 'children-ethernet:EthernetII').split()[0]
    stc.config(eth, {'srcMac': SRC_MAC, 'dstMac': ING_MAC})
    ipv4 = stc.get(sb, 'children-ipv4:IPv4').split()[0]
    stc.config(ipv4, {'sourceAddr': SRC_IP, 'destAddr': DST_IP, 'ttl': '64'})
    stc.apply()
    gen = stc.get(port, 'children-generator')
    gen_cfg = stc.get(gen, 'children-generatorconfig')
    stc.config(gen_cfg, {
        'SchedulingMode': 'PORT_BASED', 'DurationMode': 'CONTINUOUS',
        'LoadUnit': 'FRAMES_PER_SECOND', 'FixedLoad': str(LOAD_FPS),
    })
    stc.apply()
    stc.perform('GeneratorStart', params={'GeneratorList': gen})
    print(f"  Spirent generator started: {LOAD_FPS:,} fps @ {FRAME_BYTES}B")
    return sid, port, gen


def spirent_stop(stc, sid, gen):
    try:
        stc.perform('GeneratorStop', params={'GeneratorList': gen})
    except Exception as e:
        print(f"  GeneratorStop failed: {e}")
    time.sleep(2)
    try: stc.end_session(sid)
    except Exception as e: print(f"  end_session failed: {e}")


def main():
    print("SW-244107 re-run — DENY ACL, BGP-BFD in Phase C")
    print(f"DUT: {DUT_HOST}  ingress: {ING_IF} (VRF {VRF})  egress: {BUNDLE_EG}")
    print(f"Spirent: //{CHASSIS_IP}/{SLOT}/{PORT}   {LOAD_FPS:,} fps @ {FRAME_BYTES}B\n")

    ssh, shell = dut_connect()
    stc = stchttp.StcHttp(LABSERVER, port=80)
    sid = gen = None
    results = {}
    try:
        print(run(shell, "show system | no-more", wait=5))
        dut_setup_topology(shell)
        print(run(shell, f"show config interfaces {ING_IF} | no-more", wait=3))
        print(run(shell, "show config access-lists | no-more", wait=3))
        print(run(shell, "show config network-services | no-more", wait=3))
        print(run(shell, f"show route vrf {VRF} {DST_IP} | no-more", wait=3))

        sid, port, gen = spirent_start(stc)
        time.sleep(8)

        # Clear counters so the per-phase deltas are clean
        run(shell, "clear counters interfaces", wait=4)
        run(shell, "clear access-lists counters", wait=4)
        time.sleep(4)

        results['A'] = measure(shell, "Phase A: baseline (ACL=deny, no BFD)")

        dut_cfg_ubfd(shell)
        time.sleep(5)
        print(run(shell, "show bfd sessions | no-more", wait=3))
        results['B'] = measure(shell, "Phase B: ACL=deny + uBFD on bundle-10")
        dut_rm_ubfd(shell)
        time.sleep(3)

        dut_cfg_bgp_bfd(shell)
        time.sleep(5)
        print(run(shell, "show config protocols | no-more", wait=3))
        print(run(shell, "show bfd sessions | no-more", wait=3))
        print(run(shell, "show bgp neighbors | no-more", wait=3))
        results['C'] = measure(shell, "Phase C: ACL=deny + BGP BFD (SH-BFD in BGP)")
        dut_rm_bgp_bfd(shell)
        time.sleep(3)

        results['D'] = measure(shell, "Phase D: post-cleanup (ACL=deny, no BFD)")

    finally:
        if sid and gen:
            spirent_stop(stc, sid, gen)
        dut_restore_topology(shell)
        print(run(shell, "show config access-lists | no-more", wait=3))
        print(run(shell, f"show config interfaces {ING_IF} | no-more", wait=3))
        print(run(shell, "show config protocols | no-more", wait=3))
        ssh.close()

    print(f"\n{'='*72}\n  SW-244107 retest SUMMARY\n{'='*72}")
    labels = {
        'A': 'baseline (ACL=deny, no BFD)',
        'B': 'ACL=deny + uBFD',
        'C': 'ACL=deny + BGP BFD',
        'D': 'post-cleanup (ACL=deny, no BFD)',
    }
    for p in ('A', 'B', 'C', 'D'):
        r = results.get(p) or {}
        da = r.get('d_acl_matches')
        pps = (da or 0) / MEASURE_SECONDS
        inc = 'INCREMENTING' if (da or 0) > 0 else 'NOT incrementing'
        print(f"  Phase {p} {labels[p]}:  ACL Δ={da}  ({pps:,.0f} pps)  -> {inc}")

    with open('/home/dn/sw244107_retest_results.json', 'w') as f:
        json.dump(results, f, indent=2)
    print("\nResults saved to /home/dn/sw244107_retest_results.json")


if __name__ == "__main__":
    main()
