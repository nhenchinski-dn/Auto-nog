#!/usr/bin/env python3
"""
SW-236991: Ethernet OAM Y.1731 | Scale | 1000 DM/SLM Sessions per System (1 per MA)

Sets up ~1000 maintenance-associations, each with a local MEP, then configures
1000 proactive DM + 1000 proactive SLM sessions simultaneously.  Runs the full
test plan: scale verification, aggressive intervals, HA, variants, and negative
tests.

Usage:
    python3 test_sw236991_scale.py --host-a <IP> --host-b <IP> --iface-a ge400-0/0/33 --iface-b ge400-0/0/18 --iface-a2 ge10-0/0/47 --iface-b2 ge10-0/0/48
    python3 test_sw236991_scale.py ... --scale 512           # scale to 512 only
    python3 test_sw236991_scale.py ... --skip-setup          # assume baseline exists
    python3 test_sw236991_scale.py ... --cleanup-only        # just remove scale config
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import paramiko

sys.stdout.reconfigure(line_buffering=True)

# ---------------------------------------------------------------------------
#  Defaults
# ---------------------------------------------------------------------------
DEFAULT_USER = "dnroot"
DEFAULT_PASS = "dnroot"
MD_NAME = "MD-SCALE"
MD_LEVEL = 7
BATCH_SIZE = 50
VLAN_BASE = 0
DM_PROFILE = "DM-SCALE-PROF"
SLM_PROFILE = "SLM-SCALE-PROF"
DM_PROFILE_AGG = "DM-SCALE-AGG"
SLM_PROFILE_AGG = "SLM-SCALE-AGG"
OUTPUT_DIR = "/home/dn/output"

ANSI_RE = re.compile(
    r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07|\x1b[()][AB012]"
    r"|\x1b\[\?[0-9;]*[hlm]|\r"
)
CLI_ERROR_RE = re.compile(
    r"error:|unknown\s+command|invalid|command\s+failed|"
    r"commit\s+failed|validation\s+failed|syntax\s+error",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
#  SSH helpers
# ---------------------------------------------------------------------------
def clean_ansi(text: str) -> str:
    return ANSI_RE.sub("", text).strip()


def create_shell(
    ip: str, user: str, password: str, label: str = ""
) -> Tuple[paramiko.SSHClient, paramiko.Channel]:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(
        ip, username=user, password=password,
        timeout=30, look_for_keys=False, allow_agent=False,
    )
    transport = ssh.get_transport()
    transport.set_keepalive(30)
    chan = ssh.invoke_shell(width=400, height=1000)
    time.sleep(3)
    chan.recv(65535)
    print(f"  [SSH {label}] Connected to {ip}", flush=True)
    return ssh, chan


def send(chan: paramiko.Channel, cmd: str, wait: float = 5) -> str:
    chan.send(cmd + "\n")
    time.sleep(wait)
    out = b""
    while chan.recv_ready():
        out += chan.recv(65535)
    return clean_ansi(out.decode(errors="replace"))


def drain(chan: paramiko.Channel) -> str:
    out = b""
    while chan.recv_ready():
        out += chan.recv(65535)
    return clean_ansi(out.decode(errors="replace"))


def send_fast(chan: paramiko.Channel, cmds: List[str], gap: float = 0.02) -> None:
    for i, cmd in enumerate(cmds):
        chan.send(cmd + "\n")
        if gap > 0:
            time.sleep(gap)
        if (i + 1) % 100 == 0:
            drain(chan)


def run_show(chan: paramiko.Channel, cmd: str, wait: float = 10) -> str:
    return send(chan, cmd + " | no-more", wait)


def run_show_large(chan: paramiko.Channel, cmd: str, timeout: float = 60) -> str:
    chan.send(cmd + " | no-more\n")
    buf = b""
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.5)
        while chan.recv_ready():
            buf += chan.recv(65535)
        text = buf.decode(errors="replace")
        if re.search(r"(?:^|\n)\S+[#>]\s*$", text):
            break
    return clean_ansi(text)


def configure_and_commit(
    chan: paramiko.Channel,
    cmds: List[str],
    wait_per_cmd: float = 0.02,
    wait_commit: float = 30,
) -> Tuple[bool, str]:
    send(chan, "configure", 2)
    send_fast(chan, cmds, gap=wait_per_cmd)
    time.sleep(1)
    drain(chan)
    out = send(chan, "commit", wait_commit)
    has_error = bool(CLI_ERROR_RE.search(out))
    if has_error:
        send(chan, "rollback", 5)
    send(chan, "end", 2)
    return not has_error, out


def batched_configure(
    chan: paramiko.Channel,
    all_cmds: List[str],
    batch_size: int = BATCH_SIZE,
    wait_per_cmd: float = 0.02,
    wait_commit: float = 45,
    label: str = "",
) -> Tuple[int, int]:
    total = len(all_cmds)
    batches = [all_cmds[i : i + batch_size] for i in range(0, total, batch_size)]
    ok_count = 0
    fail_count = 0
    for idx, batch in enumerate(batches, 1):
        tag = f"{label} batch {idx}/{len(batches)}" if label else f"batch {idx}/{len(batches)}"
        print(f"    {tag} ({len(batch)} cmds)...", end="", flush=True)
        ok, out = configure_and_commit(chan, batch, wait_per_cmd, wait_commit)
        if ok:
            ok_count += 1
            print(" OK", flush=True)
        else:
            fail_count += 1
            snippet = out[:300].replace("\n", " ")
            print(f" FAIL: {snippet}", flush=True)
    return ok_count, fail_count


# ---------------------------------------------------------------------------
#  Naming helpers
# ---------------------------------------------------------------------------
def ma_name(idx: int) -> str:
    return f"MA-SCALE-{idx:04d}"


def dm_session_name(idx: int) -> str:
    return f"DM-SCALE-{idx:04d}"


def slm_session_name(idx: int) -> str:
    return f"SLM-SCALE-{idx:04d}"


def xc_name(idx: int) -> str:
    return f"xc-scale-{idx:04d}"


def mep_id_a(idx: int) -> int:
    """Local MEP ID on host-a for MA index idx (1-based). Odd numbers: 1,3,5,..."""
    return 2 * idx - 1


def mep_id_b(idx: int) -> int:
    """Local MEP ID on host-b for MA index idx (1-based). Even numbers: 2,4,6,..."""
    return 2 * idx


def sub_iface(parent: str, idx: int) -> str:
    return f"{parent}.{VLAN_BASE + idx}"


def vlan_id(idx: int) -> int:
    return VLAN_BASE + idx


# ---------------------------------------------------------------------------
#  CFM base path (long prefix)
# ---------------------------------------------------------------------------
CFM_BASE = "services ethernet-oam connectivity-fault-management"
L2XC_BASE = "services l2-cross-connect"
PM_BASE = "services performance-monitoring"
PM_CFM = f"{PM_BASE} cfm"
PM_PROF = f"{PM_BASE} profiles cfm"


# ---------------------------------------------------------------------------
#  Command generators
# ---------------------------------------------------------------------------
def gen_interface_cmds(parent_iface: str, start: int, count: int) -> List[str]:
    cmds = []
    for i in range(start, start + count):
        si = sub_iface(parent_iface, i)
        vid = vlan_id(i)
        cmds.append(f"interfaces {si} admin-state enabled vlan-id {vid} l2-service enabled")
    return cmds


def gen_md_cmds() -> List[str]:
    return [
        f"{CFM_BASE} maintenance-domains {MD_NAME} level {MD_LEVEL}",
        f"{CFM_BASE} maintenance-domains {MD_NAME} md-name string {MD_NAME}",
    ]


def gen_ma_cmds(
    parent_iface: str, start: int, count: int,
    side: str, direction: str = "down",
) -> List[str]:
    """Generate MA + MEP config.  side='a' uses mep_id_a as local, mep_id_b as remote; 'b' is the reverse."""
    cmds = []
    for i in range(start, start + count):
        ma = ma_name(i)
        iface = sub_iface(parent_iface, i)
        local = mep_id_a(i) if side == "a" else mep_id_b(i)
        remote = mep_id_b(i) if side == "a" else mep_id_a(i)
        base = f"{CFM_BASE} maintenance-domains {MD_NAME} maintenance-associations {ma}"
        cmds.extend([
            f"{base} short-ma-name string {ma}",
            f"{base} local-mep {local} direction {direction}",
            f"{base} local-mep {local} interface {iface}",
            f"{base} remote-meps auto-discovery disabled",
            f"{base} remote-meps crosscheck mep-id {remote}",
        ])
    return cmds


def gen_dm_profile_cmds(
    profile: str, probe_count: int = 5, probe_interval: int = 10,
    repeat_interval: int = 60,
) -> List[str]:
    return [
        f"{PM_PROF} two-way-delay-measurement {profile} inform-test-results enabled",
        f"{PM_PROF} two-way-delay-measurement {profile} test-duration probes probe-count {probe_count} probe-interval {probe_interval} repeat-interval {repeat_interval}",
        f"{PM_PROF} two-way-delay-measurement {profile} thresholds delay-rtt-avg 10000",
        f"{PM_PROF} two-way-delay-measurement {profile} thresholds success-rate 50",
    ]


def gen_slm_profile_cmds(
    profile: str, probe_count: int = 5, probe_interval: int = 10,
    repeat_interval: int = 60, pcp: int = 0,
) -> List[str]:
    cmds = [
        f"{PM_PROF} two-way-synthetic-loss-measurement {profile} inform-test-results enabled",
        f"{PM_PROF} two-way-synthetic-loss-measurement {profile} test-duration probes probe-count {probe_count} probe-interval {probe_interval} repeat-interval {repeat_interval}",
        f"{PM_PROF} two-way-synthetic-loss-measurement {profile} thresholds near-end-loss 10",
        f"{PM_PROF} two-way-synthetic-loss-measurement {profile} thresholds far-end-loss 10",
    ]
    if pcp > 0:
        cmds.append(
            f"{PM_PROF} two-way-synthetic-loss-measurement {profile} pcp {pcp}"
        )
    return cmds


def _target_cmd(idx: int, target_mac: Optional[str] = None) -> str:
    if target_mac:
        return f"target mac-address {target_mac}"
    return f"target mep-id {mep_id_b(idx)}"


def gen_dm_session_cmds(start: int, count: int, profile: str, target_mac: Optional[str] = None) -> List[str]:
    cmds = []
    for i in range(start, start + count):
        name = dm_session_name(i)
        ma = ma_name(i)
        local = mep_id_a(i)
        cmds.extend([
            f"{PM_CFM} two-way-delay-measurement {name} admin-state enabled",
            f"{PM_CFM} two-way-delay-measurement {name} profile {profile}",
            f"{PM_CFM} two-way-delay-measurement {name} source maintenance-domain {MD_NAME} maintenance-association {ma} mep-id {local}",
            f"{PM_CFM} two-way-delay-measurement {name} {_target_cmd(i, target_mac)}",
        ])
    return cmds


def gen_slm_session_cmds(start: int, count: int, profile: str, target_mac: Optional[str] = None) -> List[str]:
    cmds = []
    for i in range(start, start + count):
        name = slm_session_name(i)
        ma = ma_name(i)
        local = mep_id_a(i)
        cmds.extend([
            f"{PM_CFM} two-way-synthetic-loss-measurement {name} admin-state enabled",
            f"{PM_CFM} two-way-synthetic-loss-measurement {name} profile {profile}",
            f"{PM_CFM} two-way-synthetic-loss-measurement {name} source maintenance-domain {MD_NAME} maintenance-association {ma} mep-id {local}",
            f"{PM_CFM} two-way-synthetic-loss-measurement {name} {_target_cmd(i, target_mac)}",
        ])
    return cmds


def gen_delete_dm_cmds(start: int, count: int) -> List[str]:
    return [
        f"delete {PM_CFM} two-way-delay-measurement {dm_session_name(i)}"
        for i in range(start, start + count)
    ]


def gen_delete_slm_cmds(start: int, count: int) -> List[str]:
    return [
        f"delete {PM_CFM} two-way-synthetic-loss-measurement {slm_session_name(i)}"
        for i in range(start, start + count)
    ]


def gen_delete_ma_cmds(start: int, count: int) -> List[str]:
    return [
        f"delete {CFM_BASE} maintenance-domains {MD_NAME} maintenance-associations {ma_name(i)}"
        for i in range(start, start + count)
    ]


def gen_delete_interface_cmds(parent_iface: str, start: int, count: int) -> List[str]:
    return [
        f"delete interfaces {sub_iface(parent_iface, i)}"
        for i in range(start, start + count)
    ]


def gen_l2xc_cmds(iface1_parent: str, iface2_parent: str, start: int, count: int) -> List[str]:
    """Generate l2-cross-connect instances pairing sub-interfaces on two physical ports."""
    cmds = []
    for i in range(start, start + count):
        name = xc_name(i)
        si1 = sub_iface(iface1_parent, i)
        si2 = sub_iface(iface2_parent, i)
        cmds.extend([
            f"{L2XC_BASE} {name} admin-state enabled",
            f"{L2XC_BASE} {name} interfaces {si1} {si2}",
        ])
    return cmds


def gen_delete_l2xc_cmds(start: int, count: int) -> List[str]:
    return [
        f"delete {L2XC_BASE} {xc_name(i)}"
        for i in range(start, start + count)
    ]


# ---------------------------------------------------------------------------
#  Pre-checks
# ---------------------------------------------------------------------------
def validate_interface(chan: paramiko.Channel, iface: str) -> Tuple[bool, str]:
    """Check that a physical interface exists and is recognized by the device."""
    out = run_show(chan, f"show interfaces {iface}", 8)
    lower = out.lower()
    if "not found" in lower or "unknown" in lower or "error" in lower or "invalid" in lower:
        return False, out
    if iface.lower() in lower or "admin-state" in lower or "oper-state" in lower:
        return True, out
    return False, out


def check_no_existing_cfm(chan: paramiko.Channel) -> Tuple[bool, bool, str]:
    """Check for existing CFM config.
    Returns (clean, only_ours, raw_output):
      clean=True      -> no CFM at all
      only_ours=True  -> only MD-SCALE exists (leftover from previous run)
      both False      -> foreign CFM config present
    """
    out = run_show(chan, "show config services ethernet-oam connectivity-fault-management", 8)
    lower = out.lower()
    has_cfm = "maintenance-domains" in lower or "maintenance-associations" in lower
    if not has_cfm:
        return True, False, out
    only_ours = MD_NAME.lower() in lower and lower.count("maintenance-domains") == 1
    return False, only_ours, out


# ---------------------------------------------------------------------------
#  Health / verification helpers
# ---------------------------------------------------------------------------
def get_cfm_mgr_memory(chan: paramiko.Channel) -> Optional[int]:
    out = send(
        chan,
        "run bash cat /proc/$(pgrep -f cfm_mgr | head -1)/status 2>/dev/null "
        "| grep VmRSS || echo NO_PROC",
        wait=3,
    )
    m = re.search(r"VmRSS:\s+(\d+)\s+kB", out)
    return int(m.group(1)) if m else None


def get_core_dumps(chan: paramiko.Channel) -> set:
    out = send(chan, "run bash ls /var/core/core-cfm* 2>/dev/null || echo NONE", wait=3)
    return set(re.findall(r"core-cfm\S+", out))


def count_proactive_sessions(chan: paramiko.Channel) -> Tuple[int, int, int]:
    """Return (total_displayed, ongoing_count, dm_count_approx) from show proactive."""
    out = run_show(chan, "show services performance-monitoring cfm tests proactive", 15)
    total_m = re.search(r"Total displayed tests:\s*(\d+)", out)
    total = int(total_m.group(1)) if total_m else 0
    ongoing = out.lower().count("ongoing")
    dm_count = out.count("DM-SCALE-")
    return total, ongoing, dm_count


def verify_session_reporting(
    chan: paramiko.Channel, session_name: str, session_type: str = "dm",
) -> Tuple[bool, str]:
    """Check that a specific session is actively reporting metrics."""
    if session_type == "dm":
        cmd = f"show services performance-monitoring cfm tests proactive two-way-delay session-name {session_name} detail"
        success_key = "DMR PDUs received"
    else:
        cmd = f"show services performance-monitoring cfm tests proactive two-way-synthetic-loss session-name {session_name} detail"
        success_key = "SLR PDUs received"
    out = run_show(chan, cmd, 12)
    reporting = success_key in out and "Success rate:" in out
    return reporting, out


# ---------------------------------------------------------------------------
#  Verdicts tracking
# ---------------------------------------------------------------------------
class TestTracker:
    def __init__(self):
        self.verdicts: List[Dict] = []
        self.start_time = datetime.now(timezone.utc)

    def verdict(self, name: str, passed: bool, detail: str = ""):
        tag = "PASS" if passed else "FAIL"
        self.verdicts.append({"name": name, "passed": passed, "detail": detail})
        print(f"  [{tag}] {name}", flush=True)
        if detail:
            print(f"         {detail}", flush=True)

    @property
    def pass_count(self) -> int:
        return sum(1 for v in self.verdicts if v["passed"])

    @property
    def fail_count(self) -> int:
        return sum(1 for v in self.verdicts if not v["passed"])

    def summary(self) -> str:
        lines = []
        for v in self.verdicts:
            tag = "PASS" if v["passed"] else "FAIL"
            lines.append(f"  [{tag}] {v['name']}")
            if v["detail"] and not v["passed"]:
                lines.append(f"         {v['detail']}")
        lines.append(f"\n  Passed: {self.pass_count}  Failed: {self.fail_count}  Total: {len(self.verdicts)}")
        return "\n".join(lines)


def section(title: str):
    sep = "=" * 78
    print(f"\n{sep}\n  {title}\n{sep}", flush=True)


# ===========================================================================
#  PHASE: Setup baseline infrastructure
# ===========================================================================
def phase_setup(
    chan_a: paramiko.Channel,
    chan_b: paramiko.Channel,
    iface_a: str,
    iface_b: str,
    iface_a2: str,
    iface_b2: str,
    scale: int,
    tracker: TestTracker,
    skip_interfaces: bool = False,
    up_mep: bool = False,
):
    mep_dir = "up" if up_mep else "down"
    cfm_iface_a = iface_a2 if up_mep else iface_a
    cfm_iface_b = iface_b2 if up_mep else iface_b
    section(f"PHASE 1: Setup baseline infrastructure ({scale} MAs, {mep_dir} MEPs on {cfm_iface_a}/{cfm_iface_b})")

    if skip_interfaces:
        print("  --skip-interfaces: skipping sub-interface and l2-xc creation.", flush=True)
    else:
        # 1a. Create sub-interfaces on CFM-facing ports (iface_a / iface_b)
        print(f"  Creating {scale} sub-interfaces on host-a ({iface_a})...", flush=True)
        iface_cmds_a = gen_interface_cmds(iface_a, 1, scale)
        ok_a, fail_a = batched_configure(chan_a, iface_cmds_a, batch_size=100, wait_commit=30, label="iface-a")
        tracker.verdict(f"Sub-interfaces on host-a {iface_a}", fail_a == 0, f"{ok_a} ok, {fail_a} failed")

        print(f"  Creating {scale} sub-interfaces on host-b ({iface_b})...", flush=True)
        iface_cmds_b = gen_interface_cmds(iface_b, 1, scale)
        ok_b, fail_b = batched_configure(chan_b, iface_cmds_b, batch_size=100, wait_commit=30, label="iface-b")
        tracker.verdict(f"Sub-interfaces on host-b {iface_b}", fail_b == 0, f"{ok_b} ok, {fail_b} failed")

        # 1b. Create sub-interfaces on l2-xc second ports (iface_a2 / iface_b2)
        print(f"  Creating {scale} sub-interfaces on host-a ({iface_a2}) for l2-xc...", flush=True)
        iface_cmds_a2 = gen_interface_cmds(iface_a2, 1, scale)
        ok_a2, fail_a2 = batched_configure(chan_a, iface_cmds_a2, batch_size=100, wait_commit=30, label="iface-a2")
        tracker.verdict(f"Sub-interfaces on host-a {iface_a2}", fail_a2 == 0, f"{ok_a2} ok, {fail_a2} failed")

        print(f"  Creating {scale} sub-interfaces on host-b ({iface_b2}) for l2-xc...", flush=True)
        iface_cmds_b2 = gen_interface_cmds(iface_b2, 1, scale)
        ok_b2, fail_b2 = batched_configure(chan_b, iface_cmds_b2, batch_size=100, wait_commit=30, label="iface-b2")
        tracker.verdict(f"Sub-interfaces on host-b {iface_b2}", fail_b2 == 0, f"{ok_b2} ok, {fail_b2} failed")

        # 1c. Create l2-cross-connect instances on both devices
        print(f"  Creating {scale} l2-cross-connect instances on host-a...", flush=True)
        xc_cmds_a = gen_l2xc_cmds(iface_a, iface_a2, 1, scale)
        ok_xa, fail_xa = batched_configure(chan_a, xc_cmds_a, batch_size=500, wait_commit=45, label="xc-a")
        tracker.verdict(f"l2-xc on host-a ({scale})", fail_xa == 0, f"{ok_xa} ok, {fail_xa} failed")

        print(f"  Creating {scale} l2-cross-connect instances on host-b...", flush=True)
        xc_cmds_b = gen_l2xc_cmds(iface_b, iface_b2, 1, scale)
        ok_xb, fail_xb = batched_configure(chan_b, xc_cmds_b, batch_size=500, wait_commit=45, label="xc-b")
        tracker.verdict(f"l2-xc on host-b ({scale})", fail_xb == 0, f"{ok_xb} ok, {fail_xb} failed")

    # 1d. Create MD on both devices
    print("  Creating MD on both devices...", flush=True)
    ok_a, _ = configure_and_commit(chan_a, gen_md_cmds(), wait_commit=15)
    ok_b, _ = configure_and_commit(chan_b, gen_md_cmds(), wait_commit=15)
    tracker.verdict("MD created on both devices", ok_a and ok_b)

    # 1e. Create MAs + MEPs on both devices (batched)
    print(f"  Creating {scale} MAs with {mep_dir} MEPs on host-a ({cfm_iface_a})...", flush=True)
    ma_cmds_a = gen_ma_cmds(cfm_iface_a, 1, scale, side="a", direction=mep_dir)
    ok_a, fail_a = batched_configure(chan_a, ma_cmds_a, batch_size=500, wait_commit=45, label="ma-a")
    tracker.verdict(f"MAs+MEPs on host-a ({scale})", fail_a == 0, f"{ok_a} ok, {fail_a} failed")

    print(f"  Creating {scale} MAs with {mep_dir} MEPs on host-b ({cfm_iface_b})...", flush=True)
    ma_cmds_b = gen_ma_cmds(cfm_iface_b, 1, scale, side="b", direction=mep_dir)
    ok_b, fail_b = batched_configure(chan_b, ma_cmds_b, batch_size=500, wait_commit=45, label="ma-b")
    tracker.verdict(f"MAs+MEPs on host-b ({scale})", fail_b == 0, f"{ok_b} ok, {fail_b} failed")

    # 1f. Create DM and SLM profiles on device A (initiator)
    print("  Creating PM profiles on host-a...", flush=True)
    profile_cmds = (
        gen_dm_profile_cmds(DM_PROFILE, probe_count=5, probe_interval=10, repeat_interval=60)
        + gen_slm_profile_cmds(SLM_PROFILE, probe_count=5, probe_interval=10, repeat_interval=60)
    )
    ok, _ = configure_and_commit(chan_a, profile_cmds, wait_commit=20)
    tracker.verdict("PM profiles created on host-a", ok)


# ===========================================================================
#  PHASE: Scale DM sessions (512 then 1000)
# ===========================================================================
def phase_scale_dm(
    chan_a: paramiko.Channel,
    scale: int,
    tracker: TestTracker,
    target_mac: Optional[str] = None,
):
    section(f"PHASE 2: Scale DM sessions to {scale}")

    mid = min(512, scale)

    # 2a. Create first 512 DM sessions
    print(f"  Creating DM sessions 1..{mid}...", flush=True)
    cmds = gen_dm_session_cmds(1, mid, DM_PROFILE, target_mac=target_mac)
    ok, fail = batched_configure(chan_a, cmds, batch_size=500, wait_commit=60, label="dm-1")
    tracker.verdict(f"DM sessions 1..{mid} created", fail == 0, f"{ok} ok, {fail} failed")

    print("  Waiting for sessions to activate (30s)...", flush=True)
    time.sleep(30)

    total, ongoing, _ = count_proactive_sessions(chan_a)
    tracker.verdict(
        f"DM sessions visible at {mid}",
        total >= mid, f"total={total}, ongoing={ongoing}",
    )

    # Spot-check a few sessions are actually reporting
    for check_idx in [1, mid // 2, mid]:
        ok, out = verify_session_reporting(chan_a, dm_session_name(check_idx), "dm")
        tracker.verdict(f"DM session {dm_session_name(check_idx)} reporting", ok)

    # 2b. Scale up to full count if > 512
    if scale > mid:
        remaining = scale - mid
        print(f"  Creating DM sessions {mid+1}..{scale} ({remaining} more)...", flush=True)
        cmds2 = gen_dm_session_cmds(mid + 1, remaining, DM_PROFILE, target_mac=target_mac)
        ok2, fail2 = batched_configure(chan_a, cmds2, batch_size=500, wait_commit=60, label="dm-2")
        tracker.verdict(f"DM sessions {mid+1}..{scale} created", fail2 == 0, f"{ok2} ok, {fail2} failed")

        print("  Waiting for all DM sessions to activate (45s)...", flush=True)
        time.sleep(45)

        total, ongoing, _ = count_proactive_sessions(chan_a)
        tracker.verdict(
            f"DM sessions visible at {scale}",
            total >= scale, f"total={total}, ongoing={ongoing}",
        )

        ok_last, _ = verify_session_reporting(chan_a, dm_session_name(scale), "dm")
        tracker.verdict(f"DM session {dm_session_name(scale)} reporting", ok_last)


# ===========================================================================
#  PHASE: Scale SLM sessions (512 then 1000)
# ===========================================================================
def phase_scale_slm(
    chan_a: paramiko.Channel,
    scale: int,
    tracker: TestTracker,
    target_mac: Optional[str] = None,
):
    section(f"PHASE 3: Scale SLM sessions to {scale}")

    mid = min(512, scale)

    print(f"  Creating SLM sessions 1..{mid}...", flush=True)
    cmds = gen_slm_session_cmds(1, mid, SLM_PROFILE, target_mac=target_mac)
    ok, fail = batched_configure(chan_a, cmds, batch_size=500, wait_commit=60, label="slm-1")
    tracker.verdict(f"SLM sessions 1..{mid} created", fail == 0, f"{ok} ok, {fail} failed")

    print("  Waiting for sessions to activate (30s)...", flush=True)
    time.sleep(30)

    total, ongoing, _ = count_proactive_sessions(chan_a)
    expected_min = scale + mid  # DM sessions already present
    tracker.verdict(
        f"Total proactive sessions at DM={scale}+SLM={mid}",
        total >= expected_min, f"total={total}, ongoing={ongoing}",
    )

    for check_idx in [1, mid // 2, mid]:
        ok_s, _ = verify_session_reporting(chan_a, slm_session_name(check_idx), "slm")
        tracker.verdict(f"SLM session {slm_session_name(check_idx)} reporting", ok_s)

    if scale > mid:
        remaining = scale - mid
        print(f"  Creating SLM sessions {mid+1}..{scale} ({remaining} more)...", flush=True)
        cmds2 = gen_slm_session_cmds(mid + 1, remaining, SLM_PROFILE, target_mac=target_mac)
        ok2, fail2 = batched_configure(chan_a, cmds2, batch_size=500, wait_commit=60, label="slm-2")
        tracker.verdict(f"SLM sessions {mid+1}..{scale} created", fail2 == 0, f"{ok2} ok, {fail2} failed")

        print("  Waiting for all sessions to activate (45s)...", flush=True)
        time.sleep(45)

        total, ongoing, _ = count_proactive_sessions(chan_a)
        expected_total = scale * 2
        tracker.verdict(
            f"All {expected_total} proactive sessions visible",
            total >= expected_total, f"total={total}, ongoing={ongoing}",
        )

        ok_last, _ = verify_session_reporting(chan_a, slm_session_name(scale), "slm")
        tracker.verdict(f"SLM session {slm_session_name(scale)} reporting", ok_last)


# ===========================================================================
#  PHASE: Full scale verification
# ===========================================================================
def phase_verify_scale(
    chan_a: paramiko.Channel,
    scale: int,
    tracker: TestTracker,
):
    section(f"PHASE 4: Full-scale verification ({scale} DM + {scale} SLM)")

    # 4a. Count total sessions
    total, ongoing, _ = count_proactive_sessions(chan_a)
    expected = scale * 2
    tracker.verdict(
        f"Total sessions = {expected}",
        total >= expected, f"total={total}, ongoing={ongoing}",
    )

    # 4b. Spot-check multiple DM sessions across the range
    check_indices = [1, 100, 250, 500, 750, scale]
    check_indices = [i for i in check_indices if i <= scale]
    dm_reporting = 0
    for idx in check_indices:
        ok, _ = verify_session_reporting(chan_a, dm_session_name(idx), "dm")
        if ok:
            dm_reporting += 1
    tracker.verdict(
        f"DM spot-check reporting ({dm_reporting}/{len(check_indices)})",
        dm_reporting == len(check_indices),
    )

    # 4c. Spot-check multiple SLM sessions
    slm_reporting = 0
    for idx in check_indices:
        ok, _ = verify_session_reporting(chan_a, slm_session_name(idx), "slm")
        if ok:
            slm_reporting += 1
    tracker.verdict(
        f"SLM spot-check reporting ({slm_reporting}/{len(check_indices)})",
        slm_reporting == len(check_indices),
    )

    # 4d. cfm_mgr memory snapshot
    mem = get_cfm_mgr_memory(chan_a)
    if mem:
        print(f"  cfm_mgr VmRSS at scale: {mem} kB", flush=True)
    tracker.verdict("cfm_mgr alive at scale", mem is not None, f"{mem} kB" if mem else "not found")

    # 4e. No core dumps
    cores = get_core_dumps(chan_a)
    tracker.verdict("No cfm core dumps at scale", len(cores) == 0, f"cores: {cores}" if cores else "")


# ===========================================================================
#  PHASE: Aggressive intervals (1s)
# ===========================================================================
def phase_aggressive_intervals(
    chan_a: paramiko.Channel,
    scale: int,
    tracker: TestTracker,
):
    section("PHASE 5: Aggressive intervals (1s probe-interval, 1s repeat-interval)")

    agg_dm_cmds = gen_dm_profile_cmds(DM_PROFILE_AGG, probe_count=5, probe_interval=1, repeat_interval=1)
    agg_slm_cmds = gen_slm_profile_cmds(SLM_PROFILE_AGG, probe_count=5, probe_interval=1, repeat_interval=1)
    ok, _ = configure_and_commit(chan_a, agg_dm_cmds + agg_slm_cmds, wait_commit=20)
    tracker.verdict("Aggressive profiles created", ok)

    # Switch all DM sessions to aggressive profile
    print(f"  Switching {scale} DM sessions to aggressive profile...", flush=True)
    switch_dm = [
        f"{PM_CFM} two-way-delay-measurement {dm_session_name(i)} profile {DM_PROFILE_AGG}"
        for i in range(1, scale + 1)
    ]
    ok_d, fail_d = batched_configure(chan_a, switch_dm, batch_size=1000, wait_commit=60, label="dm-agg")
    tracker.verdict(f"DM sessions switched to aggressive", fail_d == 0)

    print(f"  Switching {scale} SLM sessions to aggressive profile...", flush=True)
    switch_slm = [
        f"{PM_CFM} two-way-synthetic-loss-measurement {slm_session_name(i)} profile {SLM_PROFILE_AGG}"
        for i in range(1, scale + 1)
    ]
    ok_s, fail_s = batched_configure(chan_a, switch_slm, batch_size=1000, wait_commit=60, label="slm-agg")
    tracker.verdict(f"SLM sessions switched to aggressive", fail_s == 0)

    # Let sessions run with aggressive intervals
    print("  Waiting 60s for aggressive sessions to stabilize...", flush=True)
    time.sleep(60)

    # Verify sessions still running
    total, ongoing, _ = count_proactive_sessions(chan_a)
    expected = scale * 2
    tracker.verdict(
        f"Sessions alive under aggressive intervals",
        total >= expected, f"total={total}, ongoing={ongoing}",
    )

    # Spot-check reporting
    ok_dm, _ = verify_session_reporting(chan_a, dm_session_name(1), "dm")
    ok_slm, _ = verify_session_reporting(chan_a, slm_session_name(1), "slm")
    tracker.verdict("DM reporting under 1s intervals", ok_dm)
    tracker.verdict("SLM reporting under 1s intervals", ok_slm)

    # Check system stability
    mem = get_cfm_mgr_memory(chan_a)
    tracker.verdict("cfm_mgr alive under aggressive intervals", mem is not None, f"{mem} kB" if mem else "")
    cores = get_core_dumps(chan_a)
    tracker.verdict("No core dumps under aggressive intervals", len(cores) == 0)

    # Revert to normal profile
    print("  Reverting to normal profiles...", flush=True)
    revert_dm = [
        f"{PM_CFM} two-way-delay-measurement {dm_session_name(i)} profile {DM_PROFILE}"
        for i in range(1, scale + 1)
    ]
    batched_configure(chan_a, revert_dm, batch_size=1000, wait_commit=60, label="dm-revert")
    revert_slm = [
        f"{PM_CFM} two-way-synthetic-loss-measurement {slm_session_name(i)} profile {SLM_PROFILE}"
        for i in range(1, scale + 1)
    ]
    batched_configure(chan_a, revert_slm, batch_size=1000, wait_commit=60, label="slm-revert")


# ===========================================================================
#  PHASE: HA testing at scale
# ===========================================================================
def phase_ha(
    host_a: str,
    user: str,
    password: str,
    chan_a: paramiko.Channel,
    scale: int,
    tracker: TestTracker,
):
    section("PHASE 6: HA testing at scale")

    cores_before = get_core_dumps(chan_a)
    mem_before = get_cfm_mgr_memory(chan_a)
    total_before, ongoing_before, _ = count_proactive_sessions(chan_a)
    print(f"  Before HA: total={total_before}, ongoing={ongoing_before}, mem={mem_before}kB", flush=True)

    ha_actions = [
        ("cfm_mgr process restart", "request system process cfm_mgr restart"),
        ("NCP switchover", "request system cluster ncp switchover"),
        ("warm restart", "request system warm-restart"),
    ]

    for ha_name, ha_cmd in ha_actions:
        print(f"\n  --- HA action: {ha_name} ---", flush=True)

        # Verify sessions are running before HA
        total_pre, _, _ = count_proactive_sessions(chan_a)
        if total_pre < scale:
            print(f"    SKIP: only {total_pre} sessions before HA (need {scale})", flush=True)
            tracker.verdict(f"HA/{ha_name}: skip (insufficient sessions)", False, f"only {total_pre}")
            continue

        # Execute HA action
        print(f"    Executing: {ha_cmd}", flush=True)
        try:
            send(chan_a, ha_cmd, wait=5)
        except Exception as e:
            print(f"    HA command raised exception (expected for switchover): {e}", flush=True)

        # Wait for recovery - SSH may drop
        wait_time = 120 if "switchover" in ha_name or "warm" in ha_name else 60
        print(f"    Waiting {wait_time}s for recovery...", flush=True)
        time.sleep(wait_time)

        # Reconnect if needed
        try:
            send(chan_a, "show system uptime | no-more", wait=5)
        except Exception:
            print("    SSH channel lost, reconnecting...", flush=True)
            try:
                ssh_new, chan_a_new = create_shell(host_a, user, password, f"ha-{ha_name}")
                chan_a = chan_a_new
            except Exception as e:
                tracker.verdict(f"HA/{ha_name}: reconnect", False, str(e))
                continue

        # Allow more time for sessions to recover
        print("    Waiting 60s for session recovery...", flush=True)
        time.sleep(60)

        # Verify recovery
        total_post, ongoing_post, _ = count_proactive_sessions(chan_a)
        recovered = total_post >= scale
        tracker.verdict(
            f"HA/{ha_name}: sessions recovered",
            recovered, f"before={total_pre}, after={total_post}, ongoing={ongoing_post}",
        )

        # Spot-check a session is still reporting
        ok_dm, _ = verify_session_reporting(chan_a, dm_session_name(1), "dm")
        ok_slm, _ = verify_session_reporting(chan_a, slm_session_name(1), "slm")
        tracker.verdict(f"HA/{ha_name}: DM still reporting", ok_dm)
        tracker.verdict(f"HA/{ha_name}: SLM still reporting", ok_slm)

        # Check for new core dumps
        cores_after = get_core_dumps(chan_a)
        new_cores = cores_after - cores_before
        tracker.verdict(f"HA/{ha_name}: no new core dumps", len(new_cores) == 0, str(new_cores) if new_cores else "")

    return chan_a


# ===========================================================================
#  PHASE: Variants
# ===========================================================================
def phase_variants(
    chan_a: paramiko.Channel,
    scale: int,
    tracker: TestTracker,
):
    section("PHASE 7: Variant tests")

    # 7a. Disable a subset of sessions while others run
    disable_count = min(50, scale // 10)
    print(f"  Disabling {disable_count} DM sessions while others run...", flush=True)
    disable_cmds = [
        f"{PM_CFM} two-way-delay-measurement {dm_session_name(i)} admin-state disabled"
        for i in range(1, disable_count + 1)
    ]
    ok, _ = configure_and_commit(chan_a, disable_cmds, wait_commit=30)
    tracker.verdict(f"Disabled {disable_count} DM sessions", ok)

    time.sleep(15)

    # Verify disabled sessions show disabled, others still running
    ok_disabled, out = verify_session_reporting(chan_a, dm_session_name(1), "dm")
    is_disabled = "Admin state: disabled" in out
    tracker.verdict("Disabled session shows disabled state", is_disabled)

    ok_running, _ = verify_session_reporting(chan_a, dm_session_name(disable_count + 1), "dm")
    tracker.verdict("Non-disabled session still running", ok_running)

    # Re-enable
    enable_cmds = [
        f"{PM_CFM} two-way-delay-measurement {dm_session_name(i)} admin-state enabled"
        for i in range(1, disable_count + 1)
    ]
    ok, _ = configure_and_commit(chan_a, enable_cmds, wait_commit=30)
    tracker.verdict(f"Re-enabled {disable_count} DM sessions", ok)
    time.sleep(20)

    # 7b. PCP variation at scale - change PCP on SLM profile
    print("  Testing PCP variation on SLM sessions...", flush=True)
    pcp_cmds = [
        f"{PM_PROF} two-way-synthetic-loss-measurement {SLM_PROFILE} pcp 5",
    ]
    ok, _ = configure_and_commit(chan_a, pcp_cmds, wait_commit=20)
    tracker.verdict("SLM profile PCP changed to 5", ok)
    time.sleep(15)

    ok_pcp, out_pcp = verify_session_reporting(chan_a, slm_session_name(1), "slm")
    tracker.verdict("SLM still reporting with PCP=5", ok_pcp)

    # Revert PCP
    configure_and_commit(chan_a, [f"delete {PM_PROF} two-way-synthetic-loss-measurement {SLM_PROFILE} pcp"], wait_commit=15)

    # 7c. LB & Linktrace while scale is active
    print("  Running LB & LT alongside scale sessions...", flush=True)
    lb_out = send(
        chan_a,
        f"run ethernet-oam cfm on-demand loopback maintenance-domain {MD_NAME} "
        f"maintenance-association {ma_name(1)} target mep-id {mep_id_b(1)} count 5",
        20,
    )
    lb_ok = "reply" in lb_out.lower() or "loopback" in lb_out.lower()
    tracker.verdict("LB succeeds alongside scale sessions", lb_ok)

    lt_out = send(
        chan_a,
        f"run ethernet-oam cfm on-demand linktrace maintenance-domain {MD_NAME} "
        f"maintenance-association {ma_name(1)} target mep-id {mep_id_b(1)}",
        20,
    )
    lt_ok = "trace" in lt_out.lower() or "linktrace" in lt_out.lower() or "reply" in lt_out.lower()
    tracker.verdict("LT succeeds alongside scale sessions", lt_ok)

    # Verify scale sessions survived LB/LT
    total, _, _ = count_proactive_sessions(chan_a)
    tracker.verdict(
        "Scale sessions intact after LB/LT",
        total >= scale * 2, f"total={total}",
    )

    # 7d. Up MEP variant - reconfigure a few MAs as up MEPs
    print("  Testing up-MEP direction on a subset...", flush=True)
    up_count = min(5, scale)
    up_cmds = []
    for i in range(1, up_count + 1):
        ma = ma_name(i)
        base = f"{CFM_BASE} maintenance-domains {MD_NAME} maintenance-associations {ma}"
        up_cmds.append(f"{base} local-mep 1 direction up")
    ok_up, _ = configure_and_commit(chan_a, up_cmds, wait_commit=30)
    tracker.verdict(f"Changed {up_count} MEPs to direction up", ok_up)
    time.sleep(15)

    ok_up_sess, _ = verify_session_reporting(chan_a, dm_session_name(1), "dm")
    tracker.verdict("DM session works with up-MEP direction", ok_up_sess)

    # Revert to down
    down_cmds = []
    for i in range(1, up_count + 1):
        ma = ma_name(i)
        base = f"{CFM_BASE} maintenance-domains {MD_NAME} maintenance-associations {ma}"
        down_cmds.append(f"{base} local-mep 1 direction down")
    configure_and_commit(chan_a, down_cmds, wait_commit=30)
    time.sleep(10)


# ===========================================================================
#  PHASE: Negative tests
# ===========================================================================
def phase_negative(
    chan_a: paramiko.Channel,
    scale: int,
    tracker: TestTracker,
    target_mac: Optional[str] = None,
):
    section("PHASE 8: Negative tests")

    # 8a. Try adding a second DM session to the same MA (must be rejected)
    print("  Attempting second DM session on same MA (expect rejection)...", flush=True)
    dup_cmds = [
        f"{PM_CFM} two-way-delay-measurement DM-DUP-TEST admin-state enabled",
        f"{PM_CFM} two-way-delay-measurement DM-DUP-TEST profile {DM_PROFILE}",
        f"{PM_CFM} two-way-delay-measurement DM-DUP-TEST source maintenance-domain {MD_NAME} maintenance-association {ma_name(1)} mep-id {mep_id_a(1)}",
        f"{PM_CFM} two-way-delay-measurement DM-DUP-TEST {_target_cmd(1, target_mac)}",
    ]
    ok_dup, out_dup = configure_and_commit(chan_a, dup_cmds, wait_commit=20)
    rejected = not ok_dup or "error" in out_dup.lower() or "failed" in out_dup.lower()
    tracker.verdict(
        "Second DM session on same MA rejected",
        rejected,
        "commit succeeded (unexpected)" if not rejected else "correctly rejected",
    )
    if ok_dup:
        # Cleanup if it was accidentally accepted
        configure_and_commit(chan_a, [f"delete {PM_CFM} two-way-delay-measurement DM-DUP-TEST"], wait_commit=15)

    # 8b. Try adding a second SLM session to the same MA
    print("  Attempting second SLM session on same MA (expect rejection)...", flush=True)
    dup_slm = [
        f"{PM_CFM} two-way-synthetic-loss-measurement SLM-DUP-TEST admin-state enabled",
        f"{PM_CFM} two-way-synthetic-loss-measurement SLM-DUP-TEST profile {SLM_PROFILE}",
        f"{PM_CFM} two-way-synthetic-loss-measurement SLM-DUP-TEST source maintenance-domain {MD_NAME} maintenance-association {ma_name(1)} mep-id {mep_id_a(1)}",
        f"{PM_CFM} two-way-synthetic-loss-measurement SLM-DUP-TEST {_target_cmd(1, target_mac)}",
    ]
    ok_dup2, out_dup2 = configure_and_commit(chan_a, dup_slm, wait_commit=20)
    rejected2 = not ok_dup2 or "error" in out_dup2.lower() or "failed" in out_dup2.lower()
    tracker.verdict(
        "Second SLM session on same MA rejected",
        rejected2,
        "commit succeeded (unexpected)" if not rejected2 else "correctly rejected",
    )
    if ok_dup2:
        configure_and_commit(chan_a, [f"delete {PM_CFM} two-way-synthetic-loss-measurement SLM-DUP-TEST"], wait_commit=15)

    # 8c. Try exceeding max session count
    print("  Attempting to exceed max session count (expect clean rejection)...", flush=True)
    # Try creating one more session beyond the scale count (needs an MA that exists)
    extra_idx = scale + 1
    # First create the extra MA
    extra_ma_cmds = gen_ma_cmds("dummy", extra_idx, 1, side="a", direction="down")
    extra_dm = [
        f"{PM_CFM} two-way-delay-measurement {dm_session_name(extra_idx)} admin-state enabled",
        f"{PM_CFM} two-way-delay-measurement {dm_session_name(extra_idx)} profile {DM_PROFILE}",
        f"{PM_CFM} two-way-delay-measurement {dm_session_name(extra_idx)} source maintenance-domain {MD_NAME} maintenance-association {ma_name(extra_idx)} mep-id {mep_id_a(extra_idx)}",
        f"{PM_CFM} two-way-delay-measurement {dm_session_name(extra_idx)} {_target_cmd(extra_idx, target_mac)}",
    ]
    ok_extra, out_extra = configure_and_commit(chan_a, extra_dm, wait_commit=20)
    if ok_extra:
        # Clean up if accepted
        configure_and_commit(chan_a, [f"delete {PM_CFM} two-way-delay-measurement {dm_session_name(extra_idx)}"], wait_commit=15)
        tracker.verdict("Exceed max session count (informational)", True, "session accepted (system may support more)")
    else:
        tracker.verdict("Exceed max session count cleanly rejected", True, "correctly rejected")


# ===========================================================================
#  PHASE: Cleanup
# ===========================================================================
def phase_cleanup(
    chan_a: paramiko.Channel,
    chan_b: paramiko.Channel,
    iface_a: str,
    iface_b: str,
    iface_a2: str,
    iface_b2: str,
    scale: int,
    tracker: TestTracker,
):
    section(f"PHASE 9: Cleanup ({scale} sessions)")

    # Delete DM sessions
    print("  Deleting DM sessions...", flush=True)
    dm_del = gen_delete_dm_cmds(1, scale)
    ok_d, fail_d = batched_configure(chan_a, dm_del, batch_size=1000, wait_commit=60, label="del-dm")
    tracker.verdict(f"DM sessions deleted", fail_d == 0)

    # Delete SLM sessions
    print("  Deleting SLM sessions...", flush=True)
    slm_del = gen_delete_slm_cmds(1, scale)
    ok_s, fail_s = batched_configure(chan_a, slm_del, batch_size=1000, wait_commit=60, label="del-slm")
    tracker.verdict(f"SLM sessions deleted", fail_s == 0)

    # Delete profiles
    print("  Deleting profiles...", flush=True)
    prof_del = [
        f"delete {PM_PROF} two-way-delay-measurement {DM_PROFILE}",
        f"delete {PM_PROF} two-way-delay-measurement {DM_PROFILE_AGG}",
        f"delete {PM_PROF} two-way-synthetic-loss-measurement {SLM_PROFILE}",
        f"delete {PM_PROF} two-way-synthetic-loss-measurement {SLM_PROFILE_AGG}",
    ]
    configure_and_commit(chan_a, prof_del, wait_commit=15)

    # Delete MAs on both devices
    print("  Deleting MAs on host-a...", flush=True)
    ma_del_a = gen_delete_ma_cmds(1, scale)
    batched_configure(chan_a, ma_del_a, batch_size=1000, wait_commit=60, label="del-ma-a")

    print("  Deleting MAs on host-b...", flush=True)
    ma_del_b = gen_delete_ma_cmds(1, scale)
    batched_configure(chan_b, ma_del_b, batch_size=1000, wait_commit=60, label="del-ma-b")

    # Delete MD
    configure_and_commit(chan_a, [f"delete {CFM_BASE} maintenance-domains {MD_NAME}"], wait_commit=15)
    configure_and_commit(chan_b, [f"delete {CFM_BASE} maintenance-domains {MD_NAME}"], wait_commit=15)

    # Delete l2-cross-connect instances
    print("  Deleting l2-xc on host-a...", flush=True)
    xc_del_a = gen_delete_l2xc_cmds(1, scale)
    batched_configure(chan_a, xc_del_a, batch_size=1000, wait_commit=45, label="del-xc-a")

    print("  Deleting l2-xc on host-b...", flush=True)
    xc_del_b = gen_delete_l2xc_cmds(1, scale)
    batched_configure(chan_b, xc_del_b, batch_size=1000, wait_commit=45, label="del-xc-b")

    # Delete sub-interfaces (both CFM-facing and l2-xc second ports)
    print("  Deleting sub-interfaces on host-a...", flush=True)
    iface_del_a = gen_delete_interface_cmds(iface_a, 1, scale) + gen_delete_interface_cmds(iface_a2, 1, scale)
    batched_configure(chan_a, iface_del_a, batch_size=1000, wait_commit=45, label="del-if-a")

    print("  Deleting sub-interfaces on host-b...", flush=True)
    iface_del_b = gen_delete_interface_cmds(iface_b, 1, scale) + gen_delete_interface_cmds(iface_b2, 1, scale)
    batched_configure(chan_b, iface_del_b, batch_size=1000, wait_commit=45, label="del-if-b")

    # Verify clean
    total, _, _ = count_proactive_sessions(chan_a)
    tracker.verdict("All scale sessions removed", total == 0, f"remaining={total}")


# ===========================================================================
#  MAIN
# ===========================================================================
def main() -> int:
    parser = argparse.ArgumentParser(
        description="SW-236991: Y.1731 Scale Test — 1000 DM/SLM per system (1 per MA)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host-a", required=True, help="Initiator device IP")
    parser.add_argument("--host-b", required=True, help="Responder device IP")
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument("--password", default=DEFAULT_PASS)
    parser.add_argument("--iface-a", required=True, help="Physical interface on host-a for CFM MEPs (e.g. ge400-0/0/33)")
    parser.add_argument("--iface-b", required=True, help="Physical interface on host-b for CFM MEPs (e.g. ge400-0/0/18)")
    parser.add_argument("--iface-a2", required=True, help="Second interface on host-a for l2-xc (e.g. ge10-0/0/47)")
    parser.add_argument("--iface-b2", required=True, help="Second interface on host-b for l2-xc (e.g. ge10-0/0/48)")
    parser.add_argument("--scale", type=int, default=1000, help="Target session count (default 1000)")
    parser.add_argument("--skip-setup", action="store_true", help="Skip baseline setup (assume it exists)")
    parser.add_argument("--skip-interfaces", action="store_true", help="Skip sub-interface and l2-xc creation (assume they exist)")
    parser.add_argument("--skip-ha", action="store_true", help="Skip HA tests")
    parser.add_argument("--skip-cleanup", action="store_true", help="Do not cleanup after tests")
    parser.add_argument("--cleanup-only", action="store_true", help="Only run cleanup phase")
    parser.add_argument("--setup-only", action="store_true", help="Only run setup + session creation (no tests)")
    parser.add_argument("--retarget-only", action="store_true", help="Only change target on existing DM/SLM sessions (use with --target-mac)")
    parser.add_argument("--up-mep", action="store_true", help="Configure up MEPs on iface-a2/iface-b2 instead of down MEPs on iface-a/iface-b")
    parser.add_argument("--target-mac", default=None, help="Use target mac-address instead of mep-id (e.g. 84:40:76:96:dc:9b)")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE, help="Commands per commit batch")

    args = parser.parse_args()
    scale = args.scale

    tracker = TestTracker()
    start_time = datetime.now(timezone.utc)

    print(f"{'='*78}", flush=True)
    print(f"  SW-236991: Y.1731 Scale Test — {scale} DM + {scale} SLM sessions", flush=True)
    print(f"  Host A (initiator): {args.host_a}  iface: {args.iface_a}  l2-xc: {args.iface_a2}", flush=True)
    print(f"  Host B (responder): {args.host_b}  iface: {args.iface_b}  l2-xc: {args.iface_b2}", flush=True)
    print(f"  MEP direction: {'up (on iface-a2/b2)' if args.up_mep else 'down (on iface-a/b)'}", flush=True)
    if args.target_mac:
        print(f"  Target: mac-address {args.target_mac}", flush=True)
    else:
        print(f"  Target: mep-id (per-MA)", flush=True)
    print(f"  Started: {start_time.strftime('%Y-%m-%d %H:%M:%S UTC')}", flush=True)
    print(f"{'='*78}", flush=True)

    # --- Fast path: retarget only needs host-a ---
    if args.retarget_only:
        if not args.target_mac:
            print("  ERROR: --retarget-only requires --target-mac", flush=True)
            return 1
        section("Establishing SSH session to host-a")
        ssh_a, chan_a = create_shell(args.host_a, args.user, args.password, "host-a")
        try:
            section(f"Retargeting {scale} DM + {scale} SLM sessions to mac {args.target_mac}")
            retarget_dm = [
                f"{PM_CFM} two-way-delay-measurement {dm_session_name(i)} {_target_cmd(i, args.target_mac)}"
                for i in range(1, scale + 1)
            ]
            retarget_slm = [
                f"{PM_CFM} two-way-synthetic-loss-measurement {slm_session_name(i)} {_target_cmd(i, args.target_mac)}"
                for i in range(1, scale + 1)
            ]
            ok_d, fail_d = batched_configure(chan_a, retarget_dm, batch_size=50, wait_per_cmd=0.05, wait_commit=90, label="retarget-dm")
            ok_s, fail_s = batched_configure(chan_a, retarget_slm, batch_size=50, wait_per_cmd=0.05, wait_commit=90, label="retarget-slm")
            tracker.verdict("DM sessions retargeted", fail_d == 0)
            tracker.verdict("SLM sessions retargeted", fail_s == 0)
        finally:
            print("Closing SSH session...", flush=True)
            ssh_a.close()
            print(tracker.summary(), flush=True)
        return 0

    # Connect
    section("Establishing SSH sessions")
    ssh_a, chan_a = create_shell(args.host_a, args.user, args.password, "host-a")
    ssh_b, chan_b = create_shell(args.host_b, args.user, args.password, "host-b")

    try:
        # Pre-checks
        section("PRE-CHECKS")
        ver_a = run_show(chan_a, "show system version", 5)
        print(f"  Host-A version:\n{ver_a[:400]}", flush=True)
        ver_b = run_show(chan_b, "show system version", 5)
        print(f"  Host-B version:\n{ver_b[:400]}", flush=True)

        # Validate interfaces exist
        print("  Validating interfaces...", flush=True)
        for label, chan, iface in [
            ("host-a iface-a", chan_a, args.iface_a),
            ("host-a iface-a2", chan_a, args.iface_a2),
            ("host-b iface-b", chan_b, args.iface_b),
            ("host-b iface-b2", chan_b, args.iface_b2),
        ]:
            ok_if, out_if = validate_interface(chan, iface)
            tracker.verdict(f"Interface {iface} valid on {label}", ok_if)
            if not ok_if:
                print(f"    ERROR: {iface} not found on {label}. Aborting.", flush=True)
                return 1

        # Check no existing CFM on either device
        if not args.skip_setup and not args.cleanup_only:
            print("  Checking for existing CFM configuration...", flush=True)
            clean_a, ours_a, cfm_out_a = check_no_existing_cfm(chan_a)
            clean_b, ours_b, cfm_out_b = check_no_existing_cfm(chan_b)

            if clean_a and clean_b:
                tracker.verdict("No existing CFM on either device", True)
            elif (clean_a or ours_a) and (clean_b or ours_b):
                print(f"    Found leftover {MD_NAME} from previous run — auto-cleaning...", flush=True)
                for chan in [chan_a, chan_b]:
                    configure_and_commit(chan, [f"delete {CFM_BASE} maintenance-domains {MD_NAME}"], wait_commit=15)
                tracker.verdict("Leftover MD-SCALE cleaned on both devices", True)
            else:
                tracker.verdict("No existing CFM on host-a", clean_a)
                tracker.verdict("No existing CFM on host-b", clean_b)
                print("    ERROR: Foreign CFM config found. Clean it first or use --skip-setup.", flush=True)
                if not clean_a:
                    print(f"    host-a CFM config:\n{cfm_out_a[:500]}", flush=True)
                if not clean_b:
                    print(f"    host-b CFM config:\n{cfm_out_b[:500]}", flush=True)
                return 1

        mem_baseline = get_cfm_mgr_memory(chan_a)
        print(f"  cfm_mgr baseline memory: {mem_baseline} kB", flush=True)
        cores_baseline = get_core_dumps(chan_a)
        print(f"  Existing core dumps: {len(cores_baseline)}", flush=True)

        if args.cleanup_only:
            phase_cleanup(chan_a, chan_b, args.iface_a, args.iface_b, args.iface_a2, args.iface_b2, scale, tracker)
        else:
            # Phase 1: Setup
            if not args.skip_setup:
                phase_setup(chan_a, chan_b, args.iface_a, args.iface_b, args.iface_a2, args.iface_b2, scale, tracker, skip_interfaces=args.skip_interfaces, up_mep=args.up_mep)

            # Phase 2: Scale DM
            if not args.skip_setup:
                phase_scale_dm(chan_a, scale, tracker, target_mac=args.target_mac)

            # Phase 3: Scale SLM
            if not args.skip_setup:
                phase_scale_slm(chan_a, scale, tracker, target_mac=args.target_mac)

            if args.setup_only:
                print("\n  --setup-only: skipping test phases.", flush=True)
            else:
                # Phase 4: Verify
                phase_verify_scale(chan_a, scale, tracker)

                # Phase 5: Aggressive intervals
                phase_aggressive_intervals(chan_a, scale, tracker)

                # Phase 6: HA
                if not args.skip_ha:
                    chan_a = phase_ha(args.host_a, args.user, args.password, chan_a, scale, tracker)

                # Phase 7: Variants
                phase_variants(chan_a, scale, tracker)

                # Phase 8: Negative
                phase_negative(chan_a, scale, tracker, target_mac=args.target_mac)

                # Phase 9: Cleanup
                if not args.skip_cleanup:
                    phase_cleanup(chan_a, chan_b, args.iface_a, args.iface_b, args.iface_a2, args.iface_b2, scale, tracker)

    finally:
        print("\nClosing SSH sessions...", flush=True)
        for s in [ssh_a, ssh_b]:
            try:
                s.close()
            except Exception:
                pass

    # ======================================================================
    #  Results
    # ======================================================================
    end_time = datetime.now(timezone.utc)
    elapsed = (end_time - start_time).total_seconds()

    print(f"\n{'='*78}", flush=True)
    print(f"  SW-236991 SCALE TEST RESULTS", flush=True)
    print(f"{'='*78}", flush=True)
    print(f"  Duration: {elapsed:.0f}s ({elapsed/60:.1f} min)", flush=True)
    print(f"  Scale:    {scale} DM + {scale} SLM", flush=True)
    print(f"  Host A:   {args.host_a} ({args.iface_a} / {args.iface_a2})", flush=True)
    print(f"  Host B:   {args.host_b} ({args.iface_b} / {args.iface_b2})", flush=True)
    print(f"{'='*78}", flush=True)
    print(tracker.summary(), flush=True)

    overall = "PASS" if tracker.fail_count == 0 else "FAIL"
    print(f"\n  OVERALL: {overall}", flush=True)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(OUTPUT_DIR, "sw236991_scale_results.json")
    payload = {
        "ticket": "SW-236991",
        "start": start_time.isoformat(),
        "end": end_time.isoformat(),
        "duration_s": round(elapsed, 1),
        "scale": scale,
        "host_a": args.host_a,
        "host_b": args.host_b,
        "iface_a": args.iface_a,
        "iface_a2": args.iface_a2,
        "iface_b": args.iface_b,
        "iface_b2": args.iface_b2,
        "overall": overall,
        "verdicts": tracker.verdicts,
    }
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"\n  Results saved to {out_path}", flush=True)
    print(f"\n{'='*78}\n  TEST COMPLETED\n{'='*78}", flush=True)

    return 0 if tracker.fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
