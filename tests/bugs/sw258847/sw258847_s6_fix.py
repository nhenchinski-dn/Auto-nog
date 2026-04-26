#!/usr/bin/env python3
"""SW-258847 — re-run Step 6 correctly (needed specific AFI after `address-family`).

Rebuilds each variant's state up through Step 5 (global mode + per-AFI v6 AD=enabled),
then runs corrected Step 6 (`no urpf address-family ipv6`) and Step 7.
"""
import json
import re
import time

import paramiko

HOST = "WKY1C7VD00008P2"
USER = "dnroot"
PASS = "dnroot"

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def strip(t):
    t = ANSI.sub("", t).replace("\r", "")
    return re.sub(r"-- More -- \(Press q to quit\)\s*", "", t)


def drain(sh):
    out, retries = "", 0
    while True:
        if sh.recv_ready():
            out += sh.recv(65536).decode("utf-8", errors="replace")
            retries = 0
        else:
            retries += 1
            if retries > 5:
                break
            time.sleep(0.5)
    return out


def run(sh, cmd, w=2.5):
    sh.send(cmd + "\n")
    time.sleep(w)
    return strip(drain(sh))


def filt(t):
    keep = [l.rstrip() for l in t.split("\n")
            if "uRPF" in l or "urpf" in l or "Network-Service" in l]
    return "\n".join(keep) if keep else "(no uRPF lines)"


def do_cfg(sh, cmds, retries=3):
    last_log = ""
    for _ in range(retries):
        log = run(sh, "configure", 1.5)
        for c in cmds:
            log += run(sh, c, 1.2)
        cmt = run(sh, "commit", 6)
        log += cmt
        if "Commit succeeded" in cmt or "no configuration changes" in cmt:
            log += run(sh, "end", 2)
            return log, True
        if "out of sync" in cmt or "another commit is in progress" in cmt:
            log += run(sh, "rollback 0", 3)
            log += run(sh, "end", 2)
            last_log = log
            time.sleep(4)
            continue
        log += run(sh, "rollback 0", 3)
        log += run(sh, "end", 2)
        return log, False
    return last_log, False


def snapshot(sh, iface):
    return {
        "show_config": run(
            sh, f"show config interfaces {iface} urpf | no-more", 3),
        "show_brief": filt(run(sh, f"show interfaces {iface} | no-more", 5)),
        "show_detail": filt(run(
            sh, f"show interfaces detail {iface} | no-more", 5)),
    }


def s6_retry(sh, label, iface, precreate, s5_state, s6_afi_to_delete,
             teardown_extra):
    """Rebuild to end-of-S5 state, then run corrected S6 + S7."""
    r = {"label": label, "iface": iface}

    if precreate:
        r["precreate"], r["precreate_ok"] = do_cfg(sh, precreate)
    r["build_to_s5_log"], r["build_ok"] = do_cfg(sh, s5_state)
    r["after_s5"] = snapshot(sh, iface)

    r["s6_log"], r["s6_ok"] = do_cfg(sh, [
        f"no interfaces {iface} urpf address-family {s6_afi_to_delete}", "top",
    ])
    r["after_s6"] = snapshot(sh, iface)

    r["s7_log"], r["s7_ok"] = do_cfg(sh, [
        f"no interfaces {iface} urpf", "top",
    ])
    r["after_s7"] = snapshot(sh, iface)

    if teardown_extra:
        r["teardown"], r["teardown_ok"] = do_cfg(sh, teardown_extra)
    return r


def main():
    out = {}
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(HOST, username=USER, password=PASS,
                look_for_keys=False, allow_agent=False, timeout=20)
    sh = cli.invoke_shell(width=300, height=6000)
    time.sleep(6)
    drain(sh)

    try:
        # --- V1: ge sub-interface, rebuild up to end-of-S5 state ---
        IF = "ge400-0/0/34.100"
        VRF = "urpf_v1b_vrf"
        out["V1"] = s6_retry(sh, "V1 ge sub-if", IF,
            precreate=[
                f"interfaces {IF} vlan-id 100", "top",
                f"network-services vrf instance {VRF}",
                f"interface {IF}", "top",
            ],
            s5_state=[
                f"interfaces {IF} urpf admin-state enabled", "top",
                f"interfaces {IF} urpf mode loose", "top",
                f"interfaces {IF} urpf allow-default disabled", "top",
                f"interfaces {IF} urpf address-family ipv6 admin-state enabled",
                "top",
                f"interfaces {IF} urpf address-family ipv6 allow-default enabled",
                "top",
            ],
            s6_afi_to_delete="ipv6",
            teardown_extra=[
                f"no interfaces {IF}", "top",
                f"no network-services vrf instance {VRF}", "top",
            ],
        )

        # --- V2: bundle ---
        IF = "bundle-99"
        VRF = "urpf_v2b_vrf"
        out["V2"] = s6_retry(sh, "V2 bundle", IF,
            precreate=[
                f"interfaces {IF} admin-state enabled", "top",
                f"network-services vrf instance {VRF}",
                f"interface {IF}", "top",
            ],
            s5_state=[
                f"interfaces {IF} urpf admin-state enabled", "top",
                f"interfaces {IF} urpf mode strict", "top",
                f"interfaces {IF} urpf allow-default disabled", "top",
                f"interfaces {IF} urpf address-family ipv6 admin-state enabled",
                "top",
                f"interfaces {IF} urpf address-family ipv6 allow-default enabled",
                "top",
            ],
            s6_afi_to_delete="ipv6",
            teardown_extra=[
                f"no interfaces {IF}", "top",
                f"no network-services vrf instance {VRF}", "top",
            ],
        )

        # --- V4: IRB ---
        IF = "irb99"
        VRF = "urpf_v4b_vrf"
        out["V4"] = s6_retry(sh, "V4 IRB (loose)", IF,
            precreate=[
                f"interfaces {IF} admin-state enabled", "top",
                f"network-services vrf instance {VRF}",
                f"interface {IF}", "top",
            ],
            s5_state=[
                f"interfaces {IF} urpf admin-state enabled", "top",
                f"interfaces {IF} urpf mode loose", "top",
                f"interfaces {IF} urpf allow-default disabled", "top",
                f"interfaces {IF} urpf address-family ipv6 admin-state enabled",
                "top",
                f"interfaces {IF} urpf address-family ipv6 allow-default enabled",
                "top",
            ],
            s6_afi_to_delete="ipv6",
            teardown_extra=[
                f"no interfaces {IF}", "top",
                f"no network-services vrf instance {VRF}", "top",
            ],
        )

        # --- V5: ge (already in VRF test), AD=enabled strict ---
        IF = "ge400-0/0/34"
        out["V5"] = s6_retry(sh, "V5 ge VRF-iso AD=en", IF,
            precreate=None,
            s5_state=[
                f"interfaces {IF} urpf admin-state enabled", "top",
                f"interfaces {IF} urpf mode loose", "top",
                f"interfaces {IF} urpf allow-default enabled", "top",
                f"interfaces {IF} urpf address-family ipv6 admin-state enabled",
                "top",
                f"interfaces {IF} urpf address-family ipv6 allow-default enabled",
                "top",
            ],
            s6_afi_to_delete="ipv6",
            teardown_extra=None,
        )

        # --- V6: ge global-only loose, v6 per-AFI AD=enabled ---
        IF = "ge400-0/0/34"
        out["V6"] = s6_retry(sh, "V6 ge global-only", IF,
            precreate=None,
            s5_state=[
                f"interfaces {IF} urpf admin-state enabled", "top",
                f"interfaces {IF} urpf mode strict", "top",
                f"interfaces {IF} urpf allow-default disabled", "top",
                f"interfaces {IF} urpf address-family ipv6 admin-state enabled",
                "top",
                f"interfaces {IF} urpf address-family ipv6 allow-default enabled",
                "top",
            ],
            s6_afi_to_delete="ipv6",
            teardown_extra=None,
        )

        out["_final"] = run(
            sh, "show config interfaces ge400-0/0/34 | no-more", 3)
    finally:
        sh.send("exit\n")
        time.sleep(1)
        cli.close()

    with open("/home/dn/sw258847_s6_fix.json", "w") as f:
        json.dump(out, f, indent=2)


if __name__ == "__main__":
    main()
