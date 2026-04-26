#!/usr/bin/env python3
"""SW-244113: Fix Step 11 — explicitly set per-AFI allow-default enabled."""

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

def rp(chan, cmd, wait=8):
    output = dut_run(chan, cmd, wait)
    print(f"  [{cmd}]")
    for line in output.split('\n'):
        print(f"    {line}")
    return output

def dut_extract(text, label):
    for line in text.split('\n'):
        if label in line:
            val = line.split(':')[-1].strip().split('(')[0].strip()
            return int(val) if val.isdigit() else 0
    return 0

def main():
    print("Connecting to DUT...")
    ssh, chan = dut_connect()

    # First check current config state
    print("\n--- Current uRPF config ---")
    rp(chan, "show config interfaces ge400-0/0/3.100 urpf | no-more", 10)
    rp(chan, "show interfaces detail ge400-0/0/3.100 | no-more", 12)

    # Set allow-default enabled at BOTH global and per-AFI levels
    print("\n--- Setting allow-default enabled at global + per-AFI levels ---")
    rp(chan, "configure", 5)
    rp(chan, "interfaces ge400-0/0/3.100 urpf allow-default enabled", 5)
    rp(chan, "top", 3)
    rp(chan, "interfaces ge400-0/0/3.100 urpf address-family ipv4 allow-default enabled", 5)
    rp(chan, "top", 3)
    rp(chan, "interfaces ge400-0/0/3.100 urpf address-family ipv6 allow-default enabled", 5)
    rp(chan, "top", 3)
    # Must be identical on all urpf interfaces
    rp(chan, "interfaces bundle-10.100 urpf allow-default enabled", 5)
    rp(chan, "top", 3)
    rp(chan, "interfaces bundle-10.100 urpf address-family ipv4 allow-default enabled", 5)
    rp(chan, "top", 3)
    rp(chan, "interfaces bundle-10.100 urpf address-family ipv6 allow-default enabled", 5)
    rp(chan, "top", 3)
    rp(chan, "interfaces ge400-0/0/5 urpf allow-default enabled", 5)
    rp(chan, "top", 3)
    commit_out = rp(chan, "commit", 15)
    rp(chan, "end", 3)

    # Verify
    print("\n--- Verify ---")
    rp(chan, "show config interfaces ge400-0/0/3.100 urpf | no-more", 10)
    rp(chan, "show interfaces detail ge400-0/0/3.100 | no-more", 12)

    # Now test with Spirent
    print("\n--- Running Spirent traffic for Step 11 ---")
    stc = stchttp.StcHttp(LABSERVER, port=80)
    for s in stc.sessions():
        if 'sw244113' in s:
            try: stc.join_session(s); stc.end_session(s)
            except: pass

    sid = stc.new_session('dn', 'sw244113_step11')
    stc.join_session(sid)
    project = stc.get('system1', 'children-project')
    port1 = stc.create('port', under=project)
    stc.config(port1, {'location': f'//{CHASSIS_IP}/1/25'})
    stc.perform('AttachPorts', params={'RevokeOwner': 'true'})
    stc.apply()
    print(f"  Port online: {stc.get(port1, 'Online')}")

    # Create stream
    sb = stc.create('streamBlock', under=port1)
    stc.config(sb, {'Name': 'Step11_allow_default', 'FixedFrameLength': '128',
                     'LoadUnit': 'FRAMES_PER_SECOND', 'Load': '1000'})
    stc.apply()
    eth = stc.get(sb, 'children-ethernet:EthernetII').split()[0]
    stc.config(eth, {'srcMac': SRC_MAC, 'dstMac': DUT_MAC})
    vlans_c = stc.get(eth, 'children-vlans').split()[0]
    vlan = stc.create('Vlan', under=vlans_c)
    stc.config(vlan, {'id': '100'})
    ipv4 = stc.get(sb, 'children-ipv4:IPv4').split()[0]
    stc.config(ipv4, {'sourceAddr': '10.100.10.100', 'destAddr': '10.100.2.100', 'ttl': '64'})
    stc.apply()

    # Configure generator
    gen = stc.get(port1, 'children-generator')
    gen_cfg = stc.get(gen, 'children-generatorconfig')
    stc.config(gen_cfg, {'SchedulingMode': 'PORT_BASED', 'DurationMode': 'CONTINUOUS',
                         'LoadUnit': 'FRAMES_PER_SECOND', 'FixedLoad': '1000'})
    stc.apply()

    # Counters before
    c_before = dut_run(chan, "show interfaces counters ge400-0/0/3.100 | no-more", 10)
    v4_before = dut_extract(c_before, "uRPF Ipv4 drops:")
    rx_before = dut_extract(c_before, "RX packets:")

    print("  Starting traffic...")
    stc.perform('GeneratorStart', params={'GeneratorList': gen})
    time.sleep(15)
    stc.perform('GeneratorStop', params={'GeneratorList': gen})
    print("  Stopped.")
    time.sleep(3)

    c_after = dut_run(chan, "show interfaces counters ge400-0/0/3.100 | no-more", 10)
    v4_after = dut_extract(c_after, "uRPF Ipv4 drops:")
    rx_after = dut_extract(c_after, "RX packets:")

    rx_delta = rx_after - rx_before
    v4_delta = v4_after - v4_before

    print(f"\n  RX delta:          {rx_delta:,}")
    print(f"  uRPF v4 drops Δ:  {v4_delta:,}")

    if v4_delta == 0 and rx_delta > 0:
        print("  >>> STEP 11: PASS — allow-default enabled, zero drops")
    elif v4_delta > 0:
        print(f"  >>> STEP 11: FAIL — still getting {v4_delta} uRPF drops")
        print("  (Investigating: allow-default may not work with per-AFI overrides active)")
    else:
        print("  >>> STEP 11: CHECK — no RX delta")

    stc.end_session(sid)
    chan.close(); ssh.close()

if __name__ == "__main__":
    main()
