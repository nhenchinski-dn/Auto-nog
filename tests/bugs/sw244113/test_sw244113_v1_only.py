#!/usr/bin/env python3
"""Recover DUT from stuck config prompt and run V1 (physical ge variant)."""
import paramiko, time, re, json, os
from datetime import datetime

HOST = "100.64.8.59"
USER = "dnroot"
PASS = "dnroot"
PHYS = "ge400-0/0/18"
PHYS_VRF = "urpf-vrf-phys"
OUT = "/home/dn/output/sw244113_retest"


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


def run(chan, cmd, wait=6):
    chan.send(cmd + "\n")
    time.sleep(wait)
    out = b""
    while chan.recv_ready():
        out += chan.recv(65535)
        time.sleep(0.3)
    return clean(out.decode(errors="replace"))


def rp(chan, cmd, wait=6):
    out = run(chan, cmd, wait)
    print(f"  [{cmd}]")
    for line in out.split("\n"):
        print(f"    {line}")
    return out


def main():
    ssh, chan = dut_connect()
    results = {"started": datetime.utcnow().isoformat() + "Z"}
    try:
        print("=== Recover DUT state (fresh shell) ===")
        # New SSH shell starts in exec mode; enter cfg fresh
        rp(chan, "configure", 5)
        rp(chan, "rollback", 5)
        rp(chan, "end", 4)

        print("\n=== V1: Physical ge variant ===")
        rp(chan, "configure", 5)
        rp(chan, f"interfaces {PHYS} admin-state enabled", 3)
        rp(chan, "top", 3)
        rp(chan, f"interfaces {PHYS} ipv4-address 10.200.1.1/24", 3)
        rp(chan, "top", 3)
        rp(chan, f"interfaces {PHYS} ipv6-address 2001:db8:300::1/64", 3)
        rp(chan, "top", 3)
        rp(chan, f"network-services vrf instance {PHYS_VRF}", 3)
        rp(chan, f"interface {PHYS}", 3)
        rp(chan, "top", 3)
        rp(chan, f"interfaces {PHYS} urpf admin-state enabled", 3)
        rp(chan, "top", 3)
        rp(chan, f"interfaces {PHYS} urpf mode strict", 3)
        rp(chan, "top", 3)
        rp(chan, f"network-services vrf instance {PHYS_VRF} protocols static address-family ipv4-unicast", 3)
        rp(chan, f"route 10.200.10.0/24 next-hop 10.200.1.2 interface {PHYS}", 3)
        rp(chan, "top", 3)
        commit_out = rp(chan, "commit", 15)
        rp(chan, "end", 4)

        print("\n=== V1 verification ===")
        detail = rp(chan, f"show interfaces detail {PHYS} | no-more", 10)
        vrf_list = rp(chan, "show network-services vrf | no-more", 6)
        route = rp(chan, f"show route vrf {PHYS_VRF} table ipv4-unicast | no-more", 8)
        config = rp(chan, f"show config interfaces {PHYS} urpf | no-more", 6)

        commit_ok = "succeed" in commit_out.lower() and "error" not in commit_out.lower()
        detail_ok = ("uRPF IPv4 check: enabled, Mode: strict" in detail
                     and f"VRF ({PHYS_VRF})" in detail)
        vrf_ok = PHYS_VRF in vrf_list
        route_ok = "10.200.10.0/24" in route

        v1_pass = commit_ok and detail_ok and vrf_ok and route_ok
        results["V1"] = {
            "commit_ok": commit_ok,
            "commit_out": commit_out,
            "detail": detail,
            "vrf_list": vrf_list,
            "route_table": route,
            "config": config,
            "detail_ok": detail_ok,
            "vrf_ok": vrf_ok,
            "route_ok": route_ok,
            "result": "PASS" if v1_pass else "FAIL",
        }

        print(f"\n>>> V1: {results['V1']['result']}")
        print(f"  commit_ok={commit_ok}, detail_ok={detail_ok}, vrf_ok={vrf_ok}, route_ok={route_ok}")

        # Cleanup V1
        print("\n=== V1 cleanup ===")
        rp(chan, "configure", 5)
        rp(chan, f"no interfaces {PHYS} urpf", 3)
        rp(chan, "top", 3)
        rp(chan, f"no interfaces {PHYS} ipv4-address", 3)
        rp(chan, "top", 3)
        rp(chan, f"no interfaces {PHYS} ipv6-address", 3)
        rp(chan, "top", 3)
        rp(chan, f"no network-services vrf instance {PHYS_VRF}", 3)
        rp(chan, "top", 3)
        rp(chan, "commit", 15)
        rp(chan, "end", 4)
    finally:
        chan.close()
        ssh.close()

    results["ended"] = datetime.utcnow().isoformat() + "Z"
    with open(f"{OUT}/v1_results.json", "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nSaved: {OUT}/v1_results.json")


if __name__ == "__main__":
    main()
