"""SW-258863 — uRPF on breakout interface, traffic verification.

Topology
  Spirent 1/25 <-> DUT ge100-0/0/3/0 (10.100.1.1/24, 2001:db8:100::1/64) — uRPF DUT
  Spirent 1/26 <-> DUT ge100-0/0/3/1 (10.200.1.1/24, 2001:db8:2000::1/64) — egress

Per-stream expectations:
  A (v4 valid)     src=10.100.1.50         dst=9.9.9.1   strict=PASS  loose=PASS
  B (v4 invalid)   src=10.200.1.50         dst=9.9.9.1   strict=DROP  loose=PASS
  C (v6 valid)     src=2001:db8:100::50    dst=9999::1   strict=PASS  loose=PASS
  D (v6 invalid)   src=2001:db8:2000::50   dst=9999::1   strict=DROP  loose=PASS
  E (v4 unroute)   src=192.168.99.99       dst=9.9.9.1   strict=DROP  loose=DROP
"""
import paramiko, time, re, sys, os
from stcrestclient import stchttp

DUT_HOST   = 'WKY1C7VD00008P2'
LABSERVER  = 'il-auto-containers'
CHASSIS_IP = '100.64.15.236'
SLOT       = 1
PORT_ING   = 25   # ge100-0/0/3/0
PORT_EGR   = 26   # ge100-0/0/3/1

DUT_IF_ING = 'ge100-0/0/3/0'
DUT_IF_EGR = 'ge100-0/0/3/1'
DUT_MAC_ING = 'e8:c5:7a:d6:30:18'
DUT_MAC_EGR = 'e8:c5:7a:d6:30:19'

ING_V4 = ('10.100.1.1', '10.100.1.2', '24')
EGR_V4 = ('10.200.1.1', '10.200.1.2', '24')
ING_V6 = ('2001:db8:100::1',  '2001:db8:100::2',  '64')
EGR_V6 = ('2001:db8:2000::1', '2001:db8:2000::2', '64')

ROUTE_V4 = '9.9.9.0/24'
ROUTE_V6 = '9999::/64'
DST_V4   = '9.9.9.1'
DST_V6   = '9999::1'

DURATION_S  = 10
RATE_FPS    = 1000
EXPECTED_FRAMES = DURATION_S * RATE_FPS  # per stream

ANSI = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')
LOG_DIR = '/tmp/sw258863'
os.makedirs(LOG_DIR, exist_ok=True)

def clean(s):
    s = ANSI.sub('', s).replace('\r', '')
    s = re.sub(r'-- More -- \(Press q to quit\)\s*', '', s)
    return s

def dut_connect():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(DUT_HOST, username='dnroot', password='dnroot',
              look_for_keys=False, allow_agent=False, timeout=20)
    sh = c.invoke_shell(width=300, height=10000)
    time.sleep(7)
    while sh.recv_ready():
        sh.recv(65536)
    return c, sh

def rp(sh, cmd, wait=4):
    sh.send(cmd + '\n')
    time.sleep(wait)
    out = b''
    retries = 0
    while True:
        if sh.recv_ready():
            out += sh.recv(65536); retries = 0
        else:
            retries += 1
            if retries > 5: break
            time.sleep(0.4)
    return clean(out.decode('utf-8', errors='replace'))

def get_counters(sh, ifname):
    out = rp(sh, f'show interfaces counters {ifname} | no-more', 8)
    def find(label):
        for line in out.split('\n'):
            if label in line:
                m = re.search(r'(\d[\d,]*)', line.split(label, 1)[1])
                if m: return int(m.group(1).replace(',', ''))
        return 0
    return {
        'rx_frames':   find('RX frames:'),
        'tx_frames':   find('TX frames:'),
        'urpf_v4_drops': find('uRPF Ipv4 drops:'),
        'urpf_v6_drops': find('uRPF Ipv6 drops:'),
    }, out

def dut_apply_setup(sh):
    print('--- DUT: apply IPs on /0 + /1, uRPF strict on /0, static routes')
    cmds = [
        'configure',
        f'interfaces {DUT_IF_ING} admin-state enabled',
        f'interfaces {DUT_IF_ING} fec rs-fec-544-514',
        f'interfaces {DUT_IF_ING} ipv4-address {ING_V4[0]}/{ING_V4[2]}',
        f'interfaces {DUT_IF_ING} ipv6-address {ING_V6[0]}/{ING_V6[2]}',
        f'interfaces {DUT_IF_EGR} admin-state enabled',
        f'interfaces {DUT_IF_EGR} fec rs-fec-544-514',
        f'interfaces {DUT_IF_EGR} ipv4-address {EGR_V4[0]}/{EGR_V4[2]}',
        f'interfaces {DUT_IF_EGR} ipv6-address {EGR_V6[0]}/{EGR_V6[2]}',
        f'interfaces {DUT_IF_ING} urpf admin-state enabled',
        f'interfaces {DUT_IF_ING} urpf mode strict',
        f'interfaces {DUT_IF_ING} urpf allow-default disabled',
        f'interfaces {DUT_IF_ING} urpf address-family ipv4 admin-state enabled',
        f'interfaces {DUT_IF_ING} urpf address-family ipv4 mode strict',
        f'interfaces {DUT_IF_ING} urpf address-family ipv6 admin-state enabled',
        f'interfaces {DUT_IF_ING} urpf address-family ipv6 mode strict',
        f'protocols static address-family ipv4-unicast route {ROUTE_V4} next-hop {EGR_V4[1]}',
        f'protocols static address-family ipv6-unicast route {ROUTE_V6} next-hop {EGR_V6[1]}',
        'commit',
        'end',
    ]
    for c in cmds:
        rp(sh, c, 2.5)

def dut_set_mode(sh, mode):
    print(f'--- DUT: set uRPF mode -> {mode} on /0 (global + per-AFI)')
    cmds = [
        'configure',
        f'interfaces {DUT_IF_ING} urpf mode {mode}',
        f'interfaces {DUT_IF_ING} urpf address-family ipv4 mode {mode}',
        f'interfaces {DUT_IF_ING} urpf address-family ipv6 mode {mode}',
        'commit',
        'end',
    ]
    for c in cmds:
        rp(sh, c, 2.5)

def dut_cleanup(sh):
    print('--- DUT: cleanup test-introduced state')
    cmds = [
        'configure',
        f'no interfaces {DUT_IF_ING} urpf',
        f'no protocols static address-family ipv4-unicast route {ROUTE_V4}',
        f'no protocols static address-family ipv6-unicast route {ROUTE_V6}',
        'commit',
        'end',
    ]
    for c in cmds:
        rp(sh, c, 2.5)

def stc_setup_session():
    stc = stchttp.StcHttp(LABSERVER, port=80)
    for s in stc.sessions():
        if 'sw258863_urpf_traffic' in s:
            try:
                stc.join_session(s); stc.end_session(s)
            except Exception:
                pass
    sid = stc.new_session('dn', 'sw258863_urpf_traffic')
    stc.join_session(sid)
    return stc, sid

def stc_setup_ports(stc):
    project = stc.get('system1', 'children-project')
    p_ing = stc.create('port', under=project)
    p_egr = stc.create('port', under=project)
    stc.config(p_ing, {'location': f'//{CHASSIS_IP}/{SLOT}/{PORT_ING}'})
    stc.config(p_egr, {'location': f'//{CHASSIS_IP}/{SLOT}/{PORT_EGR}'})
    stc.perform('AttachPorts', params={'RevokeOwner': 'true'})
    stc.apply()
    return project, p_ing, p_egr

def stc_emulated_dev(stc, project, port, mac, v4, v4gw, v6, v6gw, name):
    """Create an EmulatedDevice with both IPv4 and IPv6 interfaces (no VLAN)."""
    dev = stc.create('EmulatedDevice', under=project,
                     **{'Name': name, 'EnablePingResponse': 'TRUE',
                        'RouterId': v4})
    eth = stc.create('EthIIIf', under=dev, **{'SourceMac': mac})
    v4if = stc.create('Ipv4If', under=dev,
                      **{'Address': v4, 'Gateway': v4gw, 'PrefixLength': '24'})
    v6if = stc.create('Ipv6If', under=dev,
                      **{'Address': v6, 'Gateway': v6gw, 'PrefixLength': '64'})
    stc.config(v4if, **{'StackedOnEndpoint-targets': eth})
    stc.config(v6if, **{'StackedOnEndpoint-targets': eth})
    stc.config(dev, **{'TopLevelIf-targets': f'{v4if} {v6if}',
                       'PrimaryIf-targets': v4if})
    stc.config(port, **{'AffiliationPort-sources': dev})
    stc.apply()
    return dev

def stc_make_stream_v4(stc, port, name, src_ip, dst_ip, dst_mac, src_mac):
    sb = stc.create('streamBlock', under=port)
    stc.config(sb, {'Name': name, 'FixedFrameLength': '128',
                    'LoadUnit': 'FRAMES_PER_SECOND', 'Load': str(RATE_FPS)})
    stc.apply()
    eth = stc.get(sb, 'children-ethernet:EthernetII').split()[0]
    stc.config(eth, {'srcMac': src_mac, 'dstMac': dst_mac})
    ipv4 = stc.get(sb, 'children-ipv4:IPv4').split()[0]
    stc.config(ipv4, {'sourceAddr': src_ip, 'destAddr': dst_ip, 'ttl': '64'})
    stc.apply()
    return sb

def stc_make_stream_v6(stc, port, name, src_ip, dst_ip, dst_mac, src_mac):
    sb = stc.create('streamBlock', under=port)
    stc.config(sb, {'Name': name, 'FixedFrameLength': '128',
                    'LoadUnit': 'FRAMES_PER_SECOND', 'Load': str(RATE_FPS)})
    stc.apply()
    eth = stc.get(sb, 'children-ethernet:EthernetII').split()[0]
    stc.config(eth, {'srcMac': src_mac, 'dstMac': dst_mac})
    ipv4 = stc.get(sb, 'children-ipv4:IPv4').split()[0]
    stc.delete(ipv4)
    ipv6 = stc.create('ipv6:IPv6', under=sb)
    stc.config(ipv6, {'sourceAddr': src_ip, 'destAddr': dst_ip, 'hopLimit': '64'})
    stc.apply()
    return sb

def stc_run_traffic(stc, port_ing, port_egr, label):
    print(f'--- Spirent: run traffic ({label}) for {DURATION_S}s @ {RATE_FPS}fps/stream')
    # Reset analyzer counters by stopping/starting analyzers (use AnalyzerStart to clear)
    stc.perform('AnalyzerStart', params={'AnalyzerList': stc.get(port_ing, 'children-analyzer')})
    stc.perform('AnalyzerStart', params={'AnalyzerList': stc.get(port_egr, 'children-analyzer')})
    gen_ing = stc.get(port_ing, 'children-generator')
    gen_cfg = stc.get(gen_ing, 'children-generatorconfig')
    stc.config(gen_cfg, {'SchedulingMode': 'PORT_BASED',
                         'DurationMode': 'CONTINUOUS',
                         'LoadUnit': 'FRAMES_PER_SECOND',
                         'FixedLoad': str(RATE_FPS * 5)})  # combined rate
    stc.apply()
    stc.perform('GeneratorStart', params={'GeneratorList': gen_ing})
    time.sleep(DURATION_S)
    stc.perform('GeneratorStop', params={'GeneratorList': gen_ing})
    time.sleep(2)

def stc_get_streamblock_results(stc, sb):
    out = {}
    for child_attr, label, fields in [
        ('children-txstreamresults', 'tx', ['FrameCount']),
        ('children-rxstreamsummaryresults', 'rx', ['FrameCount', 'SigFrameCount']),
    ]:
        try:
            res = stc.get(sb, child_attr)
            if res and res.strip():
                rh = res.split()[0]
                for f in fields:
                    try:
                        v = stc.get(rh, f)
                        out[f'{label}_{f}'] = int(v) if (v or '').strip().isdigit() else v
                    except Exception:
                        pass
        except Exception:
            pass
    return out or None

def stc_port_results(stc, port):
    out = {'tx_total': None, 'rx_total': None}
    try:
        rgp = stc.get(port, 'children-generatorportresults')
        if rgp and rgp.strip():
            out['tx_total'] = int(stc.get(rgp.split()[0], 'TotalFrameCount') or 0)
    except Exception as e:
        out['tx_total_err'] = str(e)[:100]
    try:
        rap = stc.get(port, 'children-analyzerportresults')
        if rap and rap.strip():
            out['rx_total'] = int(stc.get(rap.split()[0], 'TotalFrameCount') or 0)
    except Exception as e:
        out['rx_total_err'] = str(e)[:100]
    return out

def banner(s):
    print('\n' + '=' * 72); print(s); print('=' * 72)

# ============================ Main ============================

if __name__ == '__main__':
    # ---- Phase 1: DUT setup
    banner('Phase 1 — DUT setup (uRPF strict + static routes)')
    c, sh = dut_connect()
    dut_apply_setup(sh)
    print(rp(sh, f'show interfaces {DUT_IF_ING} | no-more', 6))
    print(rp(sh, f'show route {ROUTE_V4} | no-more', 5))
    print(rp(sh, f'show route {ROUTE_V6} | no-more', 5))
    c.close()

    # ---- Phase 2: Spirent setup
    banner('Phase 2 — Spirent reserve ports + emulated devices')
    stc, sid = stc_setup_session()
    try:
        project, p_ing, p_egr = stc_setup_ports(stc)
        print(f'Port ING online: {stc.get(p_ing, "Online")}')
        print(f'Port EGR online: {stc.get(p_egr, "Online")}')

        dev_ing = stc_emulated_dev(stc, project, p_ing,
                                   '00:10:94:00:00:25',
                                   ING_V4[1], ING_V4[0],
                                   ING_V6[1], ING_V6[0],
                                   'ing_dev')
        dev_egr = stc_emulated_dev(stc, project, p_egr,
                                   '00:10:94:00:00:26',
                                   EGR_V4[1], EGR_V4[0],
                                   EGR_V6[1], EGR_V6[0],
                                   'egr_dev')

        # Set 100G FEC = KP4 (matches DUT rs-fec-544-514)
        for prt in (p_ing, p_egr):
            phy = stc.get(prt, 'children-Ethernet100GigFiber')
            if phy:
                stc.config(phy, {'CustomFecMode': 'KP4_FEC', 'AutoNegotiation': 'false'})
        stc.apply()
        print('Waiting for Spirent PHY to lock up to 30s...')
        for i in range(15):
            time.sleep(2)
            ls_i = stc.get(stc.get(p_ing, 'children-Ethernet100GigFiber'), 'LinkStatus')
            ls_e = stc.get(stc.get(p_egr, 'children-Ethernet100GigFiber'), 'LinkStatus')
            print(f'  t={i*2:>2}s  ING LinkStatus={ls_i}  EGR LinkStatus={ls_e}')
            if ls_i == 'UP' and ls_e == 'UP':
                break

        print('Resolving ARP/NDP (3 attempts)...')
        for attempt in range(3):
            stc.perform('ArpNdStartCommand', params={'HandleList': f'{p_ing} {p_egr}'})
            time.sleep(6)
        stc.perform('DeviceStartCommand', params={'DeviceList': f'{dev_ing} {dev_egr}'})
        time.sleep(5)

        # ---- Phase 3: Build streams on ingress port
        banner('Phase 3 — Build streams on ingress port')
        # Cleanup any existing
        for sb in (stc.get(p_ing, 'children-streamblock') or '').split():
            stc.delete(sb)
        stc.apply()

        sb_a = stc_make_stream_v4(stc, p_ing, 'A_v4_valid',
                                  '10.100.1.50', DST_V4, DUT_MAC_ING, '00:10:94:00:00:aa')
        sb_b = stc_make_stream_v4(stc, p_ing, 'B_v4_invalid',
                                  '10.200.1.50', DST_V4, DUT_MAC_ING, '00:10:94:00:00:bb')
        sb_c = stc_make_stream_v6(stc, p_ing, 'C_v6_valid',
                                  '2001:db8:100::50', DST_V6, DUT_MAC_ING, '00:10:94:00:00:cc')
        sb_d = stc_make_stream_v6(stc, p_ing, 'D_v6_invalid',
                                  '2001:db8:2000::50', DST_V6, DUT_MAC_ING, '00:10:94:00:00:dd')
        sb_e = stc_make_stream_v4(stc, p_ing, 'E_v4_unroutable',
                                  '192.168.99.99', DST_V4, DUT_MAC_ING, '00:10:94:00:00:ee')

        streams = [('A', sb_a, 'v4', 'valid'),
                   ('B', sb_b, 'v4', 'invalid-strict'),
                   ('C', sb_c, 'v6', 'valid'),
                   ('D', sb_d, 'v6', 'invalid-strict'),
                   ('E', sb_e, 'v4', 'unroutable')]
        # Save handles for results
        sb_handles = [t[1] for t in streams]

        # ---- Phase 4: Verify ARP/NDP on DUT (retry until populated)
        banner('Phase 4 — Verify ARP/NDP on DUT')
        c, sh = dut_connect()
        for attempt in range(8):
            arp0 = rp(sh, f'show arp interface {DUT_IF_ING} | no-more', 4)
            arp1 = rp(sh, f'show arp interface {DUT_IF_EGR} | no-more', 4)
            ok0 = '10.100.1.2' in arp0
            ok1 = '10.200.1.2' in arp1
            print(f'  attempt {attempt+1}: ARP /0={ok0}  ARP /1={ok1}')
            if ok0 and ok1:
                break
            # Re-arp on Spirent
            try:
                stc.perform('ArpNdStartCommand', params={'HandleList': f'{p_ing} {p_egr}'})
            except Exception:
                pass
            time.sleep(5)
        print(arp0); print(arp1)
        print(rp(sh, f'show ndp interface {DUT_IF_ING} | no-more', 4))
        print(rp(sh, f'show ndp interface {DUT_IF_EGR} | no-more', 4))
        # Re-apply static routes if first attempt failed (next-hop now reachable)
        rp(sh, 'configure', 2)
        rp(sh, f'protocols static address-family ipv4-unicast route {ROUTE_V4} next-hop {EGR_V4[1]}', 2)
        rp(sh, f'protocols static address-family ipv6-unicast route {ROUTE_V6} next-hop {EGR_V6[1]}', 2)
        rp(sh, 'commit', 4)
        rp(sh, 'end', 2)
        print(rp(sh, f'show route {ROUTE_V4} | no-more', 4))
        print(rp(sh, f'show route {ROUTE_V6} | no-more', 4))

        results = {}

        for mode in ('strict', 'loose'):
            banner(f'Phase 5/{mode} — uRPF mode={mode}')
            dut_set_mode(sh, mode)
            time.sleep(2)
            print(rp(sh, f'show interfaces {DUT_IF_ING} | no-more', 5))
            counters_before, _ = get_counters(sh, DUT_IF_ING)
            counters_egr_before, _ = get_counters(sh, DUT_IF_EGR)
            print(f'BEFORE: ing {counters_before}')
            print(f'BEFORE: egr {counters_egr_before}')

            stc_run_traffic(stc, p_ing, p_egr, label=mode)

            time.sleep(3)
            counters_after, raw_after = get_counters(sh, DUT_IF_ING)
            counters_egr_after, _ = get_counters(sh, DUT_IF_EGR)
            print(f'AFTER:  ing {counters_after}')
            print(f'AFTER:  egr {counters_egr_after}')

            # Per-stream results
            per_stream = {}
            for name, sb, fam, kind in streams:
                r = stc_get_streamblock_results(stc, sb)
                per_stream[name] = (fam, kind, r)
                print(f'  Stream {name} ({fam}, {kind}): {r}')
            port_egr_res = stc_port_results(stc, p_egr)
            print(f'  Port EGR totals: {port_egr_res}')

            results[mode] = {
                'dut_ing_before':  counters_before,
                'dut_ing_after':   counters_after,
                'dut_ing_delta':   {k: counters_after[k]-counters_before[k] for k in counters_before},
                'dut_egr_before':  counters_egr_before,
                'dut_egr_after':   counters_egr_after,
                'dut_egr_delta':   {k: counters_egr_after[k]-counters_egr_before[k] for k in counters_egr_before},
                'streams':         per_stream,
                'port_egr_total':  port_egr_res,
            }

        # ---- Phase 6: Cleanup DUT
        banner('Phase 6 — DUT cleanup')
        dut_cleanup(sh)
        print(rp(sh, f'show interfaces {DUT_IF_ING} | no-more', 5))
        print(rp(sh, 'show interfaces breakout | no-more', 5))
        c.close()

    finally:
        try: stc.end_session(sid)
        except Exception: pass

    # ---- Summary
    banner('Summary')
    import json
    with open(os.path.join(LOG_DIR, 'traffic_results.json'), 'w') as f:
        json.dump(results, f, indent=2, default=str)
    print(json.dumps(results, indent=2, default=str))
