#!/usr/bin/env python3
"""SW-244113: Steps 12-13 using Spirent."""

import paramiko, time, re, json
from stcrestclient import stchttp

LABSERVER = 'il-auto-containers'
CHASSIS_IP = '100.64.15.236'
DUT_IP = '100.64.8.59'
DUT_MAC = 'e8:c5:7a:d6:30:18'
SRC_MAC = '00:10:94:01:19:01'

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

def setup_spirent():
    stc = stchttp.StcHttp(LABSERVER, port=80)
    for s in stc.sessions():
        if 'sw244113' in s:
            try: stc.join_session(s); stc.end_session(s)
            except: pass
    sid = stc.new_session('dn', 'sw244113_step12_13')
    stc.join_session(sid)
    project = stc.get('system1', 'children-project')
    port1 = stc.create('port', under=project)
    stc.config(port1, {'location': f'//{CHASSIS_IP}/1/25'})
    stc.perform('AttachPorts', params={'RevokeOwner': 'true'})
    stc.apply()
    print(f"Port online: {stc.get(port1, 'Online')}")
    return stc, sid, port1

def create_stream(stc, port, name, src_ip, dst_ip, vlan_id):
    sb = stc.create('streamBlock', under=port)
    stc.config(sb, {'Name': name, 'FixedFrameLength': '128',
                     'LoadUnit': 'FRAMES_PER_SECOND', 'Load': '1000'})
    stc.apply()
    eth = stc.get(sb, 'children-ethernet:EthernetII').split()[0]
    stc.config(eth, {'srcMac': SRC_MAC, 'dstMac': DUT_MAC})
    vlans_c = stc.get(eth, 'children-vlans').split()[0]
    vlan = stc.create('Vlan', under=vlans_c)
    stc.config(vlan, {'id': str(vlan_id)})
    ipv4 = stc.get(sb, 'children-ipv4:IPv4').split()[0]
    stc.config(ipv4, {'sourceAddr': src_ip, 'destAddr': dst_ip, 'ttl': '64'})
    stc.apply()
    return sb

def clear_streams(stc, port):
    sbs = stc.get(port, 'children-streamblock')
    if sbs:
        for sb in sbs.split(): stc.delete(sb)
    stc.apply()

def run_and_measure(stc, port, chan, duration=15, label=""):
    gen = stc.get(port, 'children-generator')
    gen_cfg = stc.get(gen, 'children-generatorconfig')
    stc.config(gen_cfg, {'SchedulingMode': 'PORT_BASED', 'DurationMode': 'CONTINUOUS',
                         'LoadUnit': 'FRAMES_PER_SECOND', 'FixedLoad': '1000'})
    stc.apply()

    c1 = dut_run(chan, "show interfaces counters ge400-0/0/3.100 | no-more", 10)
    print(f"  Starting traffic ({label})...")
    stc.perform('GeneratorStart', params={'GeneratorList': gen})
    time.sleep(duration)
    stc.perform('GeneratorStop', params={'GeneratorList': gen})
    time.sleep(3)
    c2 = dut_run(chan, "show interfaces counters ge400-0/0/3.100 | no-more", 10)

    r = {
        "rx_delta": dut_extract(c2, "RX packets:") - dut_extract(c1, "RX packets:"),
        "v4_drops_delta": dut_extract(c2, "uRPF Ipv4 drops:") - dut_extract(c1, "uRPF Ipv4 drops:"),
        "v6_drops_delta": dut_extract(c2, "uRPF Ipv6 drops:") - dut_extract(c1, "uRPF Ipv6 drops:"),
        "counters_after": c2,
    }
    print(f"  RX Δ: {r['rx_delta']:,}  |  uRPF v4 drops Δ: {r['v4_drops_delta']:,}  |  v6 drops Δ: {r['v6_drops_delta']:,}")
    return r

def main():
    stc, sid, port1 = setup_spirent()
    ssh, chan = dut_connect()
    RESULTS = {}

    # =================================================================
    # STEP 12: Disable allow-default → traffic should be DROPPED
    # =================================================================
    print("\n" + "="*70)
    print("STEP 12: Disable allow-default — same src should be DROPPED")
    print("="*70)

    print("  Disabling allow-default...")
    dut_run(chan, "configure", 5)
    for intf in ["ge400-0/0/3.100", "bundle-10.100", "ge400-0/0/5"]:
        dut_run(chan, f"interfaces {intf} urpf allow-default disabled", 5)
        dut_run(chan, "top", 3)
        dut_run(chan, f"interfaces {intf} urpf address-family ipv4 allow-default disabled", 5)
        dut_run(chan, "top", 3)
        dut_run(chan, f"interfaces {intf} urpf address-family ipv6 allow-default disabled", 5)
        dut_run(chan, "top", 3)
    commit_out = dut_run(chan, "commit", 15)
    print(f"  Commit: {'OK' if 'succeeded' in commit_out.lower() else commit_out[-200:]}")
    dut_run(chan, "end", 3)

    detail = dut_run(chan, "show interfaces detail ge400-0/0/3.100 | no-more", 12)
    print(f"  allow-default: {'disabled' if 'Allow-default: disabled' in detail else 'CHECK'}")

    create_stream(stc, port1, 'Step12_no_allow_default', '10.100.10.100', '10.100.2.100', 100)
    r12 = run_and_measure(stc, port1, chan, label="Step 12")

    if r12['v4_drops_delta'] > 0:
        r12['result'] = 'PASS'
        print("  >>> STEP 12: PASS — allow-default disabled, traffic DROPPED")
    else:
        r12['result'] = 'FAIL'
        print("  >>> STEP 12: FAIL — expected drops")
    RESULTS['step12'] = r12

    # =================================================================
    # STEP 13: VRF isolation
    # =================================================================
    print("\n" + "="*70)
    print("STEP 13: VRF isolation — src valid in default VRF only")
    print("  Src 10.33.0.100 → default VRF has 10.33.0.0/24, urpf-vrf does not")
    print("="*70)

    clear_streams(stc, port1)
    create_stream(stc, port1, 'Step13_vrf_isolation', '10.33.0.100', '10.100.2.100', 100)
    r13 = run_and_measure(stc, port1, chan, label="Step 13")

    if r13['v4_drops_delta'] > 0:
        r13['result'] = 'PASS'
        print("  >>> STEP 13: PASS — VRF isolation holds, traffic DROPPED")
    else:
        r13['result'] = 'FAIL'
        print("  >>> STEP 13: FAIL — expected drops")
    RESULTS['step13'] = r13

    # Save and cleanup
    clear_streams(stc, port1)
    chan.close(); ssh.close()

    with open("/home/dn/output/sw244113_steps12_13.json", "w") as f:
        json.dump(RESULTS, f, indent=2, default=str)

    print("\n" + "="*70)
    print("SUMMARY")
    print("="*70)
    for s in ['step12', 'step13']:
        print(f"  {s}: {RESULTS[s]['result']}")

    stc.end_session(sid)
    print("Done.")

if __name__ == "__main__":
    main()
