#!/usr/bin/env python3
"""SW-244103: IPv4 strict mode on customer sub-interface with different routing conclusions.

Executes test steps 1-4, 10-12 (static/connected/aggregate/null0/no-route scenarios)
using Spirent port 1/25 → ge400-0/0/3.100 on NCP3-nog (WKY1C7VD00008P2).

Steps 5-9 (BGP/OSPF/IS-IS) require protocol emulation and are deferred.
"""

import paramiko, time, re, json, sys
from datetime import datetime
from stcrestclient import stchttp

# ── Constants ──────────────────────────────────────────────────────────
LABSERVER    = 'il-auto-containers'
CHASSIS_IP   = '100.64.15.236'
SLOT, PORT   = 1, 25
DUT_IP       = '100.64.8.59'
DUT_MAC      = 'e8:c5:7a:d6:30:18'
SRC_MAC      = '00:10:94:01:19:01'
SUB_IF       = 'ge400-0/0/3.100'
VLAN_ID      = '100'
DEST_IP      = '20.0.0.2'  # routable via bundle-10 (different egress)
TRAFFIC_SEC  = 12
TRAFFIC_FPS  = 1000
SESSION_NAME = 'sw244103_urpf'

# ── Helpers ────────────────────────────────────────────────────────────
def clean(text):
    text = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)
    text = re.sub(r'\r', '', text)
    text = re.sub(r'-- More -- \(Press q to quit\)\s*', '', text)
    return text.strip()

def dut_connect():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(DUT_IP, username='dnroot', password='dnroot', timeout=30,
                look_for_keys=False, allow_agent=False)
    chan = ssh.invoke_shell(width=300, height=5000)
    time.sleep(6)
    chan.recv(65535)
    return ssh, chan

def dut_run(chan, cmd, wait=10):
    chan.send(cmd + '\n')
    time.sleep(wait)
    out = b''
    while chan.recv_ready():
        out += chan.recv(65535)
        time.sleep(0.5)
    return clean(out.decode(errors='replace'))

def rp(chan, cmd, wait=8):
    output = dut_run(chan, cmd, wait)
    print(f"  [{cmd}]")
    for line in output.split('\n'):
        print(f"    {line}")
    return output

def extract_counter(text, label):
    """Extract integer counter value from 'show interfaces counters' output."""
    for line in text.split('\n'):
        if label in line:
            parts = line.split(':')
            if len(parts) >= 2:
                val = parts[-1].strip().split('(')[0].strip().replace(',', '')
                try:
                    return int(val)
                except ValueError:
                    return 0
    return 0

def get_urpf_counters(chan):
    """Return (rx_packets, urpf_v4_drops) from interface counters."""
    out = dut_run(chan, f"show interfaces counters {SUB_IF} | no-more", 10)
    rx = extract_counter(out, "RX packets:")
    drops = extract_counter(out, "uRPF Ipv4 drops:")
    return rx, drops, out

def dut_config(chan, commands, commit=True):
    """Enter config mode, run commands, commit, exit."""
    rp(chan, "configure", 5)
    for cmd in commands:
        rp(chan, cmd, 5)
        rp(chan, "top", 3)
    if commit:
        out = rp(chan, "commit", 15)
        if "ERROR" in out or "error" in out.lower():
            print(f"  *** COMMIT ERROR ***")
            return False
    rp(chan, "end", 3)
    return True

# ── Spirent helpers ────────────────────────────────────────────────────
def spirent_connect():
    stc = stchttp.StcHttp(LABSERVER, port=80)
    for s in stc.sessions():
        if SESSION_NAME in s:
            try:
                stc.join_session(s)
                stc.end_session(s)
            except Exception:
                pass

    sid = stc.new_session('dn', SESSION_NAME)
    stc.join_session(sid)
    project = stc.get('system1', 'children-project')
    port1 = stc.create('port', under=project)
    stc.config(port1, {'location': f'//{CHASSIS_IP}/{SLOT}/{PORT}'})
    print("  Attaching Spirent port (RevokeOwner=true)...")
    stc.perform('AttachPorts', params={'RevokeOwner': 'true'})
    stc.apply()
    online = stc.get(port1, 'Online')
    print(f"  Port online: {online}")
    if online != 'true':
        print("  WARNING: Port not online!")
    return stc, sid, project, port1

def create_stream(stc, port1, name, src_ip):
    """Create a VLAN-100-tagged IPv4 stream with given source IP."""
    sbs = stc.get(port1, 'children-streamblock')
    if sbs:
        for sb in sbs.split():
            stc.delete(sb)
        stc.apply()

    sb = stc.create('streamBlock', under=port1)
    stc.config(sb, {
        'Name': name,
        'FixedFrameLength': '128',
        'LoadUnit': 'FRAMES_PER_SECOND',
        'Load': str(TRAFFIC_FPS),
    })
    stc.apply()

    eth = stc.get(sb, 'children-ethernet:EthernetII').split()[0]
    stc.config(eth, {'srcMac': SRC_MAC, 'dstMac': DUT_MAC})
    vlans_c = stc.get(eth, 'children-vlans').split()[0]
    vlan = stc.create('Vlan', under=vlans_c)
    stc.config(vlan, {'id': VLAN_ID})

    ipv4 = stc.get(sb, 'children-ipv4:IPv4').split()[0]
    stc.config(ipv4, {'sourceAddr': src_ip, 'destAddr': DEST_IP, 'ttl': '64'})
    stc.apply()
    return sb

def send_traffic(stc, port1, duration=TRAFFIC_SEC):
    gen = stc.get(port1, 'children-generator')
    gen_cfg = stc.get(gen, 'children-generatorconfig')
    stc.config(gen_cfg, {
        'SchedulingMode': 'PORT_BASED',
        'DurationMode': 'CONTINUOUS',
        'LoadUnit': 'FRAMES_PER_SECOND',
        'FixedLoad': str(TRAFFIC_FPS),
    })
    stc.apply()
    print(f"  Sending traffic at {TRAFFIC_FPS} fps for {duration}s...")
    stc.perform('GeneratorStart', params={'GeneratorList': gen})
    time.sleep(duration)
    stc.perform('GeneratorStop', params={'GeneratorList': gen})
    time.sleep(3)
    print("  Traffic stopped.")

# ── Test execution ─────────────────────────────────────────────────────
def main():
    results = {}
    ts = datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')

    # ── Connect ──
    print("=" * 70)
    print("Connecting to DUT...")
    ssh, chan = dut_connect()
    sw_ver = dut_run(chan, "show system version | no-more", 8)
    print(f"  DUT: {DUT_IP}")
    for line in sw_ver.split('\n'):
        if 'Version:' in line or 'System Name' in line:
            print(f"  {line.strip()}")

    print("\nConnecting to Spirent...")
    stc, sid, project, port1 = spirent_connect()

    # ══════════════════════════════════════════════════════════════════
    # STEP 1: Verify uRPF strict config
    # ══════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("STEP 1: Verify uRPF strict config on ge400-0/0/3.100")
    print("=" * 70)

    cfg_out = rp(chan, f"show config interfaces {SUB_IF} urpf | no-more", 10)
    detail_out = rp(chan, f"show interfaces detail {SUB_IF} | no-more", 12)

    step1_pass = True
    checks = {
        'mode strict': False,
        'admin-state enabled': False,
        'uRPF IPv4 check: enabled': False,
    }
    for key in checks:
        if key.lower() in cfg_out.lower() or key.lower() in detail_out.lower():
            checks[key] = True

    for check, found in checks.items():
        status = "OK" if found else "MISSING"
        print(f"  [{status}] {check}")
        if not found:
            step1_pass = False

    results['Step 1'] = {
        'name': 'Verify uRPF strict config',
        'result': 'PASS' if step1_pass else 'FAIL',
        'output': cfg_out + '\n' + detail_out,
        'analysis': 'uRPF strict mode confirmed active on sub-interface' if step1_pass
                    else 'Missing expected uRPF configuration attributes',
    }
    print(f"\n  >>> STEP 1: {'PASS' if step1_pass else 'FAIL'}")

    # ══════════════════════════════════════════════════════════════════
    # STEP 2: Static route — pass
    # Add 10.10.10.0/24 via 10.100.1.2 (resolves via ge400-0/0/3.100)
    # Source 10.10.10.1 → should be forwarded, no uRPF drops
    # ══════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("STEP 2: Static route — pass (10.10.10.0/24 via ingress interface)")
    print("=" * 70)

    dut_config(chan, [
        "protocols static address-family ipv4-unicast route 10.10.10.0/24 next-hop 10.100.1.2",
    ])
    time.sleep(3)
    rp(chan, "show route vrf default table ipv4-unicast 10.10.10.0/24 | no-more", 10)

    rx_before, drops_before, _ = get_urpf_counters(chan)
    create_stream(stc, port1, 'Step2_static_pass', '10.10.10.1')
    send_traffic(stc, port1)
    rx_after, drops_after, counters_out = get_urpf_counters(chan)

    rx_delta = rx_after - rx_before
    drop_delta = drops_after - drops_before
    step2_pass = drop_delta == 0 and rx_delta > 0

    print(f"  RX Δ: {rx_delta:,}  |  uRPF drops Δ: {drop_delta:,}")
    print(f"  >>> STEP 2: {'PASS' if step2_pass else 'FAIL'}")

    results['Step 2'] = {
        'name': 'Static route — pass',
        'result': 'PASS' if step2_pass else 'FAIL',
        'command': 'protocols static address-family ipv4-unicast route 10.10.10.0/24 next-hop 10.100.1.2',
        'src_ip': '10.10.10.1',
        'rx_delta': rx_delta, 'drop_delta': drop_delta,
        'output': counters_out,
        'analysis': f'RX Δ={rx_delta:,}, uRPF drops Δ={drop_delta:,}. '
                    + ('Traffic forwarded, no drops — reverse path resolves via ingress.' if step2_pass
                       else 'Unexpected result.'),
    }

    # ══════════════════════════════════════════════════════════════════
    # STEP 3: Static route — drop (different egress)
    # Add 192.0.2.0/24 via 20.0.0.2 (resolves via bundle-10, NOT ingress)
    # Source 192.0.2.1 into ge400-0/0/3.100 → should be dropped
    # ══════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("STEP 3: Static route — drop (192.0.2.0/24 via different egress)")
    print("=" * 70)

    dut_config(chan, [
        "protocols static address-family ipv4-unicast route 192.0.2.0/24 next-hop 20.0.0.2",
    ])
    time.sleep(3)
    rp(chan, "show route vrf default table ipv4-unicast 192.0.2.0/24 | no-more", 10)

    rx_before, drops_before, _ = get_urpf_counters(chan)
    create_stream(stc, port1, 'Step3_static_drop', '192.0.2.1')
    send_traffic(stc, port1)
    rx_after, drops_after, counters_out = get_urpf_counters(chan)

    rx_delta = rx_after - rx_before
    drop_delta = drops_after - drops_before
    step3_pass = drop_delta > 0

    print(f"  RX Δ: {rx_delta:,}  |  uRPF drops Δ: {drop_delta:,}")
    print(f"  >>> STEP 3: {'PASS' if step3_pass else 'FAIL'}")

    results['Step 3'] = {
        'name': 'Static route — drop (different egress)',
        'result': 'PASS' if step3_pass else 'FAIL',
        'command': 'protocols static address-family ipv4-unicast route 192.0.2.0/24 next-hop 20.0.0.2',
        'src_ip': '192.0.2.1',
        'rx_delta': rx_delta, 'drop_delta': drop_delta,
        'output': counters_out,
        'analysis': f'RX Δ={rx_delta:,}, uRPF drops Δ={drop_delta:,}. '
                    + ('Reverse path resolves via bundle-10, not ingress — correctly dropped.' if step3_pass
                       else 'Expected uRPF drops but counter did not increment.'),
    }

    # ══════════════════════════════════════════════════════════════════
    # STEP 4: Connected route — pass
    # Source 10.100.1.2 (directly connected peer on ge400-0/0/3.100)
    # ══════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("STEP 4: Connected route — pass (source 10.100.1.2, directly connected)")
    print("=" * 70)

    rx_before, drops_before, _ = get_urpf_counters(chan)
    create_stream(stc, port1, 'Step4_connected_pass', '10.100.1.2')
    send_traffic(stc, port1)
    rx_after, drops_after, counters_out = get_urpf_counters(chan)

    rx_delta = rx_after - rx_before
    drop_delta = drops_after - drops_before
    step4_pass = drop_delta == 0 and rx_delta > 0

    print(f"  RX Δ: {rx_delta:,}  |  uRPF drops Δ: {drop_delta:,}")
    print(f"  >>> STEP 4: {'PASS' if step4_pass else 'FAIL'}")

    results['Step 4'] = {
        'name': 'Connected route — pass',
        'result': 'PASS' if step4_pass else 'FAIL',
        'src_ip': '10.100.1.2',
        'rx_delta': rx_delta, 'drop_delta': drop_delta,
        'output': counters_out,
        'analysis': f'RX Δ={rx_delta:,}, uRPF drops Δ={drop_delta:,}. '
                    + ('Connected prefix resolves via ingress — forwarded correctly.' if step4_pass
                       else 'Unexpected drops on directly connected source.'),
    }

    # ══════════════════════════════════════════════════════════════════
    # STEP 10: Aggregate route — pass
    # Add contributing static 10.40.1.0/24 via 10.100.1.2,
    # then aggregate 10.40.0.0/16. Source 10.40.1.1 should pass.
    # ══════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("STEP 10: Aggregate route — pass (10.40.0.0/16 aggregate)")
    print("=" * 70)

    dut_config(chan, [
        "protocols static address-family ipv4-unicast route 10.40.1.0/24 next-hop 10.100.1.2",
        "protocols aggregate address-family ipv4-unicast route 10.40.0.0/16",
    ])
    time.sleep(5)
    rp(chan, "show route vrf default table ipv4-unicast 10.40.1.0/24 | no-more", 10)
    rp(chan, "show route vrf default table ipv4-unicast 10.40.0.0/16 | no-more", 10)

    rx_before, drops_before, _ = get_urpf_counters(chan)
    create_stream(stc, port1, 'Step10_aggregate_pass', '10.40.1.1')
    send_traffic(stc, port1)
    rx_after, drops_after, counters_out = get_urpf_counters(chan)

    rx_delta = rx_after - rx_before
    drop_delta = drops_after - drops_before
    step10_pass = drop_delta == 0 and rx_delta > 0

    print(f"  RX Δ: {rx_delta:,}  |  uRPF drops Δ: {drop_delta:,}")
    print(f"  >>> STEP 10: {'PASS' if step10_pass else 'FAIL'}")

    results['Step 10'] = {
        'name': 'Aggregate route — pass',
        'result': 'PASS' if step10_pass else 'FAIL',
        'command': 'aggregate 10.40.0.0/16 + contributing static 10.40.1.0/24 via 10.100.1.2',
        'src_ip': '10.40.1.1',
        'rx_delta': rx_delta, 'drop_delta': drop_delta,
        'output': counters_out,
        'analysis': f'RX Δ={rx_delta:,}, uRPF drops Δ={drop_delta:,}. '
                    + ('Aggregate route with contributing static via ingress — forwarded correctly.' if step10_pass
                       else 'Unexpected result for aggregate route.'),
    }

    # ══════════════════════════════════════════════════════════════════
    # STEP 11: Null0 discard route — drop
    # Install 198.51.101.0/24 → discard. Source 198.51.101.1 should drop.
    # (Using .101 to avoid touching existing .100 route)
    # ══════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("STEP 11: Null0 discard route — drop (198.51.101.0/24)")
    print("=" * 70)

    dut_config(chan, [
        "protocols static address-family ipv4-unicast route 198.51.101.0/24 discard",
    ])
    time.sleep(3)
    rp(chan, "show route vrf default table ipv4-unicast 198.51.101.0/24 | no-more", 10)

    rx_before, drops_before, _ = get_urpf_counters(chan)
    create_stream(stc, port1, 'Step11_null0_drop', '198.51.101.1')
    send_traffic(stc, port1)
    rx_after, drops_after, counters_out = get_urpf_counters(chan)

    rx_delta = rx_after - rx_before
    drop_delta = drops_after - drops_before
    step11_pass = drop_delta > 0

    print(f"  RX Δ: {rx_delta:,}  |  uRPF drops Δ: {drop_delta:,}")
    print(f"  >>> STEP 11: {'PASS' if step11_pass else 'FAIL'}")

    results['Step 11'] = {
        'name': 'Null0 discard route — drop',
        'result': 'PASS' if step11_pass else 'FAIL',
        'command': 'protocols static address-family ipv4-unicast route 198.51.101.0/24 discard',
        'src_ip': '198.51.101.1',
        'rx_delta': rx_delta, 'drop_delta': drop_delta,
        'output': counters_out,
        'analysis': f'RX Δ={rx_delta:,}, uRPF drops Δ={drop_delta:,}. '
                    + ('Null0/discard route — uRPF correctly drops traffic.' if step11_pass
                       else 'Expected uRPF drops but counter did not increment.'),
    }

    # ══════════════════════════════════════════════════════════════════
    # STEP 12: No route — drop
    # Source 203.0.114.1 has no FIB entry → should be dropped
    # ══════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("STEP 12: No route — drop (source 203.0.114.1, no FIB entry)")
    print("=" * 70)

    rp(chan, "show route vrf default table ipv4-unicast 203.0.114.0/24 | no-more", 10)

    rx_before, drops_before, _ = get_urpf_counters(chan)
    create_stream(stc, port1, 'Step12_noroute_drop', '203.0.114.1')
    send_traffic(stc, port1)
    rx_after, drops_after, counters_out = get_urpf_counters(chan)

    rx_delta = rx_after - rx_before
    drop_delta = drops_after - drops_before
    step12_pass = drop_delta > 0

    print(f"  RX Δ: {rx_delta:,}  |  uRPF drops Δ: {drop_delta:,}")
    print(f"  >>> STEP 12: {'PASS' if step12_pass else 'FAIL'}")

    results['Step 12'] = {
        'name': 'No route — drop',
        'result': 'PASS' if step12_pass else 'FAIL',
        'src_ip': '203.0.114.1',
        'rx_delta': rx_delta, 'drop_delta': drop_delta,
        'output': counters_out,
        'analysis': f'RX Δ={rx_delta:,}, uRPF drops Δ={drop_delta:,}. '
                    + ('No FIB entry for source — uRPF correctly drops traffic.' if step12_pass
                       else 'Expected uRPF drops but counter did not increment.'),
    }

    # ══════════════════════════════════════════════════════════════════
    # STEP 13: Cleanup
    # ══════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("STEP 13: Cleanup test configurations")
    print("=" * 70)

    cleanup_cmds = [
        "no protocols static address-family ipv4-unicast route 10.10.10.0/24",
        "no protocols static address-family ipv4-unicast route 192.0.2.0/24",
        "no protocols static address-family ipv4-unicast route 10.40.1.0/24",
        "no protocols aggregate address-family ipv4-unicast route 10.40.0.0/16",
        "no protocols static address-family ipv4-unicast route 198.51.101.0/24",
    ]
    cleanup_ok = dut_config(chan, cleanup_cmds)
    time.sleep(3)
    rp(chan, "show route vrf default table ipv4-unicast | no-more", 10)

    results['Step 13'] = {
        'name': 'Cleanup',
        'result': 'PASS' if cleanup_ok else 'FAIL',
        'analysis': 'Test routes removed, config restored.' if cleanup_ok
                    else 'Cleanup had errors.',
    }
    print(f"  >>> STEP 13: {'PASS' if cleanup_ok else 'FAIL'}")

    # ── Teardown ──
    print("\n" + "=" * 70)
    print("Tearing down Spirent session and SSH...")
    stc.end_session(sid)
    chan.send('exit\n')
    time.sleep(2)
    ssh.close()

    # ══════════════════════════════════════════════════════════════════
    # SUMMARY
    # ══════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("TEST SUMMARY — SW-244103")
    print("=" * 70)
    print(f"  Date:    {ts}")
    print(f"  Device:  NCP3-nog (WKY1C7VD00008P2) — {DUT_IP}")
    print(f"  Version: DNOS 26.2.0 build 32_priv")
    print(f"  Spirent: {CHASSIS_IP} slot {SLOT} port {PORT}")
    print()

    all_pass = True
    for step, info in results.items():
        r = info['result']
        tag = "(/) PASS" if r == 'PASS' else "(x) FAIL"
        print(f"  {step}: {info['name']:45s}  {tag}")
        if r != 'PASS':
            all_pass = False

    overall = 'PASS' if all_pass else 'PARTIAL'
    print(f"\n  Overall: {overall}")
    print(f"\n  Note: Steps 5-9 (BGP/OSPF/IS-IS) require protocol emulation — deferred.")

    out_file = '/home/dn/output/sw244103_results.json'
    with open(out_file, 'w') as f:
        json.dump({'timestamp': ts, 'overall': overall, 'steps': results}, f, indent=2, default=str)
    print(f"\n  Results saved to {out_file}")

if __name__ == '__main__':
    main()
