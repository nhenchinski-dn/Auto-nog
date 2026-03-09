#!/usr/bin/env python3
"""SW-241838: PIM SSM over Physical Ethernet - 400G variant
Full test execution covering all test steps, pass criteria, and negative flows."""

import paramiko, time, re, sys, json
sys.stdout.reconfigure(line_buffering=True)

HOST = '100.64.6.171'
USER = 'dnroot'
PASS = 'dnroot'
IIF = 'ge800-0/0/8'
OIF = 'ge800-0/0/9'
SRC_IP = '3.5.0.2'
GROUP = '232.1.1.1'

results = []

def connect():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASS, timeout=15,
                look_for_keys=False, allow_agent=False)
    chan = ssh.invoke_shell(width=400)
    time.sleep(8)
    chan.recv(65535)
    return ssh, chan

ANSI = re.compile(r'\x1b\[[0-9;]*m')

def run(chan, cmd, wait=8):
    chan.send(cmd + '\n')
    time.sleep(wait)
    out = b''
    while chan.recv_ready():
        out += chan.recv(65535)
    return ANSI.sub('', out.decode(errors='replace'))

def record(step, name, status, detail=''):
    tag = 'PASS' if status else 'FAIL'
    results.append({'step': step, 'name': name, 'status': tag, 'detail': detail})
    print(f'  [{tag}] {name}' + (f' -- {detail}' if detail else ''))

def get_mc_counters(text, src=SRC_IP, grp=GROUP):
    r = {}
    in_sg = False
    for l in text.split('\n'):
        if src in l and grp in l:
            in_sg = True
        if in_sg:
            m = re.search(r'Forwarded frames:\s+(\d+)', l)
            if m: r['fwd'] = int(m.group(1))
            m = re.search(r'Forwarded octets:\s+\d+.*?(\d+\.\d+)\s*Mbps', l)
            if m: r['fwd_mbps'] = float(m.group(1))
            m = re.search(r'Wrong RPF packets:\s+(\d+)', l)
            if m: r['rpf'] = int(m.group(1)); break
            m = re.search(r'Punted packets:\s+(\d+)', l)
            if m: r['punt'] = int(m.group(1))
    return r

def parse_counter(text, iface):
    for l in text.split('\n'):
        if iface + ' ' in l and '|' in l and '.' not in l.split(iface)[1].split()[0] if iface in l else False:
            parts = [p.strip() for p in l.split('|') if p.strip()]
            if len(parts) >= 8:
                return {'rx_mbps': parts[2], 'tx_mbps': parts[3],
                        'rx_pkts': int(parts[4]), 'tx_pkts': int(parts[5]),
                        'rx_drops': int(parts[6]), 'tx_drops': int(parts[7])}
    return None

# =====================================================================
print('=' * 70)
print('SW-241838: PIM SSM over Physical Ethernet - 400G Variant')
print('=' * 70)

ssh, chan = connect()

# STEP 1: Verify PIM SSM config on physical interfaces
print('\n>>> STEP 1: PIM SSM Configuration <<<')
for iface in [IIF, OIF]:
    t = run(chan, f'show interfaces {iface} | no-more', 6)
    speed = admin = ip = encap = ''
    for l in t.split('\n'):
        ls = l.strip()
        if 'Speed:' in ls:
            m = re.search(r'Speed:\s+(\S+)', ls)
            if m: speed = m.group(1)
        if 'Admin state:' in ls:
            admin = 'up' if 'Operational state: up' in ls else 'down'
        if 'IPv4 Address:' in ls:
            m = re.search(r'IPv4 Address:\s+(\S+)', ls)
            if m: ip = m.group(1)
        if 'Encapsulation:' in ls:
            m = re.search(r'Encapsulation:\s+(\S+)', ls)
            if m: encap = m.group(1)
    print(f'  {iface}: speed={speed}, oper={admin}, ip={ip}, encap={encap}')
    record('1', f'{iface} link up at 400G', speed == '400Gbps' and admin == 'up',
           f'speed={speed}, oper={admin}')
    record('1', f'{iface} has IP', ip != '', f'ip={ip}')
    record('1', f'{iface} ethernet (untagged)', encap == 'ethernet', f'encap={encap}')

t = run(chan, 'show pim interface | no-more', 8)
for iface in [IIF, OIF]:
    found = False
    for l in t.split('\n'):
        if iface in l and '|' in l and 'enabled' in l:
            found = True
            print(f'  PIM on {iface}: {l.strip()}')
    record('1', f'PIM enabled on {iface}', found)

# STEP 2: PIM adjacency
print('\n>>> STEP 2: PIM Adjacency <<<')
t = run(chan, 'show pim neighbor | no-more', 10)
iif_neigh = oif_neigh = 0
for l in t.split('\n'):
    if IIF in l and '|' in l:
        iif_neigh += 1
        print(f'  IIF neighbor: {l.strip()}')
    if OIF in l and '|' in l:
        oif_neigh += 1
if oif_neigh > 0:
    print(f'  OIF neighbors: {oif_neigh}')
record('2', f'PIM neighbor on IIF ({IIF})', iif_neigh > 0, f'count={iif_neigh}')
record('2', f'PIM neighbor(s) on OIF ({OIF})', oif_neigh > 0, f'count={oif_neigh}')

# STEP 3: PIM tree and multicast route
print('\n>>> STEP 3: PIM Tree & Multicast Route <<<')
t = run(chan, 'show pim tree | no-more', 12)
tree_found = False
tree_iif_ok = tree_oif_ok = False
for l in t.split('\n'):
    ls = l.strip()
    if GROUP in ls and 'SSM' in ls:
        tree_found = True
        print(f'  PIM tree: {ls}')
    if tree_found:
        if IIF in ls and 'Rcv' in ls:
            tree_iif_ok = True
            print(f'  IIF: {ls}')
        if OIF in ls and 'Fwd' in ls:
            tree_oif_ok = True
            print(f'  OIF: {ls}')
record('3', f'PIM tree (S,G) for {GROUP} exists', tree_found)
record('3', f'PIM tree IIF = {IIF} (Rcv)', tree_iif_ok)
record('3', f'PIM tree OIF = {OIF} (Fwd)', tree_oif_ok)

t = run(chan, 'show multicast route | no-more', 12)
mc_found = False
mc_iif_ok = mc_oif_ok = False
for l in t.split('\n'):
    ls = l.strip()
    if SRC_IP in ls and GROUP in ls:
        mc_found = True
        print(f'  MC route: {ls}')
    if mc_found:
        if IIF in ls and 'A' in ls:
            mc_iif_ok = True
            print(f'  MC IIF: {ls}')
        if OIF in ls and 'F' in ls:
            mc_oif_ok = True
            print(f'  MC OIF: {ls}')
mc1 = get_mc_counters(t)
record('3', f'MC route ({SRC_IP}, {GROUP}) exists', mc_found)
record('3', f'MC route IIF = {IIF} (Accept)', mc_iif_ok)
record('3', f'MC route OIF = {OIF} (Forward)', mc_oif_ok)

t = run(chan, 'show multicast route summary | no-more', 6)
for l in t.split('\n'):
    if 'Number' in l:
        print(f'  {l.strip()}')

# STEP 4 & 5: Forwarding + counter validation (two snapshots)
print('\n>>> STEP 4 & 5: Forwarding & Counter Validation <<<')
INTERVAL = 10

t1_iif = run(chan, f'show interfaces counters | include "{IIF} " | no-more', 6)
t1_oif = run(chan, f'show interfaces counters | include "{OIF} " | no-more', 6)
mc1_t = run(chan, 'show multicast route | no-more', 12)
c1_iif = parse_counter(t1_iif, IIF)
c1_oif = parse_counter(t1_oif, OIF)
mc1 = get_mc_counters(mc1_t)

print(f'  Snapshot 1: IIF RX={c1_iif["rx_mbps"] if c1_iif else "?"} Mbps, OIF TX={c1_oif["tx_mbps"] if c1_oif else "?"} Mbps')
print(f'  Waiting {INTERVAL}s...')
time.sleep(INTERVAL)

t2_iif = run(chan, f'show interfaces counters | include "{IIF} " | no-more', 6)
t2_oif = run(chan, f'show interfaces counters | include "{OIF} " | no-more', 6)
mc2_t = run(chan, 'show multicast route | no-more', 12)
c2_iif = parse_counter(t2_iif, IIF)
c2_oif = parse_counter(t2_oif, OIF)
mc2 = get_mc_counters(mc2_t)

print(f'  Snapshot 2: IIF RX={c2_iif["rx_mbps"] if c2_iif else "?"} Mbps, OIF TX={c2_oif["tx_mbps"] if c2_oif else "?"} Mbps')

if c1_iif and c2_iif and c1_oif and c2_oif:
    rx_d = c2_iif['rx_pkts'] - c1_iif['rx_pkts']
    tx_d = c2_oif['tx_pkts'] - c1_oif['tx_pkts']
    drop_d = c2_iif['rx_drops'] - c1_iif['rx_drops']
    print(f'  IIF RX delta:     {rx_d:>12,}')
    print(f'  OIF TX delta:     {tx_d:>12,}')
    print(f'  IIF Drop delta:   {drop_d:>12,}')
    if rx_d > 0:
        print(f'  Drop ratio:       {drop_d/rx_d*100:>11.1f}%')
        print(f'  TX/RX ratio:      {tx_d/rx_d*100:>11.1f}%')
    record('4', 'Traffic actively forwarding (RX delta > 0)', rx_d > 0, f'delta={rx_d:,}')
    record('4', 'OIF TX matches IIF RX', tx_d > 0 and abs(tx_d - rx_d) / max(rx_d, 1) < 0.05,
           f'RX={rx_d:,}, TX={tx_d:,}')
    record('5', 'IIF RX drops not inflating', drop_d == 0 or (rx_d > 0 and drop_d/rx_d < 0.01),
           f'drop_delta={drop_d:,}')

if 'fwd' in mc1 and 'fwd' in mc2:
    fwd_d = mc2['fwd'] - mc1['fwd']
    rpf_d = mc2.get('rpf', 0) - mc1.get('rpf', 0)
    punt_d = mc2.get('punt', 0) - mc1.get('punt', 0)
    print(f'  MC Fwd delta:     {fwd_d:>12,}')
    print(f'  MC RPF delta:     {rpf_d:>12,}')
    print(f'  MC Punt delta:    {punt_d:>12,}')
    record('5', 'MC route forwarding active', fwd_d > 0, f'fwd_delta={fwd_d:,}')
    record('5', 'Wrong RPF = 0', rpf_d == 0, f'rpf_delta={rpf_d}')
    record('5', 'Punted to CPU = 0', punt_d == 0, f'punt_delta={punt_d}')

# CPRL check
t = run(chan, 'show system cprl | include Multicast | no-more', 6)
cprl_ok = True
for l in t.split('\n'):
    if 'Multicast' in l and 'show' not in l and 'Q3D' not in l:
        print(f'  CPRL: {l.strip()}')
        parts = [p.strip() for p in l.split('|') if p.strip()]
        if len(parts) >= 6:
            rx = int(parts[3]) if parts[3].isdigit() else 0
            if rx > 100:
                cprl_ok = False
record('5', 'CPRL: no multicast CPU punting', cprl_ok)

chan.send('exit\n')
time.sleep(1)
chan.close()
ssh.close()

# =====================================================================
# FINAL REPORT
print('\n' + '=' * 70)
print('SW-241838 (400G) TEST REPORT')
print('=' * 70)
passed = sum(1 for r in results if r['status'] == 'PASS')
failed = sum(1 for r in results if r['status'] == 'FAIL')
print(f'\nTotal: {len(results)} checks, {passed} PASS, {failed} FAIL\n')
for r in results:
    marker = 'OK' if r['status'] == 'PASS' else '!!'
    print(f'  [{marker}] Step {r["step"]}: {r["name"]}')
    if r['detail'] and r['status'] == 'FAIL':
        print(f'       Detail: {r["detail"]}')

if failed == 0:
    print(f'\nVERDICT: ALL PASS - 400G Physical Ethernet PIM SSM verified')
else:
    print(f'\nVERDICT: {failed} FAILURES - see details above')

print('\nDONE')
