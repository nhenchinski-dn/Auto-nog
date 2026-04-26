#!/usr/bin/env python3
"""SW-244113 RETEST: Strict uRPF | Non-default VRF — full 13-step execution.

Device: NCP3-nog (wky1c7vd00008p2) @ 100.64.8.59
Build: DNOS 26.2.0 build 310_dev
Spirent: chassis 100.64.15.236 slot 1/port 25 -> ge400-0/0/3
"""

import paramiko
import time
import re
import json
import os
import sys
import traceback
from datetime import datetime
from stcrestclient import stchttp

# -----------------------------------------------------------------------------
# Globals
# -----------------------------------------------------------------------------
HOST = "100.64.8.59"
USER = "dnroot"
PASS = "dnroot"

LABSERVER = "il-auto-containers"
CHASSIS_IP = "100.64.15.236"
SLOT = 1
PORT = 25
SESSION_NAME = "sw244113_retest_full"

DUT_MAC = "e8:c5:7a:d6:30:18"  # ge400-0/0/3 HW MAC from recon
SRC_MAC = "00:10:94:01:19:01"

GE_SUB = "ge400-0/0/3.100"
BUN_SUB = "bundle-10.100"
VRF = "urpf-vrf"

OUT = "/home/dn/output/sw244113_retest"
os.makedirs(OUT, exist_ok=True)

RESULTS = {}
OVERALL_START = datetime.utcnow()

# -----------------------------------------------------------------------------
# DUT helpers
# -----------------------------------------------------------------------------
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


def commit_config(chan, cmds, label):
    """Send a list of config commands then commit. Returns (ok, output-of-commit)."""
    rp(chan, "configure", 3, silent=True)
    rp(chan, "rollback", 4, silent=True)  # discard any pending
    rp(chan, "configure", 3, silent=True)
    last = ""
    for c in cmds:
        out = rp(chan, c, 3)
        last = out
    commit_out = rp(chan, "commit", 15)
    rp(chan, "end", 3, silent=True)
    ok = "ERROR" not in commit_out and ("succeed" in commit_out.lower() or "commit action is not applicable" in commit_out.lower())
    return ok, commit_out


# -----------------------------------------------------------------------------
# Spirent helpers
# -----------------------------------------------------------------------------
def spirent_connect():
    print(f"[spirent] connecting to {LABSERVER}")
    stc = stchttp.StcHttp(LABSERVER, port=80)
    for s in stc.sessions():
        if SESSION_NAME in s:
            print(f"[spirent] ending old session {s}")
            try:
                stc.join_session(s)
                stc.end_session(s)
            except Exception as e:
                print(f"[spirent] warn: {e}")
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


def create_stream(stc, port, name, src_ip, dst_ip, vlan_id, is_v6=False, rate_fps=1000):
    sb = stc.create("streamBlock", under=port)
    stc.config(sb, {
        "Name": name,
        "FixedFrameLength": "128",
        "LoadUnit": "FRAMES_PER_SECOND",
        "Load": str(rate_fps),
    })
    stc.apply()
    eth = stc.get(sb, "children-ethernet:EthernetII").split()[0]
    stc.config(eth, {"srcMac": SRC_MAC, "dstMac": DUT_MAC})

    vlans_container = stc.get(eth, "children-vlans").split()[0]
    vlan = stc.create("Vlan", under=vlans_container)
    stc.config(vlan, {"id": str(vlan_id)})

    if is_v6:
        # Remove default IPv4 and add IPv6
        ipv4 = stc.get(sb, "children-ipv4:IPv4")
        if ipv4:
            for i in ipv4.split():
                stc.delete(i)
        ipv6 = stc.create("ipv6:IPv6", under=sb)
        stc.config(ipv6, {"sourceAddr": src_ip, "destAddr": dst_ip, "hopLimit": "64"})
    else:
        ipv4 = stc.get(sb, "children-ipv4:IPv4").split()[0]
        stc.config(ipv4, {"sourceAddr": src_ip, "destAddr": dst_ip, "ttl": "64"})
    stc.apply()
    return sb


def run_traffic(stc, port, dut_chan, iface, duration=12, label=""):
    gen = stc.get(port, "children-generator")
    gen_cfg = stc.get(gen, "children-generatorconfig")
    stc.config(gen_cfg, {
        "SchedulingMode": "PORT_BASED",
        "DurationMode": "CONTINUOUS",
        "LoadUnit": "FRAMES_PER_SECOND",
        "FixedLoad": "1000",
    })
    stc.apply()

    before = get_counters(dut_chan, iface)
    print(f"  [{label}] starting traffic on Spirent...")
    stc.perform("GeneratorStart", params={"GeneratorList": gen})
    time.sleep(duration)
    stc.perform("GeneratorStop", params={"GeneratorList": gen})
    time.sleep(3)
    after = get_counters(dut_chan, iface)
    res = {
        "rx_delta": after["rx"] - before["rx"],
        "v4_drops_delta": after["v4_drops"] - before["v4_drops"],
        "v6_drops_delta": after["v6_drops"] - before["v6_drops"],
        "rx_before": before["rx"], "rx_after": after["rx"],
        "v4_before": before["v4_drops"], "v4_after": after["v4_drops"],
        "v6_before": before["v6_drops"], "v6_after": after["v6_drops"],
        "counters_after": after["raw"],
    }
    print(f"  [{label}] RX Δ={res['rx_delta']:,}  v4dropΔ={res['v4_drops_delta']:,}  v6dropΔ={res['v6_drops_delta']:,}")
    return res


# -----------------------------------------------------------------------------
# Cleanup prior leftover config
# -----------------------------------------------------------------------------
def cleanup_prior(chan):
    print("\n=== Cleaning up pre-existing uRPF / VRF leftovers ===")
    cmds = [
        f"no interfaces {BUN_SUB}",
        f"no interfaces {GE_SUB}",
        f"no network-services vrf instance {VRF}",
    ]
    rp(chan, "configure", 3, silent=True)
    rp(chan, "rollback", 4, silent=True)
    rp(chan, "configure", 3, silent=True)
    for c in cmds:
        rp(chan, c, 3)
    co = rp(chan, "commit", 15)
    rp(chan, "end", 3, silent=True)
    return co


# -----------------------------------------------------------------------------
# Step implementations
# -----------------------------------------------------------------------------
def step1(chan):
    print("\n" + "="*70)
    print("STEP 1: Create VRF + ge sub-if + bundle sub-if with IPv4/IPv6")
    print("="*70)
    cmds = [
        f"interfaces {GE_SUB} vlan-id 100",
        "top",
        f"interfaces {GE_SUB} ipv4-address 10.100.1.1/24",
        "top",
        f"interfaces {GE_SUB} ipv6-address 2001:db8:100::1/64",
        "top",
        f"interfaces {BUN_SUB} vlan-id 100",
        "top",
        f"interfaces {BUN_SUB} ipv4-address 10.100.2.1/24",
        "top",
        f"interfaces {BUN_SUB} ipv6-address 2001:db8:200::1/64",
        "top",
        f"network-services vrf instance {VRF}",
        f"interface {GE_SUB}",
        f"interface {BUN_SUB}",
        "top",
    ]
    ok, co = commit_config(chan, cmds, "step1")
    vrf = rp(chan, "show network-services vrf | no-more", 6)
    ge_d = rp(chan, f"show interfaces detail {GE_SUB} | no-more", 10)
    bun_d = rp(chan, f"show interfaces detail {BUN_SUB} | no-more", 10)
    vrf_cfg = rp(chan, f"show config network-services vrf instance {VRF} | no-more", 6)
    res = {
        "commit_ok": ok, "commit_out": co[-500:],
        "vrf_list": vrf, "ge_detail": ge_d, "bundle_detail": bun_d,
        "vrf_config": vrf_cfg,
    }
    passed = (ok and VRF in vrf and "10.100.1.1" in ge_d and "10.100.2.1" in bun_d
              and f"VRF ({VRF})" in ge_d and f"VRF ({VRF})" in bun_d)
    res["result"] = "PASS" if passed else "FAIL"
    print(f">>> STEP 1: {res['result']}")
    return res


def step2(chan):
    print("\n" + "="*70)
    print("STEP 2: Configure uRPF strict on ge sub-if within VRF")
    print("="*70)
    cmds = [
        f"interfaces {GE_SUB} urpf admin-state enabled",
        "top",
        f"interfaces {GE_SUB} urpf mode strict",
        "top",
    ]
    ok, co = commit_config(chan, cmds, "step2")
    detail = rp(chan, f"show interfaces detail {GE_SUB} | no-more", 10)
    cfg = rp(chan, f"show config interfaces {GE_SUB} urpf | no-more", 6)
    passed = (ok and "uRPF IPv4 check: enabled" in detail and "Mode: strict" in detail
              and "uRPF IPv6 check: enabled" in detail)
    res = {"commit_ok": ok, "commit_out": co[-500:], "detail": detail, "config": cfg,
           "result": "PASS" if passed else "FAIL"}
    print(f">>> STEP 2: {res['result']}")
    return res


def step3(chan):
    print("\n" + "="*70)
    print("STEP 3: Install static reverse-path routes in VRF")
    print("="*70)
    cmds = [
        f"network-services vrf instance {VRF} protocols static address-family ipv4-unicast",
        f"route 10.100.10.0/24 next-hop 10.100.1.2 interface {GE_SUB}",
        "top",
        f"network-services vrf instance {VRF} protocols static address-family ipv4-unicast",
        f"route 10.100.20.0/24 next-hop 10.100.2.2 interface {BUN_SUB}",
        "top",
        f"network-services vrf instance {VRF} protocols static address-family ipv4-unicast",
        f"route 0.0.0.0/0 next-hop 10.100.1.2 interface {GE_SUB}",
        "top",
        f"network-services vrf instance {VRF} protocols static address-family ipv6-unicast",
        f"route 2001:db8:10::/64 next-hop 2001:db8:100::2 interface {GE_SUB}",
        "top",
        f"network-services vrf instance {VRF} protocols static address-family ipv6-unicast",
        f"route 2001:db8:20::/64 next-hop 2001:db8:200::2 interface {BUN_SUB}",
        "top",
        f"network-services vrf instance {VRF} protocols static address-family ipv6-unicast",
        f"route ::/0 next-hop 2001:db8:100::2 interface {GE_SUB}",
        "top",
    ]
    ok, co = commit_config(chan, cmds, "step3")
    v4 = rp(chan, f"show route vrf {VRF} 10.100.10.0/24 | no-more", 8)
    v6 = rp(chan, f"show route vrf {VRF} 2001:db8:10::/64 | no-more", 8)
    full4 = rp(chan, f"show route vrf {VRF} table ipv4-unicast | no-more", 8)
    full6 = rp(chan, f"show route vrf {VRF} table ipv6-unicast | no-more", 8)
    passed = (ok and "10.100.10.0/24" in full4 and "2001:db8:10::/64" in full6
              and "10.100.20.0/24" in full4)
    res = {"commit_ok": ok, "commit_out": co[-500:], "v4_route": v4,
           "v6_route": v6, "v4_table": full4, "v6_table": full6,
           "result": "PASS" if passed else "FAIL"}
    print(f">>> STEP 3: {res['result']}")
    return res


def step4(chan, stc, port):
    print("\n" + "="*70)
    print("STEP 4: Valid IPv4 src (10.100.10.100 -> 10.100.20.100) -- zero drops")
    print("="*70)
    clear_streams(stc, port)
    create_stream(stc, port, "s4_v4_valid",
                  "10.100.10.100", "10.100.20.100", 100)
    r = run_traffic(stc, port, chan, GE_SUB, 12, "S4 v4 valid")
    passed = (r["rx_delta"] > 0 and r["v4_drops_delta"] == 0)
    r["result"] = "PASS" if passed else "FAIL"
    print(f">>> STEP 4: {r['result']}")
    return r


def step5(chan, stc, port):
    print("\n" + "="*70)
    print("STEP 5: Invalid IPv4 src (10.100.99.100) -- uRPF v4 drops expected")
    print("="*70)
    clear_streams(stc, port)
    create_stream(stc, port, "s5_v4_invalid",
                  "10.100.99.100", "10.100.20.100", 100)
    r = run_traffic(stc, port, chan, GE_SUB, 12, "S5 v4 invalid")
    passed = (r["v4_drops_delta"] > 0)
    r["result"] = "PASS" if passed else "FAIL"
    print(f">>> STEP 5: {r['result']}")
    return r


def step6(chan, stc, port):
    print("\n" + "="*70)
    print("STEP 6: Repeat with IPv6 (valid -> no drops, invalid -> v6 drops)")
    print("="*70)
    # 6a valid
    clear_streams(stc, port)
    create_stream(stc, port, "s6a_v6_valid",
                  "2001:db8:10::100", "2001:db8:20::100", 100, is_v6=True)
    r_valid = run_traffic(stc, port, chan, GE_SUB, 12, "S6a v6 valid")
    # 6b invalid
    clear_streams(stc, port)
    create_stream(stc, port, "s6b_v6_invalid",
                  "2001:db8:99::100", "2001:db8:20::100", 100, is_v6=True)
    r_inv = run_traffic(stc, port, chan, GE_SUB, 12, "S6b v6 invalid")
    passed = (r_valid["rx_delta"] > 0 and r_valid["v6_drops_delta"] == 0 and r_inv["v6_drops_delta"] > 0)
    res = {"valid": r_valid, "invalid": r_inv, "result": "PASS" if passed else "FAIL"}
    print(f">>> STEP 6: {res['result']}")
    return res


def step7(chan):
    print("\n" + "="*70)
    print("STEP 7: Configure uRPF strict on bundle sub-if within VRF")
    print("="*70)
    cmds = [
        f"interfaces {BUN_SUB} urpf admin-state enabled",
        "top",
        f"interfaces {BUN_SUB} urpf mode strict",
        "top",
    ]
    ok, co = commit_config(chan, cmds, "step7")
    detail = rp(chan, f"show interfaces detail {BUN_SUB} | no-more", 10)
    cfg = rp(chan, f"show config interfaces {BUN_SUB} urpf | no-more", 6)
    ge_d = rp(chan, f"show interfaces detail {GE_SUB} | no-more", 10)
    passed = (ok and "uRPF IPv4 check: enabled" in detail and "Mode: strict" in detail
              and "uRPF IPv4 check: enabled" in ge_d)
    res = {"commit_ok": ok, "commit_out": co[-500:], "bundle_detail": detail,
           "bundle_config": cfg, "ge_detail": ge_d,
           "result": "PASS" if passed else "FAIL"}
    print(f">>> STEP 7: {res['result']}")
    return res


def step8(chan):
    """Verify counters on bundle sub-if are independent from ge sub-if.
    No Spirent traffic goes into bundle-10.100 in this lab; verify counters are
    per-interface by observing ge counters accumulate while bundle stays 0."""
    print("\n" + "="*70)
    print("STEP 8: Bundle sub-if counters independent from ge sub-if (observation)")
    print("="*70)
    ge_cnt = get_counters(chan, GE_SUB)
    bun_cnt = get_counters(chan, BUN_SUB)
    print(f"  ge sub: rx={ge_cnt['rx']}, v4drop={ge_cnt['v4_drops']}, v6drop={ge_cnt['v6_drops']}")
    print(f"  bun sub: rx={bun_cnt['rx']}, v4drop={bun_cnt['v4_drops']}, v6drop={bun_cnt['v6_drops']}")
    # Independent if ge has received packets (from earlier steps) but bundle counters
    # haven't incremented with ge traffic.
    passed = (ge_cnt["rx"] > 0 and bun_cnt["rx"] == 0)
    res = {"ge_counters": ge_cnt["raw"], "bundle_counters": bun_cnt["raw"],
           "ge_rx": ge_cnt["rx"], "bundle_rx": bun_cnt["rx"],
           "ge_v4_drops": ge_cnt["v4_drops"], "bundle_v4_drops": bun_cnt["v4_drops"],
           "result": "PASS" if passed else "FAIL"}
    print(f">>> STEP 8: {res['result']} (counters independent per-interface)")
    return res


def step9(chan, stc, port):
    print("\n" + "="*70)
    print("STEP 9: Per-AFI IPv4 strict + IPv6 loose on ge sub-if")
    print("  Invalid v4 -> drops (strict); Invalid v6 -> forwards (loose)")
    print("="*70)
    cmds = [
        f"interfaces {GE_SUB} urpf address-family ipv4 admin-state enabled",
        "top",
        f"interfaces {GE_SUB} urpf address-family ipv4 mode strict",
        "top",
        f"interfaces {GE_SUB} urpf address-family ipv6 admin-state enabled",
        "top",
        f"interfaces {GE_SUB} urpf address-family ipv6 mode loose",
        "top",
    ]
    ok, co = commit_config(chan, cmds, "step9")
    detail = rp(chan, f"show interfaces detail {GE_SUB} | no-more", 10)

    # Send invalid v4 — should drop
    clear_streams(stc, port)
    create_stream(stc, port, "s9_v4_invalid",
                  "10.100.99.100", "10.100.20.100", 100)
    r_v4 = run_traffic(stc, port, chan, GE_SUB, 12, "S9 v4 invalid (strict)")

    # Send invalid v6 — should forward (loose).
    # "loose" requires src to have ANY reverse-path in FIB. Add a broad v6 route
    # in the VRF so 2001:db8:99::/64 has a match via a DIFFERENT egress.
    rp(chan, "configure", 3, silent=True)
    rp(chan, f"network-services vrf instance {VRF} protocols static address-family ipv6-unicast", 3)
    rp(chan, f"route 2001:db8:99::/64 next-hop 2001:db8:200::2 interface {BUN_SUB}", 3)
    rp(chan, "top", 3, silent=True)
    co_add = rp(chan, "commit", 10)
    rp(chan, "end", 3, silent=True)

    clear_streams(stc, port)
    create_stream(stc, port, "s9_v6_invalid",
                  "2001:db8:99::100", "2001:db8:20::100", 100, is_v6=True)
    r_v6 = run_traffic(stc, port, chan, GE_SUB, 12, "S9 v6 invalid (loose)")

    cfg_ok = (ok and "uRPF IPv4 check: enabled, Mode: strict" in detail
              and "uRPF IPv6 check: enabled, Mode: loose" in detail)
    behav_ok = (r_v4["v4_drops_delta"] > 0 and r_v6["rx_delta"] > 0 and r_v6["v6_drops_delta"] == 0)
    passed = cfg_ok and behav_ok
    res = {"commit_ok": ok, "commit_out": co[-500:], "detail": detail,
           "v4_traffic": r_v4, "v6_traffic": r_v6,
           "result": "PASS" if passed else "FAIL"}
    print(f">>> STEP 9: {res['result']}")
    return res


def step10(chan, stc, port):
    print("\n" + "="*70)
    print("STEP 10: Reverse per-AFI IPv4 loose + IPv6 strict on ge sub-if")
    print("  Invalid v4 -> forwards (loose); Invalid v6 -> drops (strict)")
    print("="*70)
    cmds = [
        f"interfaces {GE_SUB} urpf address-family ipv4 mode loose",
        "top",
        f"interfaces {GE_SUB} urpf address-family ipv6 mode strict",
        "top",
    ]
    ok, co = commit_config(chan, cmds, "step10")
    detail = rp(chan, f"show interfaces detail {GE_SUB} | no-more", 10)

    # Need a v4 reverse-path for 10.100.99/24 for loose (any FIB match).
    rp(chan, "configure", 3, silent=True)
    rp(chan, f"network-services vrf instance {VRF} protocols static address-family ipv4-unicast", 3)
    rp(chan, f"route 10.100.99.0/24 next-hop 10.100.2.2 interface {BUN_SUB}", 3)
    rp(chan, "top", 3, silent=True)
    rp(chan, "commit", 10)
    rp(chan, "end", 3, silent=True)

    clear_streams(stc, port)
    create_stream(stc, port, "s10_v4_invalid",
                  "10.100.99.100", "10.100.20.100", 100)
    r_v4 = run_traffic(stc, port, chan, GE_SUB, 12, "S10 v4 invalid (loose)")

    clear_streams(stc, port)
    create_stream(stc, port, "s10_v6_invalid",
                  "2001:db8:99::100", "2001:db8:20::100", 100, is_v6=True)
    r_v6 = run_traffic(stc, port, chan, GE_SUB, 12, "S10 v6 invalid (strict)")

    cfg_ok = (ok and "uRPF IPv4 check: enabled, Mode: loose" in detail
              and "uRPF IPv6 check: enabled, Mode: strict" in detail)
    behav_ok = (r_v4["rx_delta"] > 0 and r_v4["v4_drops_delta"] == 0
                and r_v6["v6_drops_delta"] > 0)
    passed = cfg_ok and behav_ok
    res = {"commit_ok": ok, "commit_out": co[-500:], "detail": detail,
           "v4_traffic": r_v4, "v6_traffic": r_v6,
           "result": "PASS" if passed else "FAIL"}
    print(f">>> STEP 10: {res['result']}")
    return res


def step11(chan, stc, port):
    print("\n" + "="*70)
    print("STEP 11: allow-default enabled + remove specific route")
    print("  Src matches only VRF default via ingress -> PASS")
    print("="*70)
    # Restore both AFIs to strict, enable allow-default on ge sub-if
    cmds = [
        f"interfaces {GE_SUB} urpf address-family ipv4 mode strict",
        "top",
        f"interfaces {GE_SUB} urpf address-family ipv6 mode strict",
        "top",
        f"interfaces {GE_SUB} urpf address-family ipv4 allow-default enabled",
        "top",
        f"interfaces {GE_SUB} urpf address-family ipv6 allow-default enabled",
        "top",
        f"interfaces {BUN_SUB} urpf address-family ipv4 allow-default enabled",
        "top",
        f"interfaces {BUN_SUB} urpf address-family ipv6 allow-default enabled",
        "top",
        # Remove specific 10.100.10.0/24 route so source 10.100.10.x matches only default
        f"no network-services vrf instance {VRF} protocols static address-family ipv4-unicast route 10.100.10.0/24",
        "top",
    ]
    ok, co = commit_config(chan, cmds, "step11")
    detail = rp(chan, f"show interfaces detail {GE_SUB} | no-more", 10)

    clear_streams(stc, port)
    create_stream(stc, port, "s11_allow_default",
                  "10.100.10.100", "10.100.20.100", 100)
    r = run_traffic(stc, port, chan, GE_SUB, 12, "S11 allow-default=enabled")
    cfg_ok = ok and "Allow-default: enabled" in detail
    behav_ok = (r["rx_delta"] > 0 and r["v4_drops_delta"] == 0)
    passed = cfg_ok and behav_ok
    res = {"commit_ok": ok, "commit_out": co[-500:], "detail": detail,
           "traffic": r, "result": "PASS" if passed else "FAIL"}
    print(f">>> STEP 11: {res['result']}")
    return res


def step12(chan, stc, port):
    print("\n" + "="*70)
    print("STEP 12: allow-default disabled -- same traffic must be DROPPED")
    print("="*70)
    cmds = [
        f"interfaces {GE_SUB} urpf address-family ipv4 allow-default disabled",
        "top",
        f"interfaces {GE_SUB} urpf address-family ipv6 allow-default disabled",
        "top",
        f"interfaces {BUN_SUB} urpf address-family ipv4 allow-default disabled",
        "top",
        f"interfaces {BUN_SUB} urpf address-family ipv6 allow-default disabled",
        "top",
    ]
    ok, co = commit_config(chan, cmds, "step12")
    detail = rp(chan, f"show interfaces detail {GE_SUB} | no-more", 10)

    clear_streams(stc, port)
    create_stream(stc, port, "s12_no_allow_default",
                  "10.100.10.100", "10.100.20.100", 100)
    r = run_traffic(stc, port, chan, GE_SUB, 12, "S12 allow-default=disabled")
    cfg_ok = ok and "Allow-default: disabled" in detail
    behav_ok = (r["v4_drops_delta"] > 0)
    passed = cfg_ok and behav_ok
    res = {"commit_ok": ok, "commit_out": co[-500:], "detail": detail,
           "traffic": r, "result": "PASS" if passed else "FAIL"}
    print(f">>> STEP 12: {res['result']}")
    return res


def step13(chan, stc, port):
    print("\n" + "="*70)
    print("STEP 13: VRF isolation -- src has route in default VRF, not in urpf-vrf")
    print("="*70)
    # Add 10.33.0.0/24 in default VRF so src 10.33.0.100 has a reverse path
    # in default VRF (but none in urpf-vrf).
    rp(chan, "configure", 3, silent=True)
    rp(chan, "protocols static address-family ipv4-unicast", 3)
    rp(chan, "route 10.33.0.0/24 next-hop 20.0.0.2 interface bundle-10", 3)
    rp(chan, "top", 3, silent=True)
    co = rp(chan, "commit", 10)
    rp(chan, "end", 3, silent=True)

    # Also restore the specific v4 reverse path for 10.100.10/24 so sanity
    # forwarding still works in urpf-vrf if we wanted, but we need src 10.33.0.100
    # to have NO route in urpf-vrf. The default route 0.0.0.0/0 exists -> would
    # catch it; remove default first then reinstall after.
    rp(chan, "configure", 3, silent=True)
    rp(chan, f"no network-services vrf instance {VRF} protocols static address-family ipv4-unicast route 0.0.0.0/0", 3)
    rp(chan, "top", 3, silent=True)
    rp(chan, "commit", 10)
    rp(chan, "end", 3, silent=True)

    clear_streams(stc, port)
    create_stream(stc, port, "s13_vrf_isolation",
                  "10.33.0.100", "10.100.20.100", 100)
    r = run_traffic(stc, port, chan, GE_SUB, 12, "S13 VRF isolation")
    vrf_route = rp(chan, f"show route vrf {VRF} 10.33.0.0/24 | no-more", 8)
    default_route = rp(chan, "show route vrf default 10.33.0.0/24 | no-more", 8)
    passed = (r["v4_drops_delta"] > 0)
    res = {"commit_out": co[-500:], "vrf_route_check": vrf_route,
           "default_route_check": default_route,
           "traffic": r, "result": "PASS" if passed else "FAIL"}
    print(f">>> STEP 13: {res['result']}")
    return res


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
def main():
    # DUT session
    ssh, chan = dut_connect()
    try:
        ver = rp(chan, "show system version | no-more", 6)
        RESULTS["version"] = ver
        cleanup_prior(chan)

        # Steps 1-3 (config)
        RESULTS["step1"] = step1(chan)
        if RESULTS["step1"]["result"] == "FAIL":
            print("STEP 1 failed — aborting.")
            return
        RESULTS["step2"] = step2(chan)
        RESULTS["step3"] = step3(chan)

        # Spirent session
        stc, sid, project, port1 = spirent_connect()
        try:
            RESULTS["step4"] = step4(chan, stc, port1)
            RESULTS["step5"] = step5(chan, stc, port1)
            RESULTS["step6"] = step6(chan, stc, port1)
            RESULTS["step7"] = step7(chan)
            RESULTS["step8"] = step8(chan)
            RESULTS["step9"] = step9(chan, stc, port1)
            RESULTS["step10"] = step10(chan, stc, port1)
            RESULTS["step11"] = step11(chan, stc, port1)
            RESULTS["step12"] = step12(chan, stc, port1)
            RESULTS["step13"] = step13(chan, stc, port1)
        finally:
            try:
                clear_streams(stc, port1)
                stc.end_session(sid)
                print("[spirent] session ended")
            except Exception as e:
                print(f"[spirent] cleanup warn: {e}")
    finally:
        chan.close()
        ssh.close()

    # Summary
    print("\n" + "="*70)
    print("SUMMARY")
    print("="*70)
    for i in range(1, 14):
        key = f"step{i}"
        r = RESULTS.get(key, {}).get("result", "N/A")
        print(f"  {key:8s}: {r}")
    passes = sum(1 for i in range(1, 14) if RESULTS.get(f"step{i}", {}).get("result") == "PASS")
    print(f"\n  OVERALL: {passes}/13 steps PASS")
    RESULTS["_summary"] = {
        "passes": passes, "total": 13,
        "started": OVERALL_START.isoformat() + "Z",
        "ended": datetime.utcnow().isoformat() + "Z",
    }

    with open(f"{OUT}/full_results.json", "w") as f:
        json.dump(RESULTS, f, indent=2, default=str)
    print(f"\nSaved: {OUT}/full_results.json")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        traceback.print_exc()
        RESULTS["_exception"] = str(e)
        with open(f"{OUT}/full_results.json", "w") as f:
            json.dump(RESULTS, f, indent=2, default=str)
        sys.exit(1)
