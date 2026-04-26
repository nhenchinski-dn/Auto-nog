#!/usr/bin/env python3
"""SW-258847 — full 7-step lifecycle per variant interface.

Each variant runs in its own isolated test VRF to bypass per-VRF
allow-default uniformity constraint with production ge400-0/0/5.

Steps (per Jira test steps):
  S1: Configure variant's uRPF starting config
  S2: show interfaces <IF>       (uRPF lines)
  S3: show interfaces detail <IF> (uRPF lines)
  S4: Modify single AFI mode
  S5: Enable allow-default on one AFI
  S6: Delete per-AFI config (fall back to global)
  S7: Delete all uRPF  (both AFIs disabled)

V4 (IRB) adapted: strict mode not supported on IRB, uses loose.
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


def session():
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(HOST, username=USER, password=PASS,
                look_for_keys=False, allow_agent=False, timeout=20)
    sh = cli.invoke_shell(width=300, height=6000)
    time.sleep(6)
    drain(sh)
    return cli, sh


def close(cli, sh):
    try:
        sh.send("exit\n")
        time.sleep(1)
    except Exception:
        pass
    cli.close()


def do_cfg(sh, cmds, retries=3):
    """Commit config block with retry on out-of-sync / in-progress errors."""
    last_log = ""
    for attempt in range(retries):
        log = run(sh, "configure", 1.5)
        for c in cmds:
            log += run(sh, c, 1.2)
        cmt = run(sh, "commit", 6)
        log += cmt
        if "Commit succeeded" in cmt:
            log += run(sh, "end", 2)
            return log, True
        if "no configuration changes" in cmt:
            log += run(sh, "end", 2)
            return log, True
        if ("out of sync" in cmt or "another commit is in progress" in cmt):
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


def run_variant(sh, name, iface, vrf, precreate_cmds, s1_cmds,
                s4_cmds, s4_desc, s5_cmds, s5_desc, s6_cmds, s6_desc,
                teardown_extra_cmds):
    """Run the 7-step lifecycle. Returns dict of step outputs."""
    r = {"name": name, "iface": iface, "vrf": vrf}

    # Isolate interface in VRF
    r["setup_vrf_log"], r["setup_vrf_ok"] = do_cfg(sh, precreate_cmds + [
        f"network-services vrf instance {vrf}",
        f"interface {iface}", "top",
    ])

    # ===== S1 =====
    r["s1_log"], r["s1_ok"] = do_cfg(sh, s1_cmds)
    r["s1_snap"] = snapshot(sh, iface)

    # ===== S2/S3 are captured in the snapshot (show brief + show detail) =====
    # Already in r["s1_snap"] — serve as Step 2 and Step 3 evidence.

    # ===== S4 =====
    r["s4_desc"] = s4_desc
    r["s4_log"], r["s4_ok"] = do_cfg(sh, s4_cmds)
    r["s4_snap"] = snapshot(sh, iface)

    # ===== S5 =====
    r["s5_desc"] = s5_desc
    r["s5_log"], r["s5_ok"] = do_cfg(sh, s5_cmds)
    r["s5_snap"] = snapshot(sh, iface)

    # ===== S6 =====
    r["s6_desc"] = s6_desc
    r["s6_log"], r["s6_ok"] = do_cfg(sh, s6_cmds)
    r["s6_snap"] = snapshot(sh, iface)

    # ===== S7: delete all uRPF =====
    r["s7_log"], r["s7_ok"] = do_cfg(sh, [
        f"no interfaces {iface} urpf", "top",
    ])
    r["s7_snap"] = snapshot(sh, iface)

    # Cleanup
    r["cleanup_log"], r["cleanup_ok"] = do_cfg(sh, teardown_extra_cmds + [
        f"no network-services vrf instance {vrf}", "top",
    ])
    return r


def main():
    results = {}
    cli, sh = session()

    try:
        # ========== V1: ge sub-interface, global-only strict ==========
        IF = "ge400-0/0/34.100"
        VRF = "urpf_v1_vrf"
        results["V1"] = run_variant(sh, "V1 ge sub-if global-only strict",
            IF, VRF,
            precreate_cmds=[f"interfaces {IF} vlan-id 100", "top"],
            s1_cmds=[
                f"interfaces {IF} urpf admin-state enabled", "top",
                f"interfaces {IF} urpf mode strict", "top",
                f"interfaces {IF} urpf allow-default disabled", "top",
            ],
            s4_cmds=[f"interfaces {IF} urpf mode loose", "top"],
            s4_desc="Change global mode strict -> loose",
            s5_cmds=[
                f"interfaces {IF} urpf address-family ipv6 admin-state enabled",
                "top",
                f"interfaces {IF} urpf address-family ipv6 allow-default enabled",
                "top",
            ],
            s5_desc="Enable v6 AFI allow-default enabled",
            s6_cmds=[f"no interfaces {IF} urpf address-family", "top"],
            s6_desc="Delete per-AFI subtree (revert to global)",
            teardown_extra_cmds=[f"no interfaces {IF}", "top"],
        )

        # ========== V2: bundle, global-only loose ==========
        IF = "bundle-99"
        VRF = "urpf_v2_vrf"
        results["V2"] = run_variant(sh, "V2 bundle global-only loose",
            IF, VRF,
            precreate_cmds=[f"interfaces {IF} admin-state enabled", "top"],
            s1_cmds=[
                f"interfaces {IF} urpf admin-state enabled", "top",
                f"interfaces {IF} urpf mode loose", "top",
                f"interfaces {IF} urpf allow-default disabled", "top",
            ],
            s4_cmds=[f"interfaces {IF} urpf mode strict", "top"],
            s4_desc="Change global mode loose -> strict",
            s5_cmds=[
                f"interfaces {IF} urpf address-family ipv6 admin-state enabled",
                "top",
                f"interfaces {IF} urpf address-family ipv6 allow-default enabled",
                "top",
            ],
            s5_desc="Enable v6 AFI allow-default enabled",
            s6_cmds=[f"no interfaces {IF} urpf address-family", "top"],
            s6_desc="Delete per-AFI subtree",
            teardown_extra_cmds=[f"no interfaces {IF}", "top"],
        )

        # ========== V3: bundle sub-interface, per-AFI only ==========
        PARENT = "bundle-99"
        IF = "bundle-99.200"
        VRF = "urpf_v3_vrf"
        results["V3"] = run_variant(sh, "V3 bundle sub-if per-AFI only",
            IF, VRF,
            precreate_cmds=[
                f"interfaces {PARENT} admin-state enabled", "top",
                f"interfaces {IF} vlan-id 200", "top",
            ],
            s1_cmds=[
                f"interfaces {IF} urpf admin-state enabled", "top",
                f"interfaces {IF} urpf address-family ipv4 admin-state enabled",
                "top",
                f"interfaces {IF} urpf address-family ipv4 mode strict", "top",
                f"interfaces {IF} urpf address-family ipv6 admin-state enabled",
                "top",
                f"interfaces {IF} urpf address-family ipv6 mode loose", "top",
            ],
            s4_cmds=[
                f"interfaces {IF} urpf address-family ipv6 mode strict", "top",
            ],
            s4_desc="Change v6 per-AFI mode loose -> strict",
            s5_cmds=[
                f"interfaces {IF} urpf address-family ipv6 allow-default enabled",
                "top",
            ],
            s5_desc="Enable v6 AFI allow-default enabled",
            s6_cmds=[
                f"no interfaces {IF} urpf address-family ipv6", "top",
            ],
            s6_desc="Delete v6 per-AFI (v4 per-AFI remains)",
            teardown_extra_cmds=[
                f"no interfaces {IF}", "top",
                f"no interfaces {PARENT}", "top",
            ],
        )

        # ========== V4: IRB, global + per-AFI override (all loose) ==========
        IF = "irb99"
        VRF = "urpf_v4_vrf"
        results["V4"] = run_variant(sh, "V4 IRB global+per-AFI loose (strict unsupported)",
            IF, VRF,
            precreate_cmds=[f"interfaces {IF} admin-state enabled", "top"],
            s1_cmds=[
                f"interfaces {IF} urpf admin-state enabled", "top",
                f"interfaces {IF} urpf mode loose", "top",
                f"interfaces {IF} urpf allow-default disabled", "top",
                f"interfaces {IF} urpf address-family ipv6 admin-state enabled",
                "top",
                f"interfaces {IF} urpf address-family ipv6 mode loose", "top",
            ],
            s4_cmds=[
                # Can't flip loose<->strict on IRB; flip AD instead
                f"interfaces {IF} urpf address-family ipv6 allow-default enabled",
                "top",
            ],
            s4_desc="Change v6 per-AFI AD disabled->enabled (strict unavailable on IRB)",
            s5_cmds=[
                # Same as S4 outcome; add v4 per-AFI to show both
                f"interfaces {IF} urpf address-family ipv4 admin-state enabled",
                "top",
            ],
            s5_desc="Enable v4 AFI (inherits global loose, AD disabled)",
            s6_cmds=[f"no interfaces {IF} urpf address-family", "top"],
            s6_desc="Delete per-AFI subtree",
            teardown_extra_cmds=[f"no interfaces {IF}", "top"],
        )

        # ========== V5: ge in VRF + allow-default enabled, strict ==========
        IF = "ge400-0/0/34"
        VRF = "urpf_v5_vrf"
        results["V5"] = run_variant(sh, "V5 ge VRF-isolated AD=enabled strict",
            IF, VRF,
            precreate_cmds=[],
            s1_cmds=[
                f"interfaces {IF} urpf admin-state enabled", "top",
                f"interfaces {IF} urpf mode strict", "top",
                f"interfaces {IF} urpf allow-default enabled", "top",
            ],
            s4_cmds=[f"interfaces {IF} urpf mode loose", "top"],
            s4_desc="Change global mode strict -> loose",
            s5_cmds=[
                # AD already enabled globally; add per-AFI v6 explicit
                f"interfaces {IF} urpf address-family ipv6 admin-state enabled",
                "top",
                f"interfaces {IF} urpf address-family ipv6 allow-default enabled",
                "top",
            ],
            s5_desc="Add per-AFI v6 admin-state+AD enabled (same as global)",
            s6_cmds=[f"no interfaces {IF} urpf address-family", "top"],
            s6_desc="Delete per-AFI subtree",
            teardown_extra_cmds=[],
        )

        # ========== V6: ge global-only loose ==========
        IF = "ge400-0/0/34"
        VRF = "urpf_v6_vrf"
        results["V6"] = run_variant(sh, "V6 ge global-only loose", IF, VRF,
            precreate_cmds=[],
            s1_cmds=[
                f"interfaces {IF} urpf admin-state enabled", "top",
                f"interfaces {IF} urpf mode loose", "top",
                f"interfaces {IF} urpf allow-default disabled", "top",
            ],
            s4_cmds=[f"interfaces {IF} urpf mode strict", "top"],
            s4_desc="Change global mode loose -> strict",
            s5_cmds=[
                f"interfaces {IF} urpf address-family ipv6 admin-state enabled",
                "top",
                f"interfaces {IF} urpf address-family ipv6 allow-default enabled",
                "top",
            ],
            s5_desc="Enable v6 AFI allow-default enabled",
            s6_cmds=[f"no interfaces {IF} urpf address-family", "top"],
            s6_desc="Delete per-AFI subtree",
            teardown_extra_cmds=[],
        )

        # Final sanity
        results["_final_ge34"] = run(
            sh, "show config interfaces ge400-0/0/34 | no-more", 3)
        results["_final_vrfs"] = run(
            sh, "show config network-services vrf instance | no-more", 3)
    finally:
        close(cli, sh)

    with open("/home/dn/sw258847_full7.json", "w") as f:
        json.dump(results, f, indent=2)


if __name__ == "__main__":
    main()
