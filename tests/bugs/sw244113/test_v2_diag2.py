#!/usr/bin/env python3
"""V2 diag2: blast tagged broadcast, find which DUT port sees RX."""
import paramiko, time, re
from stcrestclient import stchttp

HOST = "100.64.8.59"
LABSERVER = "il-auto-containers"
CHASSIS_IP = "100.64.15.236"
SLOT, PORT = 1, 25
SESSION_NAME = "sw244113_v2_diag2"
SRC_MAC = "00:10:94:01:19:01"

def clean(t):
    t = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", t)
    t = re.sub(r"\r", "", t)
    return t

def dcmd(chan, cmd, w=5):
    chan.send(cmd + "\n"); time.sleep(w)
    o = b""
    while chan.recv_ready(): o += chan.recv(65535); time.sleep(0.3)
    return clean(o.decode(errors="replace"))

def get_rx(chan, iface):
    out = dcmd(chan, f"show interfaces counters {iface} | no-more", 4)
    for line in out.split("\n"):
        if "RX packets:" in line:
            v = line.split(":")[-1].strip().split("(")[0].strip().replace(",", "")
            try: return int(v)
            except: return 0
    return 0

# Connect DUT
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username="dnroot", password="dnroot", timeout=30,
            look_for_keys=False, allow_agent=False)
chan = ssh.invoke_shell(width=300, height=5000)
time.sleep(8); chan.recv(65535)

# Find all UP interfaces
print("=== All DUT interfaces in 'up' operational state ===")
out = dcmd(chan, "show interfaces | include up | no-more", 8)
up_ifs = []
for line in out.split("\n"):
    m = re.match(r"\|\s*(ge\S+|bundle\S+|mgmt\S+)\s*\|", line)
    if m:
        name = m.group(1)
        if "." not in name and "up" in line and "not-present" not in line:
            up_ifs.append(name)
print(f"  {up_ifs}")

# Baseline
baselines = {i: get_rx(chan, i) for i in up_ifs}
print(f"  baselines: {baselines}")

# Spirent blast (tagged VLAN 250 broadcast)
print("\n=== Spirent: blast VLAN 250 broadcast for 6s ===")
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
print(f"  port online: {stc.get(port, 'Online')}")

for (vid, name) in [(250, "v250"), (100, "v100")]:
    sb = stc.create("streamBlock", under=port)
    stc.config(sb, {"Name": name, "FixedFrameLength": "128",
                    "LoadUnit": "FRAMES_PER_SECOND", "Load": "3000"})
    stc.apply()
    eth = stc.get(sb, "children-ethernet:EthernetII").split()[0]
    stc.config(eth, {"srcMac": SRC_MAC, "dstMac": "ff:ff:ff:ff:ff:ff"})
    vc = stc.get(eth, "children-vlans").split()[0]
    v = stc.create("Vlan", under=vc)
    stc.config(v, {"id": str(vid)})
    ipv4 = stc.get(sb, "children-ipv4:IPv4").split()[0]
    stc.config(ipv4, {"sourceAddr": "10.100.1.2",
                      "destAddr": "255.255.255.255", "ttl": "64"})
    stc.apply()

gen = stc.get(port, "children-generator")
gen_cfg = stc.get(gen, "children-generatorconfig")
stc.config(gen_cfg, {"SchedulingMode": "PORT_BASED",
                     "DurationMode": "CONTINUOUS",
                     "LoadUnit": "FRAMES_PER_SECOND",
                     "FixedLoad": "3000"})
stc.apply()

stc.perform("GeneratorStart", params={"GeneratorList": gen})
time.sleep(6)
stc.perform("GeneratorStop", params={"GeneratorList": gen})
time.sleep(3)

# Check deltas
print("\n=== DUT RX deltas after blast ===")
for i in up_ifs:
    after = get_rx(chan, i)
    delta = after - baselines[i]
    marker = "  <--- RX!" if delta > 0 else ""
    print(f"  {i}: Δrx={delta:,}{marker}")

# Also try a sample of common ports that could be cabled
print("\n=== Also check common non-up ports ===")
for p in ["ge400-0/0/0", "ge400-0/0/1", "ge400-0/0/2", "ge400-0/0/3",
          "ge400-0/0/4", "ge400-0/0/5", "ge400-0/0/10", "ge400-0/0/15",
          "ge400-0/0/18", "ge400-0/0/20", "ge400-0/0/25",
          "ge400-0/0/30", "ge400-0/0/31", "ge400-0/0/32",
          "ge400-0/0/34", "ge400-0/0/35"]:
    try:
        after = get_rx(chan, p)
        if after > 0:
            print(f"  {p}: RX={after:,}")
    except Exception as e:
        pass

try:
    stc.end_session(sid)
except: pass
chan.close(); ssh.close()
