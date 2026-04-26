#!/usr/bin/env python3
"""SW-244113 follow-up: Negative flows N1-N3 + physical-ge variant V1."""
import paramiko, time, re, json, os
from datetime import datetime
from stcrestclient import stchttp

HOST = "100.64.8.59"
USER = "dnroot"
PASS = "dnroot"

LABSERVER = "il-auto-containers"
CHASSIS_IP = "100.64.15.236"
SLOT = 1
PORT = 25
SESSION_NAME = "sw244113_negatives"
DUT_MAC = "e8:c5:7a:d6:30:18"
SRC_MAC = "00:10:94:01:19:01"

GE_SUB = "ge400-0/0/3.100"
BUN_SUB = "bundle-10.100"
PHYS = "ge400-0/0/18"
VRF = "urpf-vrf"
PHYS_VRF = "urpf-vrf-phys"

OUT = "/home/dn/output/sw244113_retest"
os.makedirs(OUT, exist_ok=True)

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
    time.sleep(6)
    chan.recv(65535)
    return ssh, chan


def run(chan, cmd, wait=7):
    chan.send(cmd + "\n")
    time.sleep(wait)
    out = b""
    while chan.recv_ready():
        out += chan.recv(65535)
        time.sleep(0.3)
    return clean(out.decode(errors="replace"))


def rp(chan, cmd, wait=7, silent=False):
    out = run(chan, cmd, wait)
    if not silent:
        print(f"  [{cmd}]")
        for line in out.split("\n"):
            print(f"    {line}")
    return out


def extract_counter(text, label):
    for line in text.split("\n"):
        if label in line:
            val = line.split(":")[-1].strip().split("(")[0].strip().replace(",", "")
            try:
                return int(val)
            except ValueError:
                return 0
    return 0


def get_counters(chan, iface):
    out = run(chan, f"show interfaces counters {iface} | no-more", 8)
    return {
        "raw": out,
        "rx": extract_counter(out, "RX packets:"),
        "tx": extract_counter(out, "TX packets:"),
        "v4_drops": extract_counter(out, "uRPF Ipv4 drops:"),
        "v6_drops": extract_counter(out, "uRPF Ipv6 drops:"),
    }


def commit_config(chan, cmds):
    rp(chan, "configure", 3, silent=True)
    rp(chan, "rollback", 4, silent=True)
    rp(chan, "configure", 3, silent=True)
    for c in cmds:
        rp(chan, c, 3)
    co = rp(chan, "commit", 15)
    rp(chan, "end", 3, silent=True)
    ok = "ERROR" not in co and ("succeed" in co.lower() or "no configuration changes" in co.lower())
    return ok, co


# --------- Spirent -----------
def spirent_connect():
    print(f"[spirent] connect to {LABSERVER}")
    stc = stchttp.StcHttp(LABSERVER, port=80)
    for s in stc.sessions():
        if SESSION_NAME in s:
            try:
                stc.join_session(s)
                stc.end_session(s)
            except Exception as e:
                print(f"[spirent] warn {e}")
    sid = stc.new_session("dn", SESSION_NAME)
    stc.join_session(sid)
    project = stc.get("system1", "children-project")
    port1 = stc.create("port", under=project)
    stc.config(port1, {"location": f"//{CHASSIS_IP}/{SLOT}/{PORT}"})
    stc.perform("AttachPorts", params={"RevokeOwner": "true"})
    stc.apply()
    print(f"[spirent] port online: {stc.get(port1, 'Online')}")
    return stc, sid, project, port1


def clear_streams(stc, port):
    sbs = stc.get(port, "children-streamblock")
    if sbs:
        for sb in sbs.split():
            stc.delete(sb)
    stc.apply()


def create_v4_stream(stc, port, name, src_ip, dst_ip, vlan_id, rate_fps=1000):
    sb = stc.create("streamBlock", under=port)
    stc.config(sb, {"Name": name, "FixedFrameLength": "128",
                    "LoadUnit": "FRAMES_PER_SECOND", "Load": str(rate_fps)})
    stc.apply()
    eth = stc.get(sb, "children-ethernet:EthernetII").split()[0]
    stc.config(eth, {"srcMac": SRC_MAC, "dstMac": DUT_MAC})
    vlans_container = stc.get(eth, "children-vlans").split()[0]
    vlan = stc.create("Vlan", under=vlans_container)
    stc.config(vlan, {"id": str(vlan_id)})
    ipv4 = stc.get(sb, "children-ipv4:IPv4").split()[0]
    stc.config(ipv4, {"sourceAddr": src_ip, "destAddr": dst_ip, "ttl": "64"})
    stc.apply()
    return sb


def start_traffic(stc, port):
    gen = stc.get(port, "children-generator")
    gen_cfg = stc.get(gen, "children-generatorconfig")
    stc.config(gen_cfg, {
        "SchedulingMode": "PORT_BASED",
        "DurationMode": "CONTINUOUS",
        "LoadUnit": "FRAMES_PER_SECOND",
        "FixedLoad": "1000",
    })
    stc.apply()
    stc.perform("GeneratorStart", params={"GeneratorList": gen})


def stop_traffic(stc, port):
    gen = stc.get(port, "children-generator")
    stc.perform("GeneratorStop", params={"GeneratorList": gen})
    time.sleep(2)


# --------- Restore baseline ---------
def restore_baseline(chan):
    """Make sure urpf-vrf has 10.100.10.0/24 specific route, both AFIs strict,
    allow-default disabled, no residual 10.100.99.0/24 route."""
    print("\n=== Restoring baseline for N1/N2 ===")
    cmds = [
        f"interfaces {GE_SUB} urpf address-family ipv4 mode strict",
        "top",
        f"interfaces {GE_SUB} urpf address-family ipv6 mode strict",
        "top",
        f"interfaces {GE_SUB} urpf address-family ipv4 allow-default disabled",
        "top",
        f"interfaces {GE_SUB} urpf address-family ipv6 allow-default disabled",
        "top",
        f"interfaces {BUN_SUB} urpf address-family ipv4 allow-default disabled",
        "top",
        f"interfaces {BUN_SUB} urpf address-family ipv6 allow-default disabled",
        "top",
        # Reinstall specific 10.100.10.0/24 route (may have been removed in step 11)
        f"network-services vrf instance {VRF} protocols static address-family ipv4-unicast",
        f"route 10.100.10.0/24 next-hop 10.100.1.2 interface {GE_SUB}",
        "top",
        # Remove 10.100.99.0/24 (added in step 10)
        f"no network-services vrf instance {VRF} protocols static address-family ipv4-unicast route 10.100.99.0/24",
        "top",
        # Remove 0.0.0.0/0 if exists (step 13 removed it but just in case)
    ]
    ok, co = commit_config(chan, cmds)
    print(f"[baseline commit] ok={ok}")
    # Verify state
    detail = run(chan, f"show interfaces detail {GE_SUB} | no-more", 8)
    routes = run(chan, f"show route vrf {VRF} table ipv4-unicast | no-more", 8)
    print(f"ge sub-if uRPF check lines:")
    for line in detail.split("\n"):
        if "uRPF" in line:
            print(f"  {line.strip()}")
    print(f"urpf-vrf v4 routes:")
    for line in routes.split("\n"):
        if "10.100" in line or "0.0.0.0" in line:
            print(f"  {line.strip()}")
    return {"baseline_commit": co[-500:], "detail": detail, "routes": routes}


# --------- N1 / N2: route withdraw/re-install during traffic ---------
def n1_n2(chan, stc, port):
    print("\n" + "="*70)
    print("N1: Withdraw reverse-path while valid traffic runs -> drops begin")
    print("N2: Re-install route while traffic runs -> drops stop")
    print("="*70)
    clear_streams(stc, port)
    create_v4_stream(stc, port, "n1_valid",
                     "10.100.10.100", "10.100.20.100", 100)
    c_baseline = get_counters(chan, GE_SUB)
    print(f"  baseline: rx={c_baseline['rx']:,}, v4drop={c_baseline['v4_drops']:,}")

    print("  Starting traffic...")
    start_traffic(stc, port)
    time.sleep(6)
    c_running = get_counters(chan, GE_SUB)
    print(f"  traffic running 6s: rx={c_running['rx']:,}, v4drop={c_running['v4_drops']:,}")
    run_delta_rx = c_running["rx"] - c_baseline["rx"]
    run_delta_drop = c_running["v4_drops"] - c_baseline["v4_drops"]
    print(f"    Δrx={run_delta_rx:,}  Δv4drop={run_delta_drop:,}  (should be >0 rx, 0 drops)")

    # N1: withdraw the reverse-path route
    print("\n  === N1: withdrawing 10.100.10.0/24 from urpf-vrf ===")
    rp(chan, "configure", 3, silent=True)
    rp(chan, f"no network-services vrf instance {VRF} protocols static address-family ipv4-unicast route 10.100.10.0/24", 3)
    co_rm = rp(chan, "commit", 10)
    rp(chan, "end", 3, silent=True)

    time.sleep(8)
    c_after_rm = get_counters(chan, GE_SUB)
    n1_delta_drop = c_after_rm["v4_drops"] - c_running["v4_drops"]
    n1_delta_rx = c_after_rm["rx"] - c_running["rx"]
    print(f"  after 8s (route gone): rx={c_after_rm['rx']:,}, v4drop={c_after_rm['v4_drops']:,}")
    print(f"    Δrx={n1_delta_rx:,}  Δv4drop={n1_delta_drop:,}  (drops should jump)")
    n1_pass = (n1_delta_drop > 0)

    # N2: reinstall the route
    print("\n  === N2: re-installing 10.100.10.0/24 in urpf-vrf ===")
    rp(chan, "configure", 3, silent=True)
    rp(chan, f"network-services vrf instance {VRF} protocols static address-family ipv4-unicast", 3)
    rp(chan, f"route 10.100.10.0/24 next-hop 10.100.1.2 interface {GE_SUB}", 3)
    co_add = rp(chan, "commit", 10)
    rp(chan, "end", 3, silent=True)

    time.sleep(4)
    c_during = get_counters(chan, GE_SUB)  # sample right after re-install
    time.sleep(6)
    c_after_add = get_counters(chan, GE_SUB)
    # Drop Δ between the two samples (after re-install) should be ~0
    n2_interval_drop = c_after_add["v4_drops"] - c_during["v4_drops"]
    n2_interval_rx = c_after_add["rx"] - c_during["rx"]
    print(f"  settle sample rx={c_during['rx']:,}, v4drop={c_during['v4_drops']:,}")
    print(f"  6s later:  rx={c_after_add['rx']:,}, v4drop={c_after_add['v4_drops']:,}")
    print(f"    Δrx={n2_interval_rx:,}  Δv4drop={n2_interval_drop:,}  (drops should stop)")
    n2_pass = (n2_interval_drop == 0 and n2_interval_rx > 0)

    stop_traffic(stc, port)
    clear_streams(stc, port)

    n1 = {
        "baseline": c_baseline["raw"],
        "running_pre_withdraw": c_running["raw"],
        "after_withdraw": c_after_rm["raw"],
        "pre_delta_rx": run_delta_rx, "pre_delta_drop": run_delta_drop,
        "n1_delta_rx": n1_delta_rx, "n1_delta_drop": n1_delta_drop,
        "result": "PASS" if n1_pass else "FAIL",
    }
    n2 = {
        "after_reinstall_sample1": c_during["raw"],
        "after_reinstall_sample2": c_after_add["raw"],
        "n2_interval_rx": n2_interval_rx, "n2_interval_drop": n2_interval_drop,
        "result": "PASS" if n2_pass else "FAIL",
    }
    print(f"\n>>> N1: {n1['result']}   >>> N2: {n2['result']}")
    return n1, n2


# --------- N3: inconsistent allow-default should fail commit ---------
def n3(chan):
    print("\n" + "="*70)
    print("N3: Different allow-default across two VRF interfaces -> commit error")
    print("="*70)
    rp(chan, "configure", 3, silent=True)
    rp(chan, "rollback", 3, silent=True)
    rp(chan, "configure", 3, silent=True)
    # ge sub-if: allow-default ENABLED (v4)
    rp(chan, f"interfaces {GE_SUB} urpf address-family ipv4 allow-default enabled", 3)
    rp(chan, "top", 3, silent=True)
    # bundle sub-if: allow-default DISABLED (v4)
    rp(chan, f"interfaces {BUN_SUB} urpf address-family ipv4 allow-default disabled", 3)
    rp(chan, "top", 3, silent=True)
    co = rp(chan, "commit", 15)
    rp(chan, "end", 3, silent=True)
    # Expect commit to fail
    rejected = any(kw in co.lower() for kw in ["error", "fail", "reject", "invalid", "consistency", "conflict"])
    print(f"\n  Commit output (last 800 chars):\n{co[-800:]}")
    # Rollback in case it did commit
    rp(chan, "configure", 3, silent=True)
    rp(chan, "rollback", 4, silent=True)
    rp(chan, f"interfaces {GE_SUB} urpf address-family ipv4 allow-default disabled", 3)
    rp(chan, "top", 3, silent=True)
    rp(chan, f"interfaces {BUN_SUB} urpf address-family ipv4 allow-default disabled", 3)
    rp(chan, "top", 3, silent=True)
    rp(chan, "commit", 15)
    rp(chan, "end", 3, silent=True)

    res = {"commit_out": co,
           "result": "PASS" if rejected else "FAIL",
           "behavior": "commit rejected (as expected)" if rejected else "commit unexpectedly accepted"}
    print(f"\n>>> N3: {res['result']} ({res['behavior']})")
    return res


# --------- V1: physical ge variant ---------
def v1(chan):
    print("\n" + "="*70)
    print(f"V1: Physical ge variant -- uRPF strict on {PHYS} in new VRF {PHYS_VRF}")
    print("="*70)
    cmds = [
        # First ensure PHYS isn't already in a VRF or has conflicting config
        f"interfaces {PHYS} admin-state enabled",
        "top",
        f"interfaces {PHYS} ipv4-address 10.200.1.1/24",
        "top",
        f"interfaces {PHYS} ipv6-address 2001:db8:300::1/64",
        "top",
        f"network-services vrf instance {PHYS_VRF}",
        f"interface {PHYS}",
        "top",
        f"interfaces {PHYS} urpf admin-state enabled",
        "top",
        f"interfaces {PHYS} urpf mode strict",
        "top",
        f"network-services vrf instance {PHYS_VRF} protocols static address-family ipv4-unicast",
        f"route 10.200.10.0/24 next-hop 10.200.1.2 interface {PHYS}",
        "top",
    ]
    ok, co = commit_config(chan, cmds)
    detail = rp(chan, f"show interfaces detail {PHYS} | no-more", 10)
    vrf_list = rp(chan, "show network-services vrf | no-more", 6)
    route = rp(chan, f"show route vrf {PHYS_VRF} table ipv4-unicast | no-more", 8)
    config = rp(chan, f"show config interfaces {PHYS} urpf | no-more", 6)
    cfg_ok = ("uRPF IPv4 check: enabled, Mode: strict" in detail
              and f"VRF ({PHYS_VRF})" in detail
              and PHYS_VRF in vrf_list
              and "10.200.10.0/24" in route)
    # Cleanup V1 config so device is tidy
    rp(chan, "configure", 3, silent=True)
    rp(chan, f"no interfaces {PHYS} urpf", 3)
    rp(chan, f"no interfaces {PHYS} ipv4-address", 3)
    rp(chan, f"no interfaces {PHYS} ipv6-address", 3)
    rp(chan, f"no network-services vrf instance {PHYS_VRF}", 3)
    rp(chan, "commit", 10)
    rp(chan, "end", 3, silent=True)
    res = {"commit_ok": ok, "commit_out": co[-500:], "detail": detail,
           "vrf_list": vrf_list, "route_table": route, "config": config,
           "result": "PASS" if (ok and cfg_ok) else "FAIL"}
    print(f"\n>>> V1: {res['result']}")
    return res


# --------- Main ---------
def main():
    ssh, chan = dut_connect()
    try:
        RESULTS["baseline"] = restore_baseline(chan)
        stc, sid, project, port1 = spirent_connect()
        try:
            n1, n2 = n1_n2(chan, stc, port1)
            RESULTS["N1"] = n1
            RESULTS["N2"] = n2
        finally:
            try:
                clear_streams(stc, port1)
                stc.end_session(sid)
                print("[spirent] session ended")
            except Exception as e:
                print(f"[spirent] cleanup warn: {e}")
        RESULTS["N3"] = n3(chan)
        RESULTS["V1"] = v1(chan)
    finally:
        chan.close()
        ssh.close()

    print("\n" + "="*70)
    print("SUMMARY")
    print("="*70)
    for key in ("N1", "N2", "N3", "V1"):
        r = RESULTS.get(key, {}).get("result", "N/A")
        print(f"  {key}: {r}")

    RESULTS["ended"] = datetime.utcnow().isoformat() + "Z"
    with open(f"{OUT}/negatives_results.json", "w") as f:
        json.dump(RESULTS, f, indent=2, default=str)
    print(f"\nSaved: {OUT}/negatives_results.json")


if __name__ == "__main__":
    main()
