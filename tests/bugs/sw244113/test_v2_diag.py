#!/usr/bin/env python3
"""V2 diagnostic: is Spirent port 1/25 actually reaching ge400-0/0/33?
Test with a raw VLAN 250 stream first, before re-attempting BGP."""
import paramiko, time, re
from stcrestclient import stchttp

HOST = "100.64.8.59"
LABSERVER = "il-auto-containers"
CHASSIS_IP = "100.64.15.236"
SLOT, PORT = 1, 25
SESSION_NAME = "sw244113_v2_diag"
DUT_MAC = "e8:c5:7a:d6:31:08"  # ge400-0/0/33 HW MAC
SRC_MAC = "00:10:94:01:19:01"
VLAN_ID = 250
GE_SUB = "ge400-0/0/33.250"
GE_PHY = "ge400-0/0/33"
VRF = "urpf-vrf"


def clean(t):
    t = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", t)
    t = re.sub(r"\r", "", t)
    return t


def dut_cmd(chan, cmd, w=6):
    chan.send(cmd + "\n"); time.sleep(w)
    o = b""
    while chan.recv_ready(): o += chan.recv(65535); time.sleep(0.3)
    return clean(o.decode(errors="replace"))


def extract_rx_tx(text, iface_label=None):
    rx, tx = 0, 0
    for line in text.split("\n"):
        if "RX packets:" in line:
            v = line.split(":")[-1].strip().split("(")[0].strip().replace(",", "")
            try: rx = int(v)
            except: pass
        elif "TX packets:" in line:
            v = line.split(":")[-1].strip().split("(")[0].strip().replace(",", "")
            try: tx = int(v)
            except: pass
    return rx, tx


ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username="dnroot", password="dnroot", timeout=30,
            look_for_keys=False, allow_agent=False)
chan = ssh.invoke_shell(width=300, height=5000)
time.sleep(8); chan.recv(65535)

# 1. Check DUT baseline counters on physical + sub-if
print("=== DUT baseline counters (physical + sub-if) ===")
phy_out = dut_cmd(chan, f"show interfaces counters {GE_PHY} | no-more", 6)
sub_out = dut_cmd(chan, f"show interfaces counters {GE_SUB} | no-more", 6)
phy_rx0, phy_tx0 = extract_rx_tx(phy_out)
sub_rx0, sub_tx0 = extract_rx_tx(sub_out)
print(f"  {GE_PHY}: RX={phy_rx0:,}  TX={phy_tx0:,}")
print(f"  {GE_SUB}: RX={sub_rx0:,}  TX={sub_tx0:,}")

# 2. Connect Spirent and check port counters
print("\n=== Spirent connect ===")
stc = stchttp.StcHttp(LABSERVER, port=80)
for s in stc.sessions():
    if SESSION_NAME in s:
        try: stc.join_session(s); stc.end_session(s)
        except: pass
sid = stc.new_session("dn", SESSION_NAME)
stc.join_session(sid)
project = stc.get("system1", "children-project")
port = stc.create("port", under=project)
stc.config(port, {"location": f"//{CHASSIS_IP}/{SLOT}/{PORT}"})
stc.perform("AttachPorts", params={"RevokeOwner": "true"})
stc.apply()

# Port status
print(f"  Online: {stc.get(port, 'Online')}")
# Known hw info
try:
    print(f"  LinkStatus / Speed: {stc.get(port, 'ActivePhy')}")
except Exception as e:
    print(f"  (skip ActivePhy: {e})")
for attr in ["LocalActive", "HardwareName", "Speed", "PhyMode"]:
    try:
        print(f"  {attr}: {stc.get(port, attr)}")
    except Exception as e:
        pass

# 3. Send a raw VLAN 250 stream for 5s and check DUT RX
print("\n=== TEST A: raw VLAN 250 stream -> DUT RX? ===")
sb = stc.create("streamBlock", under=port)
stc.config(sb, {"Name": "v250_diag", "FixedFrameLength": "128",
                "LoadUnit": "FRAMES_PER_SECOND", "Load": "2000"})
stc.apply()
eth = stc.get(sb, "children-ethernet:EthernetII").split()[0]
stc.config(eth, {"srcMac": SRC_MAC, "dstMac": DUT_MAC})
vc = stc.get(eth, "children-vlans").split()[0]
v = stc.create("Vlan", under=vc)
stc.config(v, {"id": str(VLAN_ID)})
ipv4 = stc.get(sb, "children-ipv4:IPv4").split()[0]
stc.config(ipv4, {"sourceAddr": "10.100.1.2", "destAddr": "10.100.1.1", "ttl": "64"})
stc.apply()

gen = stc.get(port, "children-generator")
gen_cfg = stc.get(gen, "children-generatorconfig")
stc.config(gen_cfg, {"SchedulingMode": "PORT_BASED",
                     "DurationMode": "CONTINUOUS",
                     "LoadUnit": "FRAMES_PER_SECOND",
                     "FixedLoad": "2000"})
stc.apply()

print("  starting 5s VLAN 250 blast...")
stc.perform("GeneratorStart", params={"GeneratorList": gen})
time.sleep(5)
stc.perform("GeneratorStop", params={"GeneratorList": gen})
time.sleep(3)

# Check Spirent port counters
gen_results = stc.get(gen, "children-generatorportresults").split()[0]
print(f"  Spirent TX total frames: {stc.get(gen_results, 'TotalFrameCount')}")
anl_results = stc.get(port, "children-analyzerportresults").split()
print(f"  Spirent analyzer results: {anl_results}")
if anl_results:
    ar = anl_results[0]
    try:
        print(f"  Spirent RX total frames: {stc.get(ar, 'TotalFrameCount')}")
        print(f"  Spirent RX sig frames: {stc.get(ar, 'SigFrameCount')}")
    except Exception as e:
        print(f"  (rx read err: {e})")

phy_out = dut_cmd(chan, f"show interfaces counters {GE_PHY} | no-more", 6)
sub_out = dut_cmd(chan, f"show interfaces counters {GE_SUB} | no-more", 6)
phy_rx1, _ = extract_rx_tx(phy_out)
sub_rx1, _ = extract_rx_tx(sub_out)
print(f"  DUT {GE_PHY}: RX={phy_rx1:,}  Δ={phy_rx1 - phy_rx0:,}")
print(f"  DUT {GE_SUB}: RX={sub_rx1:,}  Δ={sub_rx1 - sub_rx0:,}")

# Clean stream for next test
stc.delete(sb); stc.apply()

# 4. Also try VLAN 100 (maybe cable is actually on a different port/VLAN)
print("\n=== TEST B: raw VLAN 100 stream -> which interface RXes? ===")
sb2 = stc.create("streamBlock", under=port)
stc.config(sb2, {"Name": "v100_diag", "FixedFrameLength": "128",
                 "LoadUnit": "FRAMES_PER_SECOND", "Load": "2000"})
stc.apply()
eth2 = stc.get(sb2, "children-ethernet:EthernetII").split()[0]
stc.config(eth2, {"srcMac": SRC_MAC, "dstMac": "ff:ff:ff:ff:ff:ff"})
vc2 = stc.get(eth2, "children-vlans").split()[0]
v100 = stc.create("Vlan", under=vc2)
stc.config(v100, {"id": "100"})
ipv4_2 = stc.get(sb2, "children-ipv4:IPv4").split()[0]
stc.config(ipv4_2, {"sourceAddr": "10.100.1.2", "destAddr": "10.100.1.255", "ttl": "64"})
stc.apply()

# Baselines across all breakout ports
print("\n  Baselines on breakout children:")
baselines = {}
for p in ["ge400-0/0/30", "ge400-0/0/31", "ge400-0/0/32", "ge400-0/0/33", "ge400-0/0/34"]:
    rx, tx = extract_rx_tx(dut_cmd(chan, f"show interfaces counters {p} | no-more", 5))
    baselines[p] = (rx, tx)
    print(f"    {p}: RX={rx:,}  TX={tx:,}")

print("  blasting VLAN 100 for 5s...")
stc.perform("GeneratorStart", params={"GeneratorList": gen})
time.sleep(5)
stc.perform("GeneratorStop", params={"GeneratorList": gen})
time.sleep(3)

print("\n  Deltas on breakout children:")
for p in ["ge400-0/0/30", "ge400-0/0/31", "ge400-0/0/32", "ge400-0/0/33", "ge400-0/0/34"]:
    rx, tx = extract_rx_tx(dut_cmd(chan, f"show interfaces counters {p} | no-more", 5))
    rx0, tx0 = baselines[p]
    print(f"    {p}: Δrx={rx - rx0:,}  Δtx={tx - tx0:,}")

try:
    stc.delete(sb2); stc.apply()
    stc.end_session(sid)
except Exception as e:
    print(f"  cleanup warn: {e}")

chan.close(); ssh.close()
