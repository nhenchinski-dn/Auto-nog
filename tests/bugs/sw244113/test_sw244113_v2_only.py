#!/usr/bin/env python3
"""SW-244113 V2: BGP reverse-path variant only (baseline already in place)."""
import paramiko, time, re, json, os
from datetime import datetime
from stcrestclient import stchttp

HOST = "100.64.8.59"
USER, PASS = "dnroot", "dnroot"

LABSERVER = "il-auto-containers"
CHASSIS_IP = "100.64.15.236"
SLOT, PORT = 1, 25
SESSION_NAME = "sw244113_v2only"

DUT_MAC = "e8:c5:7a:d6:30:18"
SRC_MAC = "00:10:94:01:19:01"

GE_SUB = "ge400-0/0/3.250"
VRF = "urpf-vrf"
VLAN_ID = 250
DUT_AS = 65001
SPIRENT_AS = 65002
DUT_IP = "10.100.1.1"
SPIRENT_IP = "10.100.1.2"
BGP_PREFIX = "172.16.1.0"
BGP_PREFIX_LEN = 24
BGP_SRC = "172.16.1.100"
DST_IP = "10.100.20.100"
PHYS_VRF = "urpf-vrf-phys"
PHYS = "ge400-0/0/18"

OUT = "/home/dn/output/sw244113_retest"
RESULTS = {"started": datetime.utcnow().isoformat() + "Z"}


def clean(t):
    t = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", t)
    t = re.sub(r"\r", "", t)
    t = re.sub(r"-- More -- \(Press q to quit\)\s*", "", t)
    return t.strip()


def dut_connect():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASS, timeout=30,
                look_for_keys=False, allow_agent=False)
    chan = ssh.invoke_shell(width=300, height=5000)
    time.sleep(8); chan.recv(65535)
    return ssh, chan


def run(chan, cmd, wait=6):
    chan.send(cmd + "\n"); time.sleep(wait)
    o = b""
    while chan.recv_ready():
        o += chan.recv(65535); time.sleep(0.3)
    return clean(o.decode(errors="replace"))


def rp(chan, cmd, wait=6, silent=False):
    out = run(chan, cmd, wait)
    if not silent:
        print(f"  [{cmd}]")
        for line in out.split("\n"):
            print(f"    {line}")
    return out


def commit(chan, cmds):
    rp(chan, "end", 2, silent=True)
    rp(chan, "configure", 4, silent=True)
    rp(chan, "rollback", 4, silent=True)
    for c in cmds:
        rp(chan, c, 3)
    co = rp(chan, "commit", 25)
    rp(chan, "end", 4, silent=True)
    ok = "ERROR" not in co and ("succeed" in co.lower() or "not applicable" in co.lower())
    return ok, co


def extract(text, label):
    for line in text.split("\n"):
        if label in line:
            v = line.split(":")[-1].strip().split("(")[0].strip().replace(",", "")
            try:
                return int(v)
            except ValueError:
                return 0
    return 0


def counters(chan, iface):
    out = run(chan, f"show interfaces counters {iface} | no-more", 8)
    return {"raw": out,
            "rx": extract(out, "RX packets:"),
            "v4_drops": extract(out, "uRPF Ipv4 drops:")}


def stc_connect():
    print(f"[spirent] connect {LABSERVER}")
    stc = stchttp.StcHttp(LABSERVER, port=80)
    for s in stc.sessions():
        if SESSION_NAME in s:
            try:
                stc.join_session(s); stc.end_session(s)
            except Exception as e:
                print(f"  cleanup warn: {e}")
    sid = stc.new_session("dn", SESSION_NAME)
    stc.join_session(sid)
    project = stc.get("system1", "children-project")
    port1 = stc.create("port", under=project)
    stc.config(port1, {"location": f"//{CHASSIS_IP}/{SLOT}/{PORT}"})
    stc.perform("AttachPorts", params={"RevokeOwner": "true"})
    stc.apply()
    print(f"[spirent] online={stc.get(port1, 'Online')}")
    return stc, sid, project, port1


def build_bgp_device(stc, project, port):
    dev = stc.create("EmulatedDevice", under=project, **{
        "Name": "BGP_Peer", "EnablePingResponse": "TRUE", "RouterId": SPIRENT_IP})
    eth = stc.create("EthIIIf", under=dev, **{"SourceMac": SRC_MAC})
    vlan = stc.create("VlanIf", under=dev, **{"VlanId": str(VLAN_ID)})
    ip = stc.create("Ipv4If", under=dev, **{
        "Address": SPIRENT_IP, "Gateway": DUT_IP, "PrefixLength": "24"})
    stc.config(ip, **{"StackedOnEndpoint-targets": vlan})
    stc.config(vlan, **{"StackedOnEndpoint-targets": eth})
    stc.config(dev, **{"TopLevelIf-targets": ip, "PrimaryIf-targets": ip})
    stc.config(port, **{"AffiliationPort-sources": dev})

    bgp = stc.create("BgpRouterConfig", under=dev, **{
        "AsNum": str(SPIRENT_AS), "DutAsNum": str(DUT_AS),
        "IpVersion": "IPV4", "UseGatewayAsDut": "TRUE"})
    stc.config(bgp, **{"UsesIf-targets": ip})

    rt = stc.create("BgpIpv4RouteConfig", under=bgp, **{
        "NextHop": SPIRENT_IP, "AsPath": str(SPIRENT_AS)})
    blk = stc.get(rt, "children-Ipv4NetworkBlock").split()[0]
    stc.config(blk, {"StartIpList": BGP_PREFIX,
                     "PrefixLength": str(BGP_PREFIX_LEN),
                     "NetworkCount": "1"})
    stc.apply()
    return dev, bgp, rt


def clear_streams(stc, port):
    sbs = stc.get(port, "children-streamblock")
    if sbs:
        for sb in sbs.split():
            stc.delete(sb)
    stc.apply()


def create_stream(stc, port, name, src, dst, vlan_id):
    sb = stc.create("streamBlock", under=port)
    stc.config(sb, {"Name": name, "FixedFrameLength": "128",
                    "LoadUnit": "FRAMES_PER_SECOND", "Load": "1000"})
    stc.apply()
    eth = stc.get(sb, "children-ethernet:EthernetII").split()[0]
    stc.config(eth, {"srcMac": SRC_MAC, "dstMac": DUT_MAC})
    vc = stc.get(eth, "children-vlans").split()[0]
    v = stc.create("Vlan", under=vc)
    stc.config(v, {"id": str(vlan_id)})
    ipv4 = stc.get(sb, "children-ipv4:IPv4").split()[0]
    stc.config(ipv4, {"sourceAddr": src, "destAddr": dst, "ttl": "64"})
    stc.apply()
    return sb


def traffic(stc, port, seconds=10):
    gen = stc.get(port, "children-generator")
    gen_cfg = stc.get(gen, "children-generatorconfig")
    stc.config(gen_cfg, {"SchedulingMode": "PORT_BASED",
                         "DurationMode": "CONTINUOUS",
                         "LoadUnit": "FRAMES_PER_SECOND",
                         "FixedLoad": "1000"})
    stc.apply()
    stc.perform("GeneratorStart", params={"GeneratorList": gen})
    time.sleep(seconds)
    stc.perform("GeneratorStop", params={"GeneratorList": gen})
    time.sleep(3)


def cleanup_v1_leftover(chan):
    """Remove urpf-vrf-phys / ge400-0/0/18 config from V1 run."""
    print("\n=== Cleaning V1 leftover ===")
    cmds = [
        f"no interfaces {PHYS} urpf", "top",
        f"no interfaces {PHYS} ipv4-address", "top",
        f"no interfaces {PHYS} ipv6-address", "top",
        f"no network-services vrf instance {PHYS_VRF}", "top",
    ]
    ok, co = commit(chan, cmds)
    print(f"  V1 cleanup ok={ok}")


def v2(chan):
    print("\n" + "="*72)
    print(f"V2: BGP reverse-path variant — eBGP from Spirent into {VRF}")
    print("="*72)

    # Step 1: configure BGP on DUT in VRF
    print("\n[V2 step 1] configure BGP on DUT in urpf-vrf")
    bgp_cmds = [
        f"network-services vrf instance {VRF} protocols bgp {DUT_AS}",
        f"router-id {DUT_IP}",
        f"neighbor {SPIRENT_IP} remote-as {SPIRENT_AS}",
        f"neighbor {SPIRENT_IP} admin-state enabled",
        f"neighbor {SPIRENT_IP} address-family ipv4-unicast",
        "top",
    ]
    ok_bgp, co_bgp = commit(chan, bgp_cmds)
    print(f"  BGP config commit ok={ok_bgp}")

    # Step 2: build Spirent BGP peer
    print("\n[V2 step 2] build Spirent BGP emulated device")
    stc, sid, project, port = stc_connect()
    try:
        dev, bgp, rt = build_bgp_device(stc, project, port)
        stc.perform("ArpNdStartCommand", params={"HandleList": port})
        time.sleep(5)
        stc.perform("DeviceStartCommand", params={"DeviceList": dev})
        print("[V2] waiting for BGP session...")

        # Step 3: poll for session established
        session_up = False
        bgp_summary = ""
        for i in range(18):
            time.sleep(5)
            bgp_summary = run(chan, f"show bgp instance vrf {VRF} ipv4 unicast summary | no-more", 8)
            print(f"  poll #{i+1}:")
            for line in bgp_summary.split("\n")[-10:]:
                print(f"    {line}")
            for line in bgp_summary.split("\n"):
                if SPIRENT_IP in line:
                    parts = line.split()
                    if parts and parts[-1].isdigit():
                        session_up = True
                        break
            if session_up:
                break
        print(f"[V2] session_up={session_up}")

        # Step 4: verify route installed
        route_out = run(chan, f"show route vrf {VRF} table ipv4-unicast | no-more", 8)
        print(f"\n[V2 step 4] route table after BGP up:")
        for line in route_out.split("\n"):
            print(f"    {line}")
        route_installed = f"{BGP_PREFIX}/{BGP_PREFIX_LEN}" in route_out

        # Step 5: valid-source traffic (BGP-learned reverse path) → forward
        print("\n[V2 step 5] send traffic from BGP-learned prefix (valid reverse path)")
        clear_streams(stc, port)
        create_stream(stc, port, "v2_valid_bgp", BGP_SRC, DST_IP, VLAN_ID)
        c0 = counters(chan, GE_SUB)
        traffic(stc, port, 10)
        c1 = counters(chan, GE_SUB)
        d_rx = c1["rx"] - c0["rx"]
        d_drop = c1["v4_drops"] - c0["v4_drops"]
        print(f"  valid-source: Δrx={d_rx:,} Δv4drop={d_drop:,}  (expect rx>0, drop=0)")
        v2_valid_pass = (d_rx > 0 and d_drop == 0)

        # Step 6: stop BGP session on Spirent → route withdrawn → drops
        print("\n[V2 step 6] stop Spirent BGP (withdraw route) → uRPF should drop same traffic")
        stc.perform("DeviceStopCommand", params={"DeviceList": dev})
        time.sleep(12)
        route_out2 = run(chan, f"show route vrf {VRF} table ipv4-unicast | no-more", 8)
        print(f"  route table after BGP stop:")
        for line in route_out2.split("\n"):
            print(f"    {line}")
        route_withdrawn = f"{BGP_PREFIX}/{BGP_PREFIX_LEN}" not in route_out2

        c2 = counters(chan, GE_SUB)
        traffic(stc, port, 10)
        c3 = counters(chan, GE_SUB)
        d_rx2 = c3["rx"] - c2["rx"]
        d_drop2 = c3["v4_drops"] - c2["v4_drops"]
        print(f"  after BGP down: Δrx={d_rx2:,} Δv4drop={d_drop2:,} (expect drop>0)")
        v2_drop_pass = (d_drop2 > 0)

        clear_streams(stc, port)
        try:
            stc.delete(dev)
            stc.apply()
        except Exception as e:
            print(f"  spirent cleanup warn: {e}")
    finally:
        try:
            stc.end_session(sid)
        except Exception as e:
            print(f"  stc end warn: {e}")

    # Step 7: remove BGP config from DUT
    print("\n[V2 cleanup] removing DUT BGP config")
    commit(chan, [f"no network-services vrf instance {VRF} protocols bgp {DUT_AS}", "top"])

    v2_pass = ok_bgp and session_up and route_installed and v2_valid_pass and v2_drop_pass
    res = {
        "bgp_commit_ok": ok_bgp, "bgp_commit_out": co_bgp[-400:],
        "bgp_session_up": session_up, "bgp_summary": bgp_summary,
        "route_installed": route_installed, "route_out": route_out,
        "valid_traffic_delta_rx": d_rx, "valid_traffic_delta_drops": d_drop,
        "valid_traffic_pass": v2_valid_pass,
        "route_out_after_bgp_stop": route_out2,
        "route_withdrawn": route_withdrawn,
        "drop_traffic_delta_rx": d_rx2, "drop_traffic_delta_drops": d_drop2,
        "drop_traffic_pass": v2_drop_pass,
        "result": "PASS" if v2_pass else "FAIL",
    }
    print(f"\n>>> V2: {res['result']} (session={session_up} route={route_installed} valid={v2_valid_pass} drop={v2_drop_pass})")
    return res


def main():
    ssh, chan = dut_connect()
    try:
        cleanup_v1_leftover(chan)
        RESULTS["V2"] = v2(chan)
    finally:
        chan.close(); ssh.close()

    with open(f"{OUT}/v2_results.json", "w") as f:
        json.dump(RESULTS, f, indent=2, default=str)
    print(f"\nSaved: {OUT}/v2_results.json")


if __name__ == "__main__":
    main()
