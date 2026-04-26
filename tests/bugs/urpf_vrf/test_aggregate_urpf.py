"""
Test: Aggregate route — uRPF pass
1. Configure contributing static route 10.40.1.0/24 via Spirent interface
2. Configure aggregate route 10.40.0.0/16 null0
3. Send traffic with source 10.40.1.1 from Spirent
4. Verify forwarding and no uRPF drops (longest match /24 via ingress)
"""
import paramiko, time, re, json
from stcrestclient import stchttp

DUT_HOST = 'WKY1C7VD00008P2'
SUB_IF = 'ge400-0/0/3.100'
SPIRENT_GW = '10.100.1.2'

LABSERVER = 'il-auto-containers'
CHASSIS_IP = '100.64.15.236'
SLOT, PORT = 1, 25
SRC_MAC = '00:10:94:01:19:01'
DUT_MAC = 'e8:c5:7a:d6:30:18'
VLAN_ID = '100'
DEST_IP = '20.0.0.2'
SRC_IP = '10.40.1.1'

def dut_connect():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(DUT_HOST, username='dnroot', password='dnroot',
              look_for_keys=False, allow_agent=False, timeout=15)
    sh = c.invoke_shell(width=250, height=5000)
    time.sleep(6); sh.recv(65535)
    return c, sh

def rp(sh, cmd, wait=5):
    sh.send(cmd + '\n')
    time.sleep(wait)
    out = b''
    while sh.recv_ready():
        out += sh.recv(65535); time.sleep(0.3)
    txt = out.decode('utf-8', errors='replace')
    txt = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', txt)
    txt = re.sub(r'\r', '', txt)
    return txt

def extract_counter(text, label):
    for line in text.split('\n'):
        if label.lower() in line.lower():
            nums = re.findall(r'[\d,]+', line)
            for n in nums:
                v = int(n.replace(',', ''))
                if v >= 0:
                    return v
    return 0

def get_urpf_counters(sh):
    out = rp(sh, f'show interfaces counters {SUB_IF} | no-more', 10)
    rx = extract_counter(out, 'RX packets:')
    drops = extract_counter(out, 'uRPF Ipv4 drops:')
    return rx, drops, out

# ============================
# STEP 1: Configure routes
# ============================
print('='*60)
print('STEP 1: Configure contributing static + aggregate null0')
print('='*60)

client, sh = dut_connect()
for cmd in ['configure',
            'protocols static address-family ipv4-unicast',
            'route 10.40.1.0/24', f'next-hop {SPIRENT_GW}', 'exit',
            'route 10.40.0.0/16', 'null0', 'commit', 'end']:
    rp(sh, cmd, 3)

time.sleep(3)
print(rp(sh, 'show route 10.40.1.0/24 | no-more', 8))
print(rp(sh, 'show route 10.40.0.0/16 | no-more', 8))

rx_before, drops_before, _ = get_urpf_counters(sh)
print(f'Baseline: RX={rx_before:,}  uRPF drops={drops_before:,}')
client.close()

# ============================
# STEP 2: Send traffic
# ============================
print('\n'+'='*60)
print(f'STEP 2: Send traffic src={SRC_IP} dst={DEST_IP} (12s @ 1000fps)')
print('='*60)

stc = stchttp.StcHttp(LABSERVER, port=80)
for s in stc.sessions():
    if 'aggr_test' in s:
        try: stc.join_session(s); stc.end_session(s)
        except: pass

sid = stc.new_session('dn', 'aggr_test')
stc.join_session(sid)
project = stc.get('system1', 'children-project')
port1 = stc.create('port', under=project)
stc.config(port1, {'location': f'//{CHASSIS_IP}/{SLOT}/{PORT}'})
stc.perform('AttachPorts', params={'RevokeOwner': 'true'})
stc.apply()
print(f'Port online: {stc.get(port1, "Online")}')

sb = stc.create('streamBlock', under=port1)
stc.config(sb, {'Name': 'aggr_pass', 'FixedFrameLength': '128',
                'LoadUnit': 'FRAMES_PER_SECOND', 'Load': '1000'})
stc.apply()

eth = stc.get(sb, 'children-ethernet:EthernetII').split()[0]
stc.config(eth, {'srcMac': SRC_MAC, 'dstMac': DUT_MAC})
vlans_c = stc.get(eth, 'children-vlans').split()[0]
vlan = stc.create('Vlan', under=vlans_c)
stc.config(vlan, {'id': VLAN_ID})
ipv4 = stc.get(sb, 'children-ipv4:IPv4').split()[0]
stc.config(ipv4, {'sourceAddr': SRC_IP, 'destAddr': DEST_IP, 'ttl': '64'})
stc.apply()

gen = stc.get(port1, 'children-generator')
gen_cfg = stc.get(gen, 'children-generatorconfig')
stc.config(gen_cfg, {'SchedulingMode': 'PORT_BASED', 'DurationMode': 'CONTINUOUS',
                     'LoadUnit': 'FRAMES_PER_SECOND', 'FixedLoad': '1000'})
stc.apply()

print('Starting traffic...')
stc.perform('GeneratorStart', params={'GeneratorList': gen})
time.sleep(12)
stc.perform('GeneratorStop', params={'GeneratorList': gen})
time.sleep(2)
print('Traffic stopped.')

stc.end_session(sid)

# ============================
# STEP 3: Check results
# ============================
print('\n'+'='*60)
print('STEP 3: Check uRPF drops')
print('='*60)

client, sh = dut_connect()
rx_after, drops_after, counters_out = get_urpf_counters(sh)

rx_delta = rx_after - rx_before
drop_delta = drops_after - drops_before

print(f'After:    RX={rx_after:,}  uRPF drops={drops_after:,}')
print(f'Delta:    RX Δ={rx_delta:,}  uRPF drops Δ={drop_delta:,}')

if drop_delta == 0 and rx_delta > 0:
    print('\n>>> PASS: No uRPF drops — aggregate with contributing static via ingress passes uRPF <<<')
elif drop_delta == 0 and rx_delta == 0:
    print('\n>>> INCONCLUSIVE: No RX delta and no drops — traffic may not have arrived <<<')
else:
    print(f'\n>>> FAIL: {drop_delta:,} uRPF drops detected <<<')

# ============================
# STEP 4: Cleanup
# ============================
print('\n'+'='*60)
print('STEP 4: Cleanup')
print('='*60)
for cmd in ['configure', 'protocols static address-family ipv4-unicast',
            'no route 10.40.1.0/24', 'no route 10.40.0.0/16', 'commit', 'end']:
    rp(sh, cmd, 3)

print(rp(sh, 'show route 10.40.0.0/16 | no-more', 5))
client.close()
print('Done.')
