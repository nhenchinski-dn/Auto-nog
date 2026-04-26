#!/usr/bin/env python3
"""V2 retry: wait for BGP bestpath delay to expire before measuring valid-source."""
import paramiko, time, re, json
from datetime import datetime
from stcrestclient import stchttp

HOST = "100.64.8.59"
USER, PASS = "dnroot", "dnroot"
LABSERVER = "il-auto-containers"
CHASSIS_IP = "100.64.15.236"
SLOT, PORT = 1, 25
SESSION_NAME = "sw244113_v2retry"

DUT_MAC = "e8:c5:7a:d6:30:18"
SRC_MAC = "00:10:94:01:19:01"
GE_SUB = "ge400-0/0/3.250"
VRF = "urpf-vrf"
VLAN_ID = 250
DUT_AS, SPIRENT_AS = 65001, 65002
DUT_IP, SPIRENT_IP = "10.100.1.1", "10.100.1.2"
BGP_PREFIX, BGP_PREFIX_LEN = "172.16.1.0", 24
BGP_SRC, DST_IP = "172.16.1.100", "10.100.20.100"

OUT = "/home/dn/output/sw244113_retest"


def clean(t):
    t = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", t)
    t = re.sub(r"\r", "", t)
    return t.strip()


def run(chan, cmd, wait=6):
    chan.send(cmd + "\n"); time.sleep(wait)
    o = b""
    while chan.recv_ready():
        o += chan.recv(65535); time.sleep(0.3)
    return clean(o.decode(errors="replace"))


def rp(chan, cmd, wait=6, silent=False):
    out = run(chan, cmd, wait)
    if not silent:
        print(f"  [{cmd[:70]}]")
        for line in out.split("\n")[-15:]:
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
            try: return int(v)
            except: return 0
    return 0


def counters(chan, iface):
    out = run(chan, f"show interfaces counters {iface} | no-more", 8)
    return {"rx": extract(out, "RX packets:"),
            "v4_drops": extract(out, "uRPF Ipv4 drops:")}


def dut_connect():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASS, timeout=30, look_for_keys=False, allow_agent=False)
    chan = ssh.invoke_shell(width=300, height=5000)
    time.sleep(8); chan.recv(65535)
    return ssh, chan


def stc_connect():
    stc = stchttp.StcHttp(LABSERVER, port=80)
    for s in stc.sessions():
        if SESSION_NAME in s:
            try: stc.join_session(s); stc.end_session(s)
            except: pass
    sid = stc.new_session("dn", SESSION_NAME)
    stc.join_session(sid)
    project = stc.get("system1", "children-project")
    p = stc.create("port", under=project)
    stc.config(p, {"location": f"//{CHASSIS_IP}/{SLOT}/{PORT}"})
    stc.perform("AttachPorts", params={"RevokeOwner": "true"})
    stc.apply()
    return stc, sid, project, p


def build_bgp(stc, project, port):
    dev = stc.create("EmulatedDevice", under=project, **{"Name": "BGP_Peer", "EnablePingResponse": "TRUE", "RouterId": SPIRENT_IP})
    eth = stc.create("EthIIIf", under=dev, **{"SourceMac": SRC_MAC})
    vlan = stc.create("VlanIf", under=dev, **{"VlanId": str(VLAN_ID)})
    ip = stc.create("Ipv4If", under=dev, **{"Address": SPIRENT_IP, "Gateway": DUT_IP, "PrefixLength": "24"})
    stc.config(ip, **{"StackedOnEndpoint-targets": vlan})
    stc.config(vlan, **{"StackedOnEndpoint-targets": eth})
    stc.config(dev, **{"TopLevelIf-targets": ip, "PrimaryIf-targets": ip})
    stc.config(port, **{"AffiliationPort-sources": dev})
    bgp = stc.create("BgpRouterConfig", under=dev, **{
        "AsNum": str(SPIRENT_AS), "DutAsNum": str(DUT_AS), "IpVersion": "IPV4", "UseGatewayAsDut": "TRUE"})
    stc.config(bgp, **{"UsesIf-targets": ip})
    rt = stc.create("BgpIpv4RouteConfig", under=bgp, **{"NextHop": SPIRENT_IP, "AsPath": str(SPIRENT_AS)})
    blk = stc.get(rt, "children-Ipv4NetworkBlock").split()[0]
    stc.config(blk, {"StartIpList": BGP_PREFIX, "PrefixLength": str(BGP_PREFIX_LEN), "NetworkCount": "1"})
    stc.apply()
    return dev, bgp, rt


def clear_streams(stc, port):
    sbs = stc.get(port, "children-streamblock")
    if sbs:
        for sb in sbs.split(): stc.delete(sb)
    stc.apply()


def create_stream(stc, port, name, src, dst):
    sb = stc.create("streamBlock", under=port)
    stc.config(sb, {"Name": name, "FixedFrameLength": "128",
                    "LoadUnit": "FRAMES_PER_SECOND", "Load": "1000"})
    stc.apply()
    eth = stc.get(sb, "children-ethernet:EthernetII").split()[0]
    stc.config(eth, {"srcMac": SRC_MAC, "dstMac": DUT_MAC})
    vc = stc.get(eth, "children-vlans").split()[0]
    v = stc.create("Vlan", under=vc)
    stc.config(v, {"id": str(VLAN_ID)})
    ipv4 = stc.get(sb, "children-ipv4:IPv4").split()[0]
    stc.config(ipv4, {"sourceAddr": src, "destAddr": dst, "ttl": "64"})
    stc.apply()
    return sb


def traffic(stc, port, seconds=10):
    gen = stc.get(port, "children-generator")
    gcfg = stc.get(gen, "children-generatorconfig")
    stc.config(gcfg, {"SchedulingMode": "PORT_BASED", "DurationMode": "CONTINUOUS",
                      "LoadUnit": "FRAMES_PER_SECOND", "FixedLoad": "1000"})
    stc.apply()
    stc.perform("GeneratorStart", params={"GeneratorList": gen})
    time.sleep(seconds)
    stc.perform("GeneratorStop", params={"GeneratorList": gen})
    time.sleep(3)


def main():
    print("\n" + "="*72)
    print("V2 RETRY: BGP reverse-path with bestpath-delay wait")
    print("="*72)
    _, chan = dut_connect()

    # 1. re-install DUT BGP config
    print("\n[V2 step 1] configure BGP on DUT in urpf-vrf")
    ok, _ = commit(chan, [
        f"network-services vrf instance {VRF} protocols bgp {DUT_AS}",
        f"router-id {DUT_IP}",
        f"neighbor {SPIRENT_IP} remote-as {SPIRENT_AS}",
        f"neighbor {SPIRENT_IP} admin-state enabled",
        f"neighbor {SPIRENT_IP} address-family ipv4-unicast",
        "top",
    ])
    print(f"  BGP cfg commit ok={ok}")

    # 2. Spirent
    print("\n[V2 step 2] build Spirent BGP peer")
    stc, sid, project, port = stc_connect()
    try:
        dev, bgp, rt = build_bgp(stc, project, port)
        stc.perform("ArpNdStartCommand", params={"HandleList": port})
        time.sleep(5)
        stc.perform("DeviceStartCommand", params={"DeviceList": dev})

        # 3. wait for session
        print("[V2 step 3] waiting for BGP session up...")
        session_up = False
        for i in range(18):
            time.sleep(5)
            out = run(chan, f"show bgp instance vrf {VRF} ipv4 unicast summary | no-more", 6)
            for line in out.split("\n"):
                if SPIRENT_IP in line and line.strip().split()[-1].isdigit():
                    session_up = True; break
            print(f"  poll {i+1}: session_up={session_up}")
            if session_up: break

        # 3b. wait for bestpath delay to clear
        print("\n[V2 step 3b] waiting for bestpath-delay to expire & route to enter FIB")
        for i in range(30):
            time.sleep(10)
            out = run(chan, f"show bgp instance vrf {VRF} ipv4 unicast summary | no-more", 6)
            m = re.search(r"Bestpath delay is on, remaining (\d+) seconds", out)
            remain = int(m.group(1)) if m else -1
            rt_out = run(chan, f"show route vrf {VRF} table ipv4-unicast | include \"B>\\*|172.16\" | no-more", 6)
            in_rib = BGP_PREFIX in rt_out
            print(f"  poll {i+1}: bestpath_remaining={remain}  route_in_rib={in_rib}")
            if remain <= 0 and in_rib:
                break

        # 4. route check
        print("\n[V2 step 4] route table")
        rt_out = run(chan, f"show route vrf {VRF} table ipv4-unicast | no-more", 6)
        print(rt_out)
        route_installed = f"{BGP_PREFIX}/{BGP_PREFIX_LEN}" in rt_out

        # 5. valid-source traffic
        print("\n[V2 step 5] traffic from BGP-learned prefix (valid reverse path)")
        clear_streams(stc, port)
        create_stream(stc, port, "v2_valid", BGP_SRC, DST_IP)
        c0 = counters(chan, GE_SUB)
        traffic(stc, port, 10)
        c1 = counters(chan, GE_SUB)
        d_rx, d_drop = c1["rx"] - c0["rx"], c1["v4_drops"] - c0["v4_drops"]
        print(f"  valid: Δrx={d_rx:,} Δdrop={d_drop:,}  (expect rx>0, drop≈0)")
        valid_pass = d_rx > 0 and d_drop < d_rx * 0.1

        # 6. stop BGP → drops
        print("\n[V2 step 6] stop BGP session (withdraw route)")
        stc.perform("DeviceStopCommand", params={"DeviceList": dev})
        time.sleep(15)
        rt_out2 = run(chan, f"show route vrf {VRF} table ipv4-unicast | no-more", 6)
        route_withdrawn = f"{BGP_PREFIX}/{BGP_PREFIX_LEN}" not in rt_out2
        c2 = counters(chan, GE_SUB)
        traffic(stc, port, 10)
        c3 = counters(chan, GE_SUB)
        d_rx2, d_drop2 = c3["rx"] - c2["rx"], c3["v4_drops"] - c2["v4_drops"]
        print(f"  after-down: Δrx={d_rx2:,} Δdrop={d_drop2:,}  (expect drop≈rx)")
        drop_pass = d_drop2 > d_rx2 * 0.9

        clear_streams(stc, port)
        try: stc.delete(dev); stc.apply()
        except: pass
    finally:
        try: stc.end_session(sid)
        except: pass

    # 7. cleanup DUT BGP
    commit(chan, [f"no network-services vrf instance {VRF} protocols bgp {DUT_AS}", "top"])

    verdict = ok and session_up and route_installed and valid_pass and drop_pass and route_withdrawn
    print("\n" + "="*72)
    print(f">>> V2 RETRY: {'PASS' if verdict else 'FAIL'}")
    print(f"    session={session_up} route_installed={route_installed} valid={valid_pass}")
    print(f"    drop_on_withdraw={drop_pass} route_withdrawn={route_withdrawn}")
    print(f"    valid: rx={d_rx:,} drop={d_drop:,}")
    print(f"    drop:  rx={d_rx2:,} drop={d_drop2:,}")

    with open(f"{OUT}/v2_retry_results.json", "w") as f:
        json.dump({
            "verdict": "PASS" if verdict else "FAIL",
            "session": session_up, "route_installed": route_installed,
            "valid_pass": valid_pass, "drop_pass": drop_pass, "route_withdrawn": route_withdrawn,
            "valid_rx": d_rx, "valid_drop": d_drop,
            "withdraw_rx": d_rx2, "withdraw_drop": d_drop2,
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }, f, indent=2)
    print(f"\nSaved: {OUT}/v2_retry_results.json")


if __name__ == "__main__":
    main()
