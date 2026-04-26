#!/usr/bin/env python3
"""V2 debug: start Spirent BGP device and verify ARP/BGP come up."""
import paramiko, time, re, json
from stcrestclient import stchttp

HOST = "100.64.8.59"
LABSERVER = "il-auto-containers"
CHASSIS_IP = "100.64.15.236"
SLOT, PORT = 1, 25
SESSION_NAME = "sw244113_v2_dbg"
DUT_MAC = "e8:c5:7a:d6:31:08"
SRC_MAC = "00:10:94:01:19:01"
VLAN_ID = 250
DUT_AS = 65001
SPIRENT_AS = 65002
DUT_IP = "10.100.1.1"
SPIRENT_IP = "10.100.1.2"
BGP_PREFIX = "172.16.1.0"
GE_SUB = "ge400-0/0/33.250"
VRF = "urpf-vrf"


def clean(t):
    t = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", t)
    t = re.sub(r"\r", "", t)
    return t


def dut_cmd(chan, cmd, wait=5):
    chan.send(cmd + "\n"); time.sleep(wait)
    o = b""
    while chan.recv_ready(): o += chan.recv(65535); time.sleep(0.3)
    return clean(o.decode(errors="replace"))


ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username="dnroot", password="dnroot", timeout=30,
            look_for_keys=False, allow_agent=False)
chan = ssh.invoke_shell(width=300, height=5000)
time.sleep(8); chan.recv(65535)

print("Initial ARP:")
print(dut_cmd(chan, f"show arp vrf {VRF} | no-more", 6))

print("\n=== Spirent: start new session with BGP device ===")
stc = stchttp.StcHttp(LABSERVER, port=80)
for s in stc.sessions():
    if SESSION_NAME in s:
        try: stc.join_session(s); stc.end_session(s)
        except: pass
sid = stc.new_session("dn", SESSION_NAME)
stc.join_session(sid)
project = stc.get("system1", "children-project")
port1 = stc.create("port", under=project)
stc.config(port1, {"location": f"//{CHASSIS_IP}/{SLOT}/{PORT}"})
stc.perform("AttachPorts", params={"RevokeOwner": "true"})
stc.apply()
print(f"port online: {stc.get(port1, 'Online')}")

dev = stc.create("EmulatedDevice", under=project, **{
    "Name": "BGP_Peer", "EnablePingResponse": "TRUE", "RouterId": SPIRENT_IP})
eth = stc.create("EthIIIf", under=dev, **{"SourceMac": SRC_MAC})
vlan = stc.create("VlanIf", under=dev, **{"VlanId": str(VLAN_ID)})
ip = stc.create("Ipv4If", under=dev, **{
    "Address": SPIRENT_IP, "Gateway": DUT_IP, "PrefixLength": "24"})
stc.config(ip, **{"StackedOnEndpoint-targets": vlan})
stc.config(vlan, **{"StackedOnEndpoint-targets": eth})
stc.config(dev, **{"TopLevelIf-targets": ip, "PrimaryIf-targets": ip})
stc.config(port1, **{"AffiliationPort-sources": dev})

bgp = stc.create("BgpRouterConfig", under=dev, **{
    "AsNum": str(SPIRENT_AS), "DutAsNum": str(DUT_AS),
    "IpVersion": "IPV4", "UseGatewayAsDut": "TRUE"})
stc.config(bgp, **{"UsesIf-targets": ip})

rt = stc.create("BgpIpv4RouteConfig", under=bgp, **{
    "NextHop": SPIRENT_IP, "AsPath": str(SPIRENT_AS)})
blk = stc.get(rt, "children-Ipv4NetworkBlock").split()[0]
stc.config(blk, {"StartIpList": BGP_PREFIX, "PrefixLength": "24", "NetworkCount": "1"})
stc.apply()

print("\nDeviceStartCommand (starts device which handles ARP internally)...")
r = stc.perform("DeviceStartCommand", params={"DeviceList": dev})
print(f"  result: {r}")
time.sleep(10)

print(f"\nIpv4If addr resolve state: {stc.get(ip, 'AddrResolveState')}")

print("\nArpNdStartCommand on port (explicit)...")
r = stc.perform("ArpNdStartCommand", params={"HandleList": port1})
print(f"  result: {r}")
time.sleep(5)

print("\nDUT ARP after Device+ArpNd Start:")
print(dut_cmd(chan, f"show arp vrf {VRF} | no-more", 6))

print("\nge33.250 counters:")
print(dut_cmd(chan, f"show interfaces counters {GE_SUB} | no-more", 6))

for i in range(15):
    time.sleep(6)
    summary = dut_cmd(chan, f"show bgp summary | no-more", 6)
    # Extract the line for 10.100.1.2
    for line in summary.split("\n"):
        if SPIRENT_IP in line:
            print(f"  poll #{i+1}: {line.strip()}")
            break

print("\nFinal BGP neighbors:")
print(dut_cmd(chan, f"show bgp neighbors {SPIRENT_IP} | no-more", 8))

print("\nFinal route table:")
print(dut_cmd(chan, f"show route vrf {VRF} table ipv4-unicast | no-more", 8))

# Clean Spirent
try:
    stc.delete(dev); stc.apply()
    stc.end_session(sid)
except Exception as e:
    print(f"cleanup warn: {e}")

chan.close(); ssh.close()
