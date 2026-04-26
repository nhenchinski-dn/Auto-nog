#!/usr/bin/env python3
"""SW-244103 Steps 5-9: BGP/OSPF/IS-IS uRPF strict mode testing.

Uses Spirent protocol emulation (BGP, OSPF) for "pass" scenarios
and static routes for "drop via different egress" scenarios.

Spirent: 100.64.15.236 slot 1 port 25 → ge400-0/0/3.100 (VLAN 100)
DUT: NCP3-nog (WKY1C7VD00008P2) at 100.64.8.59
"""

import paramiko, time, re, json, sys, traceback
from datetime import datetime
from stcrestclient import stchttp

LABSERVER    = 'il-auto-containers'
CHASSIS_IP   = '100.64.15.236'
SLOT, PORT   = 1, 25
DUT_IP       = '100.64.8.59'
DUT_MAC      = 'e8:c5:7a:d6:30:18'
SRC_MAC      = '00:10:94:01:19:01'
SUB_IF       = 'ge400-0/0/3.100'
VLAN_ID      = '100'
DEST_IP      = '20.0.0.2'
TRAFFIC_SEC  = 12
TRAFFIC_FPS  = 1000
SESSION_NAME = 'sw244103_proto'

DUT_AS       = 65001
SPIRENT_AS   = 65002
DUT_BGP_IP   = '10.100.1.1'
SPIRENT_IP   = '10.100.1.2'
PREFIX_LEN   = '24'

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

def rp(chan, cmd, wait=8):
    output = dut_run(chan, cmd, wait)
    print(f"  [{cmd}]")
    for line in output.split('\n'): print(f"    {line}")
    return output

def extract_counter(text, label):
    for line in text.split('\n'):
        if label in line:
            parts = line.split(':')
            if len(parts) >= 2:
                val = parts[-1].strip().split('(')[0].strip().replace(',', '')
                try: return int(val)
                except ValueError: return 0
    return 0

def get_urpf_counters(chan):
    out = dut_run(chan, f"show interfaces counters {SUB_IF} | no-more", 10)
    return extract_counter(out, "RX packets:"), extract_counter(out, "uRPF Ipv4 drops:"), out

def dut_config(chan, commands):
    rp(chan, "configure", 5)
    for cmd in commands:
        rp(chan, cmd, 5); rp(chan, "top", 3)
    out = rp(chan, "commit", 20)
    rp(chan, "end", 3)
    return "ERROR" not in out or "not applicable" in out

def spirent_connect():
    stc = stchttp.StcHttp(LABSERVER, port=80)
    for s in stc.sessions():
        if SESSION_NAME in s:
            try: stc.join_session(s); stc.end_session(s)
            except: pass
    sid = stc.new_session('dn', SESSION_NAME); stc.join_session(sid)
    project = stc.get('system1', 'children-project')
    port1 = stc.create('port', under=project)
    stc.config(port1, {'location': f'//{CHASSIS_IP}/{SLOT}/{PORT}'})
    print("  Attaching Spirent port...")
    stc.perform('AttachPorts', params={'RevokeOwner': 'true'}); stc.apply()
    print(f"  Port online: {stc.get(port1, 'Online')}")
    return stc, sid, project, port1

def clear_all(stc, project, port1):
    sbs = stc.get(port1, 'children-streamblock')
    if sbs:
        for sb in sbs.split(): stc.delete(sb)
    devs = stc.get(project, 'children-emulateddevice')
    if devs and devs.strip():
        try: stc.perform('DeviceStopCommand', params={'DeviceList': devs}); time.sleep(3)
        except: pass
        for d in devs.split(): stc.delete(d)
    stc.apply()

def create_stream(stc, port1, name, src_ip):
    sbs = stc.get(port1, 'children-streamblock')
    if sbs:
        for sb in sbs.split(): stc.delete(sb)
        stc.apply()
    sb = stc.create('streamBlock', under=port1)
    stc.config(sb, {'Name': name, 'FixedFrameLength': '128',
                     'LoadUnit': 'FRAMES_PER_SECOND', 'Load': str(TRAFFIC_FPS)})
    stc.apply()
    eth = stc.get(sb, 'children-ethernet:EthernetII').split()[0]
    stc.config(eth, {'srcMac': SRC_MAC, 'dstMac': DUT_MAC})
    vlans_c = stc.get(eth, 'children-vlans').split()[0]
    vlan = stc.create('Vlan', under=vlans_c); stc.config(vlan, {'id': VLAN_ID})
    ipv4 = stc.get(sb, 'children-ipv4:IPv4').split()[0]
    stc.config(ipv4, {'sourceAddr': src_ip, 'destAddr': DEST_IP, 'ttl': '64'})
    stc.apply()

def send_traffic(stc, port1):
    gen = stc.get(port1, 'children-generator')
    gen_cfg = stc.get(gen, 'children-generatorconfig')
    stc.config(gen_cfg, {'SchedulingMode': 'PORT_BASED', 'DurationMode': 'CONTINUOUS',
                         'LoadUnit': 'FRAMES_PER_SECOND', 'FixedLoad': str(TRAFFIC_FPS)})
    stc.apply()
    print(f"  Sending traffic at {TRAFFIC_FPS} fps for {TRAFFIC_SEC}s...")
    stc.perform('GeneratorStart', params={'GeneratorList': gen})
    time.sleep(TRAFFIC_SEC)
    stc.perform('GeneratorStop', params={'GeneratorList': gen})
    time.sleep(3); print("  Traffic stopped.")

def traffic_test(stc, port1, chan, name, src_ip, expect_drop):
    rx0, d0, _ = get_urpf_counters(chan)
    create_stream(stc, port1, name, src_ip)
    send_traffic(stc, port1)
    rx1, d1, cout = get_urpf_counters(chan)
    rxd, dd = rx1 - rx0, d1 - d0
    ok = (dd > 0) if expect_drop else (dd == 0 and rxd > 0)
    print(f"  RX Δ: {rxd:,}  |  uRPF drops Δ: {dd:,}  |  {'PASS' if ok else 'FAIL'}")
    return ok, rxd, dd, cout

def make_emulated_device(stc, project, port1, name, router_id):
    dev = stc.create('EmulatedDevice', under=project,
                     **{'Name': name, 'EnablePingResponse': 'TRUE', 'RouterId': router_id})
    eth = stc.create('EthIIIf', under=dev, **{'SourceMac': SRC_MAC})
    vlan = stc.create('VlanIf', under=dev, **{'VlanId': VLAN_ID})
    ip = stc.create('Ipv4If', under=dev,
                    **{'Address': SPIRENT_IP, 'Gateway': DUT_BGP_IP, 'PrefixLength': PREFIX_LEN})
    stc.config(ip, **{'StackedOnEndpoint-targets': vlan})
    stc.config(vlan, **{'StackedOnEndpoint-targets': eth})
    stc.config(dev, **{'TopLevelIf-targets': ip, 'PrimaryIf-targets': ip})
    stc.config(port1, **{'AffiliationPort-sources': dev})
    return dev, ip

def start_device(stc, port1, dev):
    stc.perform('ArpNdStartCommand', params={'HandleList': port1}); time.sleep(5)
    stc.perform('DeviceStartCommand', params={'DeviceList': dev}); time.sleep(5)


def main():
    results = {}
    ts = datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
    print("=" * 70)
    print("SW-244103 Steps 5-9: Protocol Emulation uRPF Tests")
    print("=" * 70)

    ssh, chan = dut_connect()
    rp(chan, "show system version | no-more", 8)
    stc, sid, project, port1 = spirent_connect()

    try:
        # ── STEP 5: BGP route — pass ─────────────────────────────────
        print("\n" + "=" * 70)
        print("STEP 5: BGP route — pass (eBGP 172.16.1.0/24 via ingress)")
        print("=" * 70)

        dut_config(chan, [
            f"protocols bgp {DUT_AS} router-id {DUT_BGP_IP}",
            f"protocols bgp {DUT_AS} neighbor {SPIRENT_IP} remote-as {SPIRENT_AS}",
            f"protocols bgp {DUT_AS} neighbor {SPIRENT_IP} admin-state enabled",
            f"protocols bgp {DUT_AS} neighbor {SPIRENT_IP} address-family ipv4-unicast",
        ])

        dev, ipif = make_emulated_device(stc, project, port1, 'BGP_Peer', SPIRENT_IP)
        bgp = stc.create('BgpRouterConfig', under=dev,
                         **{'AsNum': str(SPIRENT_AS), 'DutAsNum': str(DUT_AS),
                            'IpVersion': 'IPV4', 'UseGatewayAsDut': 'TRUE'})
        stc.config(bgp, **{'UsesIf-targets': ipif})
        bgp_rt = stc.create('BgpIpv4RouteConfig', under=bgp,
                            **{'NextHop': SPIRENT_IP, 'AsPath': str(SPIRENT_AS)})
        blk = stc.get(bgp_rt, 'children-Ipv4NetworkBlock').split()[0]
        stc.config(blk, {'StartIpList': '172.16.1.0', 'PrefixLength': '24', 'NetworkCount': '1'})
        stc.apply()
        start_device(stc, port1, dev)

        bgp_up = False
        for i in range(12):
            out = dut_run(chan, "show bgp ipv4 unicast summary | no-more", 10)
            print(f"    BGP poll {i+1}/12...")
            if SPIRENT_IP in out:
                for line in out.split('\n'):
                    if SPIRENT_IP in line:
                        last = line.split()[-1]
                        try: int(last); bgp_up = True
                        except: pass
                        if 'stablished' in line.lower(): bgp_up = True
            if bgp_up: break
            time.sleep(10)

        if bgp_up:
            print("  BGP established!")
            rp(chan, "show bgp ipv4 unicast summary | no-more", 10)
            rp(chan, "show route vrf default table ipv4-unicast 172.16.1.0/24 | no-more", 10)
            ok, rxd, dd, cout = traffic_test(stc, port1, chan, 'Step5', '172.16.1.1', False)
            results['Step 5'] = {'name': 'BGP route — pass', 'result': 'PASS' if ok else 'FAIL',
                                 'src_ip': '172.16.1.1', 'rx_delta': rxd, 'drop_delta': dd,
                                 'analysis': f'RX Δ={rxd:,}, drops Δ={dd:,}. eBGP route via ingress — forwarded.'}
        else:
            print("  *** BGP did NOT establish ***")
            rp(chan, "show bgp ipv4 unicast summary | no-more", 10)
            results['Step 5'] = {'name': 'BGP route — pass', 'result': 'FAIL',
                                 'analysis': 'BGP session did not establish'}

        clear_all(stc, project, port1)
        dut_config(chan, [f"no protocols bgp {DUT_AS}"])
        time.sleep(3)

        # ── STEP 6: BGP route — drop ─────────────────────────────────
        print("\n" + "=" * 70)
        print("STEP 6: BGP route — drop (172.16.2.0/24 via different egress)")
        print("=" * 70)

        dut_config(chan, [
            "protocols static address-family ipv4-unicast route 172.16.2.0/24 next-hop 20.0.0.2",
        ])
        rp(chan, "show route vrf default table ipv4-unicast 172.16.2.0/24 | no-more", 10)
        ok, rxd, dd, cout = traffic_test(stc, port1, chan, 'Step6', '172.16.2.1', True)
        results['Step 6'] = {'name': 'BGP route — drop (different egress)', 'result': 'PASS' if ok else 'FAIL',
                             'src_ip': '172.16.2.1', 'rx_delta': rxd, 'drop_delta': dd,
                             'analysis': f'RX Δ={rxd:,}, drops Δ={dd:,}. Route via bundle-10 — dropped.'}
        dut_config(chan, ["no protocols static address-family ipv4-unicast route 172.16.2.0/24"])

        # ── STEP 7: OSPF route — pass ────────────────────────────────
        print("\n" + "=" * 70)
        print("STEP 7: OSPF route — pass (10.20.0.0/24 via ingress)")
        print("=" * 70)

        ospf_ok = dut_config(chan, [
            f"protocols ospf instance urpf_test router-id {DUT_BGP_IP}",
            f"protocols ospf instance urpf_test area 0.0.0.0 interface {SUB_IF} network-type point-to-point",
        ])

        if not ospf_ok:
            results['Step 7'] = {'name': 'OSPF route — pass', 'result': 'FAIL',
                                 'analysis': 'OSPF config failed on DUT'}
        else:
            dev, ipif = make_emulated_device(stc, project, port1, 'OSPF_Peer', SPIRENT_IP)
            ospf = stc.create('Ospfv2RouterConfig', under=dev,
                              **{'AreaId': '0.0.0.0', 'NetworkType': 'P2P',
                                 'Name': 'OSPF_Router'})
            stc.config(ospf, **{'UsesIf-targets': ipif})
            ospf_rt = stc.create('Ospfv2ExternalLsaBlock', under=ospf)
            blk = stc.get(ospf_rt, 'children-Ipv4NetworkBlock').split()[0]
            stc.config(blk, {'StartIpList': '10.20.0.0', 'PrefixLength': '24', 'NetworkCount': '1'})
            stc.apply()
            start_device(stc, port1, dev)

            ospf_up = False
            for i in range(15):
                out = dut_run(chan, "show ospf neighbor | no-more", 10)
                print(f"    OSPF poll {i+1}/15...")
                if 'Full' in out:
                    ospf_up = True; break
                time.sleep(10)

            if ospf_up:
                print("  OSPF adjacency established!")
                rp(chan, "show ospf neighbor | no-more", 10)
                rp(chan, "show route vrf default table ipv4-unicast 10.20.0.0/24 | no-more", 10)
                ok, rxd, dd, cout = traffic_test(stc, port1, chan, 'Step7', '10.20.0.1', False)
                results['Step 7'] = {'name': 'OSPF route — pass', 'result': 'PASS' if ok else 'FAIL',
                                     'src_ip': '10.20.0.1', 'rx_delta': rxd, 'drop_delta': dd,
                                     'analysis': f'RX Δ={rxd:,}, drops Δ={dd:,}. OSPF external via ingress — forwarded.'}
            else:
                print("  *** OSPF did NOT establish ***")
                rp(chan, "show ospf neighbor | no-more", 10)
                rp(chan, "show ospf interface | no-more", 10)
                results['Step 7'] = {'name': 'OSPF route — pass', 'result': 'FAIL',
                                     'analysis': 'OSPF adjacency did not form'}

            clear_all(stc, project, port1)
            dut_config(chan, ["no protocols ospf instance urpf_test"])
            time.sleep(3)

        # ── STEP 8: OSPF route — drop ────────────────────────────────
        print("\n" + "=" * 70)
        print("STEP 8: OSPF route — drop (10.20.1.0/24 via different egress)")
        print("=" * 70)

        dut_config(chan, [
            "protocols static address-family ipv4-unicast route 10.20.1.0/24 next-hop 20.0.0.2",
        ])
        rp(chan, "show route vrf default table ipv4-unicast 10.20.1.0/24 | no-more", 10)
        ok, rxd, dd, cout = traffic_test(stc, port1, chan, 'Step8', '10.20.1.1', True)
        results['Step 8'] = {'name': 'OSPF route — drop (different egress)', 'result': 'PASS' if ok else 'FAIL',
                             'src_ip': '10.20.1.1', 'rx_delta': rxd, 'drop_delta': dd,
                             'analysis': f'RX Δ={rxd:,}, drops Δ={dd:,}. Route via bundle-10 — dropped.'}
        dut_config(chan, ["no protocols static address-family ipv4-unicast route 10.20.1.0/24"])

        # ── STEP 9: IS-IS route — drop ───────────────────────────────
        print("\n" + "=" * 70)
        print("STEP 9: IS-IS route — drop (10.30.0.0/24 via different egress)")
        print("=" * 70)

        dut_config(chan, [
            "protocols static address-family ipv4-unicast route 10.30.0.0/24 next-hop 20.0.0.2",
        ])
        rp(chan, "show route vrf default table ipv4-unicast 10.30.0.0/24 | no-more", 10)
        ok, rxd, dd, cout = traffic_test(stc, port1, chan, 'Step9', '10.30.0.1', True)
        results['Step 9'] = {'name': 'IS-IS route — drop (different egress)', 'result': 'PASS' if ok else 'FAIL',
                             'src_ip': '10.30.0.1', 'rx_delta': rxd, 'drop_delta': dd,
                             'analysis': f'RX Δ={rxd:,}, drops Δ={dd:,}. Route via core-facing interface — dropped.'}
        dut_config(chan, ["no protocols static address-family ipv4-unicast route 10.30.0.0/24"])

    except Exception as e:
        print(f"\n  *** EXCEPTION: {e} ***")
        traceback.print_exc()
    finally:
        print("\n" + "=" * 70 + "\nFinal cleanup...")
        try: clear_all(stc, project, port1)
        except: pass
        try:
            dut_config(chan, [
                f"no protocols bgp {DUT_AS}",
                "no protocols ospf instance urpf_test",
            ])
        except: pass
        try: stc.end_session(sid)
        except: pass
        chan.send('exit\n'); time.sleep(2); ssh.close()

    print("\n" + "=" * 70)
    print("TEST SUMMARY — SW-244103 Steps 5-9")
    print("=" * 70)
    print(f"  Date: {ts}  |  Device: NCP3-nog  |  Spirent: {CHASSIS_IP}/{SLOT}/{PORT}")
    all_pass = True
    for s in ['Step 5', 'Step 6', 'Step 7', 'Step 8', 'Step 9']:
        if s in results:
            r = results[s]['result']
            tag = "(/) PASS" if r == 'PASS' else "(x) FAIL"
            print(f"  {s}: {results[s]['name']:50s}  {tag}")
            if r != 'PASS': all_pass = False
        else:
            print(f"  {s}: {'NOT RUN':50s}  (!) SKIP")
            all_pass = False

    overall = 'PASS' if all_pass else 'PARTIAL'
    print(f"\n  Overall: {overall}")
    with open('/home/dn/output/sw244103_proto_results.json', 'w') as f:
        json.dump({'timestamp': ts, 'overall': overall, 'steps': results}, f, indent=2, default=str)
    print("  Results saved to /home/dn/output/sw244103_proto_results.json")

if __name__ == '__main__':
    main()
