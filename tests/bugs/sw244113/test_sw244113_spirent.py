#!/usr/bin/env python3
"""SW-244113: Spirent traffic generation for uRPF VRF testing.
Chassis slot 1 port 25 → connected to NCP3-nog ge400-0/0/3.
"""

import time
import paramiko
import re
import json
from stcrestclient import stchttp

LABSERVER = 'il-auto-containers'
CHASSIS_IP = '100.64.15.236'
SLOT = 1
PORT = 25
DUT_IP = '100.64.8.59'
DUT_MAC = 'e8:c5:7a:d6:30:18'
SRC_MAC = '00:10:94:01:19:01'
SESSION_NAME = 'sw244113_urpf_vrf'

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
    time.sleep(6); chan.recv(65535)
    return ssh, chan

def dut_run(chan, cmd, wait=10):
    chan.send(cmd + '\n'); time.sleep(wait)
    out = b''
    while chan.recv_ready(): out += chan.recv(65535); time.sleep(0.5)
    return clean(out.decode(errors='replace'))

def dut_extract(text, label):
    for line in text.split('\n'):
        if label in line:
            val = line.split(':')[-1].strip().split('(')[0].strip()
            return int(val) if val.isdigit() else 0
    return 0

def spirent_connect():
    print(f"Connecting to Spirent Lab Server: {LABSERVER}")
    stc = stchttp.StcHttp(LABSERVER, port=80)

    for s in stc.sessions():
        if SESSION_NAME in s:
            print(f"Ending old session: {s}")
            try:
                stc.join_session(s)
                stc.end_session(s)
            except Exception as e:
                print(f"  Warning: {e}")

    print(f"Creating new session: {SESSION_NAME}")
    sid = stc.new_session('dn', SESSION_NAME)
    stc.join_session(sid)

    project = stc.get('system1', 'children-project')
    port1 = stc.create('port', under=project)
    stc.config(port1, {'location': f'//{CHASSIS_IP}/{SLOT}/{PORT}'})

    print(f"Reserving port //{CHASSIS_IP}/{SLOT}/{PORT}...")
    stc.perform('AttachPorts', params={'RevokeOwner': 'true'})
    stc.apply()
    print(f"Port online: {stc.get(port1, 'Online')}")

    return stc, sid, project, port1


def create_ipv4_stream(stc, port, name, src_ip, dst_ip, vlan_id, rate_fps=1000):
    """Create IPv4 stream with VLAN using STC object model."""
    sb = stc.create('streamBlock', under=port)
    stc.config(sb, {
        'Name': name,
        'FixedFrameLength': '128',
        'LoadUnit': 'FRAMES_PER_SECOND',
        'Load': str(rate_fps),
    })
    stc.apply()

    # Get the default EthernetII header
    eth = stc.get(sb, 'children-ethernet:EthernetII').split()[0]
    stc.config(eth, {'srcMac': SRC_MAC, 'dstMac': DUT_MAC})

    # Add VLAN: get existing vlans container, create Vlan under it
    vlans_container = stc.get(eth, 'children-vlans').split()[0]
    vlan = stc.create('Vlan', under=vlans_container)
    stc.config(vlan, {'id': str(vlan_id)})

    # Get the default IPv4 header and configure it
    ipv4 = stc.get(sb, 'children-ipv4:IPv4').split()[0]
    stc.config(ipv4, {'sourceAddr': src_ip, 'destAddr': dst_ip, 'ttl': '64'})

    stc.apply()
    return sb


def clear_streams(stc, port):
    sbs = stc.get(port, 'children-streamblock')
    if sbs:
        for sb in sbs.split():
            stc.delete(sb)
    stc.apply()


def run_traffic_and_check(stc, port, dut_chan, duration_s=15, label=""):
    gen = stc.get(port, 'children-generator')
    gen_cfg = stc.get(gen, 'children-generatorconfig')
    stc.config(gen_cfg, {
        'SchedulingMode': 'PORT_BASED',
        'DurationMode': 'CONTINUOUS',
        'LoadUnit': 'FRAMES_PER_SECOND',
        'FixedLoad': '1000',
    })
    stc.apply()

    c_before = dut_run(dut_chan, "show interfaces counters ge400-0/0/3.100 | no-more", 10)
    v4_before = dut_extract(c_before, "uRPF Ipv4 drops:")
    v6_before = dut_extract(c_before, "uRPF Ipv6 drops:")
    rx_before = dut_extract(c_before, "RX packets:")

    print(f"  Starting generator ({label})...")
    stc.perform('GeneratorStart', params={'GeneratorList': gen})
    time.sleep(duration_s)
    stc.perform('GeneratorStop', params={'GeneratorList': gen})
    print(f"  Generator stopped after {duration_s}s.")
    time.sleep(3)

    c_after = dut_run(dut_chan, "show interfaces counters ge400-0/0/3.100 | no-more", 10)
    v4_after = dut_extract(c_after, "uRPF Ipv4 drops:")
    v6_after = dut_extract(c_after, "uRPF Ipv6 drops:")
    rx_after = dut_extract(c_after, "RX packets:")

    result = {
        "rx_delta": rx_after - rx_before,
        "v4_drops_delta": v4_after - v4_before,
        "v6_drops_delta": v6_after - v6_before,
        "v4_drops_total": v4_after,
        "v6_drops_total": v6_after,
        "counters_before": c_before,
        "counters_after": c_after,
    }

    print(f"  DUT RX delta:         {result['rx_delta']:,}")
    print(f"  uRPF IPv4 drops Δ:    {result['v4_drops_delta']:,}")
    print(f"  uRPF IPv6 drops Δ:    {result['v6_drops_delta']:,}")

    return result


def main():
    stc, sid, project, port1 = spirent_connect()
    print("\nConnecting to DUT...")
    ssh, chan = dut_connect()

    RESULTS = {}

    # =====================================================================
    # STEP 11: allow-default enabled — src 10.100.10.100 matches only default route
    # =====================================================================
    print("\n" + "="*70)
    print("STEP 11: allow-default ENABLED — src matching only default route")
    print("  Src 10.100.10.100 → only 0.0.0.0/0 via ge400-0/0/3.100 → should PASS")
    print("="*70)

    clear_streams(stc, port1)
    create_ipv4_stream(stc, port1,
                       name='Step11_allow_default',
                       src_ip='10.100.10.100', dst_ip='10.100.2.100',
                       vlan_id=100, rate_fps=1000)

    r11 = run_traffic_and_check(stc, port1, chan, duration_s=15, label="Step 11 allow-default=enabled")

    if r11['v4_drops_delta'] == 0 and r11['rx_delta'] > 0:
        r11['result'] = 'PASS'
        print("  >>> STEP 11: PASS — allow-default enabled, zero uRPF drops")
    else:
        r11['result'] = 'FAIL'
        print(f"  >>> STEP 11: FAIL — uRPF drops: {r11['v4_drops_delta']}")
    RESULTS['step11'] = r11

    # =====================================================================
    # STEP 12: Disable allow-default — same traffic should be DROPPED
    # =====================================================================
    print("\n" + "="*70)
    print("STEP 12: Disable allow-default — same traffic should be DROPPED")
    print("="*70)

    print("  Disabling allow-default on all uRPF interfaces...")
    dut_run(chan, "configure", 5)
    dut_run(chan, "interfaces ge400-0/0/3.100 urpf allow-default disabled", 5)
    dut_run(chan, "top", 3)
    dut_run(chan, "interfaces bundle-10.100 urpf allow-default disabled", 5)
    dut_run(chan, "top", 3)
    dut_run(chan, "interfaces ge400-0/0/5 urpf allow-default disabled", 5)
    dut_run(chan, "top", 3)
    commit_out = dut_run(chan, "commit", 15)
    commit_ok = 'succeeded' in commit_out.lower()
    print(f"  Commit: {'OK' if commit_ok else 'ISSUE: ' + commit_out[-200:]}")
    dut_run(chan, "end", 3)

    detail = dut_run(chan, "show interfaces detail ge400-0/0/3.100 | no-more", 12)
    print(f"  allow-default: {'disabled' if 'Allow-default: disabled' in detail else 'CHECK'}")

    r12 = run_traffic_and_check(stc, port1, chan, duration_s=15, label="Step 12 allow-default=disabled")

    if r12['v4_drops_delta'] > 0:
        r12['result'] = 'PASS'
        print("  >>> STEP 12: PASS — allow-default disabled, traffic DROPPED by uRPF")
    else:
        r12['result'] = 'FAIL'
        print("  >>> STEP 12: FAIL — expected uRPF drops but got none")
    RESULTS['step12'] = r12

    # =====================================================================
    # STEP 13: VRF isolation — src valid in default VRF but not in urpf-vrf
    # =====================================================================
    print("\n" + "="*70)
    print("STEP 13: VRF isolation — src route exists in default VRF only")
    print("  Src 10.33.0.100 (default VRF has 10.33.0.0/24 on ge400-0/0/33)")
    print("  urpf-vrf has no route for this → should be DROPPED")
    print("="*70)

    # Restore per-AFI to strict for clean step 13
    print("  Setting per-AFI both to strict...")
    dut_run(chan, "configure", 5)
    dut_run(chan, "interfaces ge400-0/0/3.100 urpf address-family ipv4 mode strict", 5)
    dut_run(chan, "top", 3)
    dut_run(chan, "interfaces ge400-0/0/3.100 urpf address-family ipv6 mode strict", 5)
    dut_run(chan, "top", 3)
    commit_out = dut_run(chan, "commit", 15)
    print(f"  Commit: {'OK' if 'succeeded' in commit_out.lower() else 'ISSUE'}")
    dut_run(chan, "end", 3)

    clear_streams(stc, port1)
    create_ipv4_stream(stc, port1,
                       name='Step13_vrf_isolation',
                       src_ip='10.33.0.100', dst_ip='10.100.2.100',
                       vlan_id=100, rate_fps=1000)

    r13 = run_traffic_and_check(stc, port1, chan, duration_s=15, label="Step 13 VRF isolation")

    if r13['v4_drops_delta'] > 0:
        r13['result'] = 'PASS'
        print("  >>> STEP 13: PASS — VRF isolation holds, uRPF drops the traffic")
    else:
        r13['result'] = 'FAIL'
        print("  >>> STEP 13: FAIL — expected uRPF drops for VRF-isolated source")
    RESULTS['step13'] = r13

    # Cleanup
    print("\nCleaning up...")
    clear_streams(stc, port1)

    chan.close(); ssh.close()

    with open("/home/dn/output/sw244113_steps11_13.json", "w") as f:
        json.dump(RESULTS, f, indent=2, default=str)

    print("\n" + "="*70)
    print("SUMMARY — Steps 11-13")
    print("="*70)
    for step in ['step11', 'step12', 'step13']:
        print(f"  {step}: {RESULTS[step]['result']}")

    print("\nEnding Spirent session...")
    stc.end_session(sid)
    print("Done.")

if __name__ == "__main__":
    main()
