#!/usr/bin/env python3
"""
SW-237080: Ethernet OAM Y.1731 | SNMP Tables: Availability, Content & Consistency

Validates the DRIVENETS-CFM-MIB SNMP tables for proactive and on-demand
DM/SLM sessions.  Walks all 8 tables, compares values with CLI output,
checks for duplicates/missing entries, and tests negative scenarios.

Requires an existing CFM MD/MA/MEP pair between two devices (at least one
proactive DM session should already be configured and running).

Usage:
    # SNMP from this (outer) machine (default):
    python3 test_sw237080_snmp_tables.py --host 100.64.5.225
    python3 test_sw237080_snmp_tables.py --host 100.64.5.225 --community mycomm

    # SNMP from DUT itself (legacy):
    python3 test_sw237080_snmp_tables.py --host 100.64.5.225 --snmp-source dut

    python3 test_sw237080_snmp_tables.py --host 100.64.5.225 --skip-setup
    python3 test_sw237080_snmp_tables.py --host 100.64.5.225 --cleanup-only

The script uses the existing CFM config on the device.  It will:
  - Enable SNMP community (if not already)
  - Create a proactive SLM session (if none exists)
  - Walk all DRIVENETS-CFM-MIB tables (from outer machine or DUT)
  - Compare SNMP values with CLI detail output
  - Run negative tests (disable session -> verify removal)
  - Clean up what it added
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import paramiko

sys.stdout.reconfigure(line_buffering=True)

# ---------------------------------------------------------------------------
#  Defaults & Constants
# ---------------------------------------------------------------------------
DEFAULT_USER = "dnroot"
DEFAULT_PASS = "dnroot"
OUTPUT_DIR = "/home/dn/output"

SNMP_COMMUNITY = "public"
SLM_SESSION_NAME = "SLM-SNMP-TEST"
SLM_PROFILE_NAME = "SLM-SNMP-PROF"

DN_CFM_OID = "1.3.6.1.4.1.49739.2.15"
DN_CFM_OBJECTS = f"{DN_CFM_OID}.1"

TABLES = {
    "ondemand_dm_info":     {"oid": f"{DN_CFM_OBJECTS}.1",  "label": "On-Demand DM Info"},
    "ondemand_dm_results":  {"oid": f"{DN_CFM_OBJECTS}.2",  "label": "On-Demand DM Results"},
    "ondemand_slm_info":    {"oid": f"{DN_CFM_OBJECTS}.3",  "label": "On-Demand SLM Info"},
    "ondemand_slm_results": {"oid": f"{DN_CFM_OBJECTS}.4",  "label": "On-Demand SLM Results"},
    "proactive_dm_session": {"oid": f"{DN_CFM_OBJECTS}.15", "label": "Proactive DM Session"},
    "proactive_dm_results": {"oid": f"{DN_CFM_OBJECTS}.16", "label": "Proactive DM Results"},
    "proactive_slm_session":{"oid": f"{DN_CFM_OBJECTS}.17", "label": "Proactive SLM Session"},
    "proactive_slm_results":{"oid": f"{DN_CFM_OBJECTS}.18", "label": "Proactive SLM Results"},
}

ANSI_RE = re.compile(
    r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07|\x1b[()][AB012]"
    r"|\x1b\[\?[0-9;]*[hlm]|\r"
)
CLI_ERROR_RE = re.compile(
    r"error:|unknown\s+command|invalid|command\s+failed|"
    r"commit\s+failed|validation\s+failed|syntax\s+error",
    re.IGNORECASE,
)
# DUT-local walk output:  DRIVENETS-SMI::dnMibs.15.1.<table>.1.<col>.<index> = ...
SNMP_VALUE_RE = re.compile(
    r"DRIVENETS-SMI::dnMibs\.15\.1\.(\d+)\.1\.(\d+)\.([\d.]+)\s+=\s+(.+)"
)
# External snmpwalk numeric output: .1.3.6.1.4.1.49739.2.15.1.<table>.1.<col>.<index> = ...
SNMP_NUMERIC_RE = re.compile(
    r"\.1\.3\.6\.1\.4\.1\.49739\.2\.15\.1\.(\d+)\.1\.(\d+)\.([\d.]+)\s+=\s+(.+)"
)

CFM_BASE = "services ethernet-oam connectivity-fault-management"
PM_BASE = "services performance-monitoring"
PM_CFM = f"{PM_BASE} cfm"
PM_PROF = f"{PM_BASE} profiles cfm"


class SnmpWalker:
    """Abstraction over DUT-local vs external SNMP walk."""

    def __init__(
        self, mode: str, host: str,
        community: str = "public",
        chan: Optional["paramiko.Channel"] = None,
    ):
        self.mode = mode          # "external" or "dut"
        self.host = host
        self.community = community
        self.chan = chan

    def walk(self, oid: str, wait: float = 20) -> str:
        if self.mode == "external":
            return run_snmp_walk_external(self.host, oid, self.community)
        return run_snmp_walk(self.chan, oid, wait)

    def has_data(self, raw: str) -> bool:
        """Check whether walk output contains actual SNMP data."""
        if self.mode == "external":
            return bool(SNMP_NUMERIC_RE.search(raw))
        return "dnMibs" in raw and "No more variables" not in raw.split("\n")[0]

    def has_any_oid(self, raw: str) -> bool:
        """Check whether walk output has any OID line at all."""
        if self.mode == "external":
            return bool(SNMP_NUMERIC_RE.search(raw))
        return "dnMibs" in raw


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
    chan = ssh.invoke_shell(width=400, height=1000)
    time.sleep(5)
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


def run_show(chan: paramiko.Channel, cmd: str, wait: float = 10) -> str:
    return send(chan, cmd + " | no-more", wait)


def run_snmp_walk(chan: paramiko.Channel, oid: str, wait: float = 20) -> str:
    """Execute 'run system snmp walk "<oid>" | no-more' and return output (DUT-local)."""
    return send(chan, f'run system snmp walk "{oid}" | no-more', wait)


def run_snmp_walk_external(
    host: str, oid: str, community: str = "public", timeout: int = 30,
) -> str:
    """Run snmpwalk from this (outer) machine using net-snmp CLI."""
    cmd = [
        "snmpwalk", "-v2c", "-c", community,
        "-On",       # numeric OID output for reliable parsing
        "-t", str(timeout),
        "-r", "2",   # retries
        host, oid,
    ]
    print(f"    [EXT-SNMP] {' '.join(cmd)}", flush=True)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout + 10,
        )
        output = result.stdout
        if result.returncode != 0 and not output:
            output = result.stderr
        return output
    except FileNotFoundError:
        return "ERROR: snmpwalk not found. Install net-snmp: apt install snmp"
    except subprocess.TimeoutExpired:
        return "ERROR: snmpwalk timed out"


def configure_hierarchical(
    chan: paramiko.Channel,
    nav_cmds: List[str],
    set_cmds: List[str],
    wait_per_cmd: float = 2,
    wait_commit: float = 15,
) -> Tuple[bool, str]:
    """Navigate hierarchical config mode, set values, exit back, commit."""
    send(chan, "configure", 3)
    for cmd in nav_cmds:
        send(chan, cmd, wait_per_cmd)
    for cmd in set_cmds:
        send(chan, cmd, wait_per_cmd)
    for _ in nav_cmds:
        send(chan, "exit", 1)
    out = send(chan, "commit", wait_commit)
    has_error = bool(CLI_ERROR_RE.search(out))
    if has_error:
        send(chan, "rollback", 5)
    send(chan, "end", 3)
    return not has_error, out


def configure_flat(
    chan: paramiko.Channel,
    cmds: List[str],
    wait_per_cmd: float = 1.5,
    wait_commit: float = 15,
) -> Tuple[bool, str]:
    """Enter configure mode, send flat-path commands, commit."""
    send(chan, "configure", 3)
    for cmd in cmds:
        send(chan, cmd, wait_per_cmd)
    out = send(chan, "commit", wait_commit)
    has_error = bool(CLI_ERROR_RE.search(out))
    if has_error:
        send(chan, "rollback", 5)
    send(chan, "end", 3)
    return not has_error, out


# ---------------------------------------------------------------------------
#  SNMP output parser
# ---------------------------------------------------------------------------
def parse_snmp_walk(raw: str) -> Dict[str, Dict[int, str]]:
    """Parse SNMP walk output into {table_num.index: {col_num: value}}.
    Handles both DUT-local (DRIVENETS-SMI::) and external numeric OID formats."""
    rows: Dict[str, Dict[int, str]] = {}
    for line in raw.splitlines():
        m = SNMP_VALUE_RE.search(line) or SNMP_NUMERIC_RE.search(line)
        if not m:
            continue
        table_num = int(m.group(1))
        col_num = int(m.group(2))
        index = m.group(3)
        value = m.group(4).strip()
        key = f"{table_num}.{index}"
        if key not in rows:
            rows[key] = {}
        rows[key][col_num] = value
    return rows


def extract_snmp_value(raw_val: str) -> str:
    """Extract the meaningful value from SNMP type-prefixed strings."""
    raw_val = raw_val.strip()
    for prefix in ["STRING:", "INTEGER:", "Gauge32:", "Hex-STRING:", "OID:", "Timeticks:"]:
        if raw_val.startswith(prefix):
            return raw_val[len(prefix):].strip().strip('"')
    return raw_val


# ---------------------------------------------------------------------------
#  CLI detail parsers
# ---------------------------------------------------------------------------
def parse_dm_detail(output: str) -> Dict[str, str]:
    """Extract key fields from proactive DM detail CLI output."""
    info: Dict[str, str] = {}
    patterns = {
        "md_name": r"Maintenance Domain:\s*(\S+)",
        "ma_name": r"Maintenance Association:\s*(\S+)",
        "mep_id": r"MEP-ID:\s*(\d+)",
        "source_interface": r"Source Interface:\s*(\S+)",
        "source_mac": r"Source MAC:\s*([0-9a-fA-F:]+)",
        "count": r"Count:\s*(\d+)\s*probes",
        "interval": r"Interval:\s*(\d+)\s*second",
        "timeout": r"Timeout:\s*(\d+)\s*second",
        "dmm_transmitted": r"DMM PDUs transmitted:\s*(\d+)",
        "dmr_received": r"DMR PDUs received:\s*(\d+)",
        "success_rate": r"Success rate:\s*([\d.]+)%",
        "delay_min": r"Minimum:\s*(\d+)\s*usec",
        "delay_avg": r"Average:\s*(\d+)\s*usec",
        "delay_max": r"Maximum:\s*(\d+)\s*usec",
        "ifdv_avg": r"IFDV Average:\s*(\d+)\s*usec",
        "ifdv_max": r"IFDV Maximum:\s*(\d+)\s*usec",
        "session_id": r"Session ID:\s*(\d+)",
        "validity": r"Measurement validity:\s*(\w+)",
    }
    for key, pat in patterns.items():
        m = re.search(pat, output, re.IGNORECASE)
        if m:
            info[key] = m.group(1).rstrip(",")
    target_m = re.search(r"Target:.*MEP-ID\s*(\d+)", output, re.IGNORECASE)
    if target_m:
        info["target_mep_id"] = target_m.group(1)
    return info


def parse_slm_detail(output: str) -> Dict[str, str]:
    """Extract key fields from proactive SLM detail CLI output."""
    info: Dict[str, str] = {}
    patterns = {
        "md_name": r"Maintenance Domain:\s*(\S+)",
        "ma_name": r"Maintenance Association:\s*(\S+)",
        "mep_id": r"MEP-ID:\s*(\d+)",
        "source_interface": r"Source Interface:\s*(\S+)",
        "source_mac": r"Source MAC:\s*([0-9a-fA-F:]+)",
        "count": r"Count:\s*(\d+)\s*probes",
        "interval": r"Interval:\s*(\d+)\s*second",
        "timeout": r"Timeout:\s*(\d+)\s*second",
        "slm_sent": r"SLM PDUs transmitted:\s*(\d+)",
        "slr_received": r"SLR PDUs received:\s*(\d+)",
        "near_end_loss_pct": r"Near-end.*?loss.*?:\s*([\d.]+)%",
        "far_end_loss_pct": r"Far-end.*?loss.*?:\s*([\d.]+)%",
        "session_id": r"Session ID:\s*(\d+)",
        "validity": r"Measurement validity:\s*(\w+)",
    }
    for key, pat in patterns.items():
        m = re.search(pat, output, re.IGNORECASE)
        if m:
            info[key] = m.group(1).rstrip(",")
    target_m = re.search(r"Target:.*MEP-ID\s*(\d+)", output, re.IGNORECASE)
    if target_m:
        info["target_mep_id"] = target_m.group(1)
    return info


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
        if detail and not passed:
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


# ---------------------------------------------------------------------------
#  Test phases
# ---------------------------------------------------------------------------
def phase_snmp_setup(
    chan: paramiko.Channel, tracker: TestTracker, walker: SnmpWalker,
) -> bool:
    """Enable SNMP community on the device if not already configured."""
    section("Phase 1: SNMP Setup")

    communities = run_show(chan, "show system snmp communities")
    already_configured = walker.community in communities

    if already_configured:
        print(f"  SNMP community '{walker.community}' already configured.", flush=True)
        tracker.verdict("SNMP community exists", True)
    else:
        print(f"  Configuring SNMP community '{walker.community}'...", flush=True)
        ok, out = configure_hierarchical(
            chan,
            nav_cmds=["system", "snmp", f"community {walker.community} vrf default"],
            set_cmds=["admin-state enabled"],
        )
        tracker.verdict("SNMP community configured", ok, "" if ok else out[:200])
        if not ok:
            return False

    snmp_test = walker.walk("1.3.6.1.2.1.1", 10)
    works = "sysDescr" in snmp_test or "STRING:" in snmp_test
    tracker.verdict("SNMP walk returns system OIDs", works,
                    "" if works else snmp_test[:200])

    mibs = run_show(chan, "show system snmp mibs")
    has_cfm_mib = "DRIVENETS-CFM-MIB" in mibs
    tracker.verdict("DRIVENETS-CFM-MIB available", has_cfm_mib)

    return works


def phase_session_setup(
    chan: paramiko.Channel, tracker: TestTracker,
    dm_session: Optional[str], md_name: str, ma_name: str,
    local_mep: int, remote_mep: int,
) -> Tuple[Optional[str], Optional[str], bool, bool]:
    """Ensure both a DM and SLM proactive session are running.
    Returns (dm_session, slm_session, created_slm, created_slm_profile)."""
    section("Phase 2: Session Setup")

    proactive = run_show(chan, "show services performance-monitoring cfm tests proactive")
    dm_data_lines = [l for l in proactive.splitlines()
                     if "two-way-delay-measurement" in l and "|" in l and not l.strip().startswith("+")]
    slm_data_lines = [l for l in proactive.splitlines()
                      if "two-way-synthetic-loss" in l and "|" in l and not l.strip().startswith("+")]
    print(f"  Existing sessions: {len(dm_data_lines)} DM, {len(slm_data_lines)} SLM", flush=True)

    if dm_session and dm_session in proactive:
        print(f"  DM session '{dm_session}' already running.", flush=True)
        tracker.verdict(f"DM session '{dm_session}' exists", True)
    elif dm_data_lines:
        dm_match = re.search(r"\|\s*([\w-]+)\s*\|", dm_data_lines[0])
        if dm_match:
            dm_session = dm_match.group(1).strip()
            print(f"  Found existing DM session: {dm_session}", flush=True)
            tracker.verdict(f"DM session '{dm_session}' exists", True)
    else:
        tracker.verdict("No DM session found", False, "Need at least one proactive DM session running")
        return dm_session, None, False, False

    slm_session_name = None
    created_slm = False
    created_slm_profile = False

    if slm_data_lines:
        slm_match = re.search(r"\|\s*([\w-]+)\s*\|", slm_data_lines[0])
        if slm_match:
            slm_session_name = slm_match.group(1).strip()
            print(f"  Found existing SLM session: {slm_session_name}", flush=True)
            tracker.verdict(f"SLM session '{slm_session_name}' exists", True)
    else:
        print(f"  No SLM session found. Creating '{SLM_SESSION_NAME}'...", flush=True)

        profile_out = run_show(chan, f"show config {PM_PROF}")
        has_slm_profile = "two-way-synthetic-loss-measurement" in profile_out

        if not has_slm_profile:
            print(f"  Creating SLM profile '{SLM_PROFILE_NAME}'...", flush=True)
            ok, out = configure_flat(chan, [
                f"{PM_PROF} two-way-synthetic-loss-measurement {SLM_PROFILE_NAME} inform-test-results enabled",
                f"{PM_PROF} two-way-synthetic-loss-measurement {SLM_PROFILE_NAME} test-duration probes probe-count 5 probe-interval 1 repeat-interval 10",
                f"{PM_PROF} two-way-synthetic-loss-measurement {SLM_PROFILE_NAME} thresholds near-end-loss 10",
                f"{PM_PROF} two-way-synthetic-loss-measurement {SLM_PROFILE_NAME} thresholds far-end-loss 10",
            ])
            if not ok:
                tracker.verdict("SLM profile creation", False, out[:200])
                return dm_session, None, False, False
            slm_profile = SLM_PROFILE_NAME
            created_slm_profile = True
        else:
            slm_prof_match = re.search(r"two-way-synthetic-loss-measurement\s+(\S+)", profile_out)
            slm_profile = slm_prof_match.group(1) if slm_prof_match else SLM_PROFILE_NAME

        ok, out = configure_flat(chan, [
            f"{PM_CFM} two-way-synthetic-loss-measurement {SLM_SESSION_NAME} admin-state enabled",
            f"{PM_CFM} two-way-synthetic-loss-measurement {SLM_SESSION_NAME} profile {slm_profile}",
            f"{PM_CFM} two-way-synthetic-loss-measurement {SLM_SESSION_NAME} source maintenance-domain {md_name} maintenance-association {ma_name} mep-id {local_mep}",
            f"{PM_CFM} two-way-synthetic-loss-measurement {SLM_SESSION_NAME} target mep-id {remote_mep}",
        ])
        tracker.verdict(f"SLM session '{SLM_SESSION_NAME}' created", ok,
                        "" if ok else out[:200])
        if ok:
            slm_session_name = SLM_SESSION_NAME
            created_slm = True
            print("  Waiting 30s for first computation interval...", flush=True)
            time.sleep(30)

    return dm_session, slm_session_name, created_slm, created_slm_profile


def phase_cli_baseline(
    chan: paramiko.Channel, tracker: TestTracker,
    dm_session: str, slm_session: Optional[str],
) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Collect CLI detail output as the reference baseline."""
    section("Phase 3: CLI Baseline Collection")

    dm_detail = run_show(
        chan,
        f"show {PM_BASE} cfm tests proactive two-way-delay session-name {dm_session} detail",
        15,
    )
    dm_info = parse_dm_detail(dm_detail)
    print(f"  DM session '{dm_session}': {len(dm_info)} fields parsed", flush=True)
    for k, v in sorted(dm_info.items()):
        print(f"    {k}: {v}", flush=True)
    tracker.verdict(f"DM CLI baseline collected ({len(dm_info)} fields)", len(dm_info) >= 5)

    slm_info: Dict[str, str] = {}
    if slm_session:
        slm_detail = run_show(
            chan,
            f"show {PM_BASE} cfm tests proactive two-way-synthetic-loss session-name {slm_session} detail",
            15,
        )
        slm_info = parse_slm_detail(slm_detail)
        print(f"  SLM session '{slm_session}': {len(slm_info)} fields parsed", flush=True)
        for k, v in sorted(slm_info.items()):
            print(f"    {k}: {v}", flush=True)
        tracker.verdict(f"SLM CLI baseline collected ({len(slm_info)} fields)", len(slm_info) >= 3)

    return dm_info, slm_info


def phase_snmp_walk_all(
    chan: paramiko.Channel, tracker: TestTracker, walker: SnmpWalker,
) -> Dict[str, str]:
    """Walk all 8 SNMP tables and store raw output."""
    section("Phase 4: SNMP Table Walks")
    raw_outputs: Dict[str, str] = {}

    for key, info in TABLES.items():
        print(f"  Walking {info['label']} ({info['oid']})...", flush=True)
        raw = walker.walk(info["oid"], 20)
        raw_outputs[key] = raw

        has_data = walker.has_data(raw)
        is_proactive = key.startswith("proactive")

        if is_proactive:
            tracker.verdict(f"{info['label']} populated", has_data,
                            "" if has_data else "Table empty")
        else:
            parsed = parse_snmp_walk(raw)
            print(f"    {len(parsed)} rows returned", flush=True)

    return raw_outputs


def phase_compare(
    chan: paramiko.Channel, tracker: TestTracker,
    dm_info: Dict[str, str], slm_info: Dict[str, str],
    raw_outputs: Dict[str, str], walker: SnmpWalker,
):
    """Compare SNMP values against CLI baseline."""
    section("Phase 5: SNMP vs CLI Comparison")

    # --- Proactive DM session table ---
    dm_walk = raw_outputs.get("proactive_dm_session", "")
    dm_parsed = parse_snmp_walk(dm_walk)

    if dm_parsed:
        first_key = next(iter(dm_parsed))
        first_row = dm_parsed[first_key]
        print(f"  DM session SNMP row (key={first_key}):", flush=True)

        if 5 in first_row:
            snmp_md = extract_snmp_value(first_row[5])
            cli_md = dm_info.get("md_name", "")
            tracker.verdict(f"DM SourceMdName: SNMP='{snmp_md}' vs CLI='{cli_md}'",
                            snmp_md == cli_md)

        if 6 in first_row:
            snmp_ma = extract_snmp_value(first_row[6])
            cli_ma = dm_info.get("ma_name", "")
            tracker.verdict(f"DM SourceMaName: SNMP='{snmp_ma}' vs CLI='{cli_ma}'",
                            snmp_ma == cli_ma)

        if 9 in first_row:
            snmp_target = extract_snmp_value(first_row[9])
            cli_target = dm_info.get("target_mep_id", "")
            tracker.verdict(f"DM TargetMepId: SNMP='{snmp_target}' vs CLI='{cli_target}'",
                            snmp_target == cli_target)

        if 10 in first_row:
            snmp_iface = extract_snmp_value(first_row[10])
            cli_iface = dm_info.get("source_interface", "")
            tracker.verdict(f"DM SourceInterface: SNMP='{snmp_iface}' vs CLI='{cli_iface}'",
                            snmp_iface == cli_iface)

        if 13 in first_row:
            snmp_count = extract_snmp_value(first_row[13])
            cli_count = dm_info.get("count", "")
            tracker.verdict(f"DM ProbeCount: SNMP='{snmp_count}' vs CLI='{cli_count}'",
                            snmp_count == cli_count)

        if 14 in first_row:
            snmp_interval = extract_snmp_value(first_row[14])
            cli_interval = dm_info.get("interval", "")
            tracker.verdict(f"DM Interval: SNMP='{snmp_interval}' vs CLI='{cli_interval}'",
                            snmp_interval == cli_interval)

        if 15 in first_row:
            snmp_timeout = extract_snmp_value(first_row[15])
            cli_timeout = dm_info.get("timeout", "")
            tracker.verdict(f"DM Timeout: SNMP='{snmp_timeout}' vs CLI='{cli_timeout}'",
                            snmp_timeout == cli_timeout)

        if 11 in first_row:
            snmp_hex = extract_snmp_value(first_row[11]).lower()
            cli_mac = dm_info.get("source_mac", "").lower()
            cli_hex = cli_mac.replace(":", " ")
            match = cli_hex in snmp_hex or cli_mac in snmp_hex.replace(" ", ":")
            tracker.verdict(f"DM SourceMAC match", match,
                            f"SNMP='{snmp_hex}' vs CLI='{cli_mac}'")
    else:
        tracker.verdict("DM session SNMP data parsed", False, "No parseable data")

    # --- Proactive DM results table ---
    dm_results_walk = raw_outputs.get("proactive_dm_results", "")
    dm_results_parsed = parse_snmp_walk(dm_results_walk)

    if dm_results_parsed:
        sorted_keys = sorted(dm_results_parsed.keys())
        if len(sorted_keys) >= 2:
            row_key = sorted_keys[-2]
            print(f"  DM results: using second-to-last (completed) interval, key={row_key}:", flush=True)
        else:
            row_key = sorted_keys[-1]
            print(f"  DM results: only 1 interval, key={row_key}:", flush=True)
        row = dm_results_parsed[row_key]

        validity_map = {"1": "invalid", "2": "incomplete", "3": "valid"}
        if 8 in row:
            val = extract_snmp_value(row[8])
            cli_val = dm_info.get("validity", "")
            snmp_label = validity_map.get(val, f"unknown({val})")
            valid_ok = snmp_label == cli_val.lower() or (val == "3" and "valid" in cli_val.lower())
            tracker.verdict(f"DM Validity: SNMP={val} ({snmp_label}) vs CLI='{cli_val}'", valid_ok)

        if 9 in row:
            snmp_tx = extract_snmp_value(row[9])
            cli_tx = dm_info.get("dmm_transmitted", "")
            cli_count = dm_info.get("count", "")
            try:
                in_range = 0 < int(snmp_tx) <= int(cli_count)
            except ValueError:
                in_range = False
            tracker.verdict(
                f"DM DmmTransmitted: SNMP={snmp_tx} (per-interval), probe-count={cli_count}, CLI cumulative={cli_tx}",
                in_range or snmp_tx == cli_tx,
            )

        if 10 in row:
            snmp_rx = extract_snmp_value(row[10])
            cli_rx = dm_info.get("dmr_received", "")
            cli_count = dm_info.get("count", "")
            try:
                in_range = 0 < int(snmp_rx) <= int(cli_count)
            except ValueError:
                in_range = False
            tracker.verdict(
                f"DM DmrReceived: SNMP={snmp_rx} (per-interval), probe-count={cli_count}, CLI cumulative={cli_rx}",
                in_range or snmp_rx == cli_rx,
            )

        if 11 in row:
            snmp_sr = extract_snmp_value(row[11])
            cli_sr = dm_info.get("success_rate", "")
            if cli_sr:
                try:
                    expected = str(int(float(cli_sr) * 100))
                    match = snmp_sr == expected
                except ValueError:
                    match = False
                tracker.verdict(
                    f"DM SuccessRate: SNMP={snmp_sr} (={float(snmp_sr)/100:.1f}%) vs CLI={cli_sr}%",
                    match,
                )

        if 13 in row:
            snmp_avg = extract_snmp_value(row[13])
            cli_avg = dm_info.get("delay_avg", "")
            tracker.verdict(f"DM DelayAvg: SNMP={snmp_avg}us vs CLI={cli_avg}us",
                            snmp_avg == cli_avg)

        if 15 in row:
            snmp_jitter = extract_snmp_value(row[15])
            cli_jitter = dm_info.get("ifdv_avg", "")
            tracker.verdict(f"DM IfdvAvg: SNMP={snmp_jitter}us vs CLI={cli_jitter}us",
                            snmp_jitter == cli_jitter)

        result_count = len(dm_results_parsed)
        tracker.verdict(f"DM results: {result_count} intervals (max 10+1, no duplicates)",
                        result_count <= 11)
    else:
        tracker.verdict("DM results SNMP data parsed", False, "No parseable data")

    # --- Proactive SLM session table ---
    slm_walk = raw_outputs.get("proactive_slm_session", "")
    if slm_info and walker.has_any_oid(slm_walk):
        slm_parsed = parse_snmp_walk(slm_walk)
        if slm_parsed:
            first_key = next(iter(slm_parsed))
            first_row = slm_parsed[first_key]
            if 5 in first_row:
                snmp_md = extract_snmp_value(first_row[5])
                cli_md = slm_info.get("md_name", "")
                tracker.verdict(f"SLM SourceMdName: SNMP='{snmp_md}' vs CLI='{cli_md}'",
                                snmp_md == cli_md)
            if 6 in first_row:
                snmp_ma = extract_snmp_value(first_row[6])
                cli_ma = slm_info.get("ma_name", "")
                tracker.verdict(f"SLM SourceMaName: SNMP='{snmp_ma}' vs CLI='{cli_ma}'",
                                snmp_ma == cli_ma)
            if 9 in first_row:
                snmp_target = extract_snmp_value(first_row[9])
                cli_target = slm_info.get("target_mep_id", "")
                tracker.verdict(f"SLM TargetMepId: SNMP='{snmp_target}' vs CLI='{cli_target}'",
                                snmp_target == cli_target)
            tracker.verdict("SLM session table populated", True)
        else:
            tracker.verdict("SLM session SNMP data parsed", False)
    elif slm_info:
        tracker.verdict("SLM session table populated", False, "Table empty")


def phase_consistency(
    chan: paramiko.Channel, tracker: TestTracker,
    raw_outputs: Dict[str, str], walker: SnmpWalker,
):
    """Check for duplicates and missing entries across tables."""
    section("Phase 6: Consistency Checks")

    dm_session_walk = raw_outputs.get("proactive_dm_session", "")
    dm_session_parsed = parse_snmp_walk(dm_session_walk)
    dm_sessions = set()
    for key in dm_session_parsed:
        parts = key.split(".", 1)
        if int(parts[0]) == 15:
            dm_sessions.add(parts[1])

    print(f"  Unique DM session indices: {len(dm_sessions)}", flush=True)
    tracker.verdict(f"DM session count >= 1", len(dm_sessions) >= 1,
                    f"Found {len(dm_sessions)} sessions")

    dm_results_walk = raw_outputs.get("proactive_dm_results", "")
    dm_results_parsed = parse_snmp_walk(dm_results_walk)

    for sid in dm_sessions:
        has_results = any(sid in k for k in dm_results_parsed if k.startswith("16."))
        tracker.verdict(f"DM session {sid} has result entries", has_results)

    full_walk = walker.walk(DN_CFM_OID, 30)
    # Match OIDs in both named and numeric format
    oid_re = re.compile(
        r"((?:DRIVENETS-SMI::dnMibs|\.1\.3\.6\.1\.4\.1\.49739\.2\.15)"
        r"[\d.]+)\s*="
    )
    oid_list = []
    for line in full_walk.splitlines():
        m = oid_re.search(line)
        if m:
            oid_list.append(m.group(1))
    seen: Dict[str, int] = {}
    duplicates = []
    for oid in oid_list:
        seen[oid] = seen.get(oid, 0) + 1
        if seen[oid] == 2:
            duplicates.append(oid)
    tracker.verdict(
        f"No duplicate OIDs in full MIB walk ({len(oid_list)} total)",
        len(duplicates) == 0,
        f"{len(duplicates)} duplicates: {', '.join(duplicates[:5])}" if duplicates else "",
    )


def phase_negative(
    chan: paramiko.Channel, tracker: TestTracker,
    slm_session: Optional[str],
    created_slm: bool,
    walker: SnmpWalker,
):
    """Test negative scenarios: disable session, verify SNMP removal."""
    section("Phase 7: Negative Tests")

    if not slm_session:
        print("  Skipping negative tests (no SLM session).", flush=True)
        return

    if not created_slm:
        print("  Skipping SLM disable test (session was pre-existing, won't modify).", flush=True)
        return

    print(f"  Disabling SLM session '{slm_session}'...", flush=True)
    ok, _ = configure_flat(chan, [
        f"{PM_CFM} two-way-synthetic-loss-measurement {slm_session} admin-state disabled",
    ])
    if not ok:
        tracker.verdict("SLM session disabled", False)
        return
    time.sleep(10)

    slm_walk = walker.walk(TABLES["proactive_slm_session"]["oid"], 15)
    session_gone = not walker.has_any_oid(slm_walk)
    tracker.verdict(f"Disabled SLM session removed from SNMP", session_gone,
                    "" if session_gone else f"Still visible: {slm_walk[:200]}")

    print(f"  Re-enabling SLM session '{slm_session}'...", flush=True)
    ok, _ = configure_flat(chan, [
        f"{PM_CFM} two-way-synthetic-loss-measurement {slm_session} admin-state enabled",
    ])
    tracker.verdict("SLM session re-enabled", ok)
    if ok:
        time.sleep(15)
        slm_walk2 = walker.walk(TABLES["proactive_slm_session"]["oid"], 15)
        reappeared = walker.has_any_oid(slm_walk2)
        tracker.verdict("SLM session reappears in SNMP after re-enable", reappeared)


def phase_ondemand(
    chan: paramiko.Channel, tracker: TestTracker,
    md_name: str, ma_name: str, remote_mep: int,
    walker: SnmpWalker,
):
    """Run on-demand DM and SLM, verify on-demand SNMP tables populate."""
    section("Phase 8: On-Demand Table Validation")

    print("  Running on-demand DM...", flush=True)
    send(chan,
         f"run ethernet-oam cfm on-demand delay-measurement two-way "
         f"maintenance-domain {md_name} maintenance-association {ma_name} "
         f"target mep-id {remote_mep}",
         20)
    time.sleep(5)

    dm_info_walk = walker.walk(TABLES["ondemand_dm_info"]["oid"], 15)
    dm_results_walk = walker.walk(TABLES["ondemand_dm_results"]["oid"], 15)
    has_dm_info = walker.has_any_oid(dm_info_walk)
    has_dm_results = walker.has_any_oid(dm_results_walk)
    tracker.verdict("On-Demand DM Info table populated", has_dm_info,
                    "" if has_dm_info else "Table empty after on-demand DM")
    tracker.verdict("On-Demand DM Results table populated", has_dm_results,
                    "" if has_dm_results else "Table empty after on-demand DM")

    print("  Running on-demand SLM...", flush=True)
    send(chan,
         f"run ethernet-oam cfm on-demand synthetic-loss-measurement two-way "
         f"maintenance-domain {md_name} maintenance-association {ma_name} "
         f"target mep-id {remote_mep}",
         20)
    time.sleep(5)

    slm_info_walk = walker.walk(TABLES["ondemand_slm_info"]["oid"], 15)
    slm_results_walk = walker.walk(TABLES["ondemand_slm_results"]["oid"], 15)
    has_slm_info = walker.has_any_oid(slm_info_walk)
    has_slm_results = walker.has_any_oid(slm_results_walk)
    tracker.verdict("On-Demand SLM Info table populated", has_slm_info,
                    "" if has_slm_info else "Table empty after on-demand SLM")
    tracker.verdict("On-Demand SLM Results table populated", has_slm_results,
                    "" if has_slm_results else "Table empty after on-demand SLM")


def phase_cleanup(
    chan: paramiko.Channel, tracker: TestTracker,
    created_slm: bool, created_slm_profile: bool,
    created_snmp: bool, community: str = SNMP_COMMUNITY,
):
    """Remove anything this script added."""
    section("Phase 9: Cleanup")

    if created_slm:
        print(f"  Removing SLM session '{SLM_SESSION_NAME}'...", flush=True)
        ok, _ = configure_flat(chan, [
            f"delete {PM_CFM} two-way-synthetic-loss-measurement {SLM_SESSION_NAME}",
        ])
        tracker.verdict(f"SLM session '{SLM_SESSION_NAME}' removed", ok)

    if created_slm_profile:
        print(f"  Removing SLM profile '{SLM_PROFILE_NAME}'...", flush=True)
        ok, _ = configure_flat(chan, [
            f"delete {PM_PROF} two-way-synthetic-loss-measurement {SLM_PROFILE_NAME}",
        ])
        tracker.verdict(f"SLM profile '{SLM_PROFILE_NAME}' removed", ok)

    if created_snmp:
        print(f"  Removing SNMP community '{community}'...", flush=True)
        ok, _ = configure_hierarchical(
            chan,
            nav_cmds=["system", "snmp"],
            set_cmds=[f"no community {community} vrf default"],
        )
        tracker.verdict(f"SNMP community removed", ok)

    if not any([created_slm, created_slm_profile, created_snmp]):
        print("  Nothing to clean up (all pre-existing).", flush=True)


# ---------------------------------------------------------------------------
#  Auto-detect CFM config
# ---------------------------------------------------------------------------
def detect_cfm_config(chan: paramiko.Channel) -> Dict[str, str]:
    """Detect existing CFM MD/MA/MEP config from proactive sessions or config."""
    proactive = run_show(chan, f"show {PM_BASE} cfm tests proactive")

    dm_match = re.search(
        r"\|\s+([\w-]+)\s+\|\s+two-way-delay-measurement\s+\|\s+(\S+)\s+\|\s+(\S+)\s+\|\s+(\d+)\s+\|\s+(\d+)",
        proactive,
    )
    if dm_match:
        return {
            "dm_session": dm_match.group(1).strip(),
            "md_name": dm_match.group(2).strip(),
            "ma_name": dm_match.group(3).strip(),
            "local_mep": dm_match.group(4).strip(),
            "remote_mep": dm_match.group(5).strip(),
        }

    dm_name_match = re.search(r"([\w-]+)\s+.*two-way-delay", proactive)
    dm_session = dm_name_match.group(1).strip() if dm_name_match else None

    if dm_session:
        detail = run_show(
            chan,
            f"show {PM_BASE} cfm tests proactive two-way-delay session-name {dm_session} detail",
            15,
        )
        md_m = re.search(r"Maintenance Domain:\s*(\S+)", detail)
        ma_m = re.search(r"Maintenance Association:\s*(\S+)", detail)
        mep_m = re.search(r"MEP-ID:\s*(\d+)", detail)
        target_m = re.search(r"Target:.*MEP-ID\s*(\d+)", detail)
        return {
            "dm_session": dm_session,
            "md_name": md_m.group(1).rstrip(",") if md_m else "MD-SCALE",
            "ma_name": ma_m.group(1).rstrip(",") if ma_m else "MA-SCALE-0001",
            "local_mep": mep_m.group(1) if mep_m else "2",
            "remote_mep": target_m.group(1) if target_m else "1",
        }

    cfm_config = run_show(chan, f"show config {CFM_BASE}", 15)
    md_match = re.search(r"maintenance-domains\s+(\S+)", cfm_config)
    ma_match = re.search(r"maintenance-associations\s+(\S+)", cfm_config)
    mep_match = re.search(r"local-mep\s+(\d+)", cfm_config)
    remote_match = re.search(r"crosscheck\s+mep-id\s+(\d+)", cfm_config)

    pm_config = run_show(chan, f"show config {PM_CFM}", 15)
    dm_name_match2 = re.search(r"two-way-delay-measurement\s+(\S+)", pm_config)

    return {
        "dm_session": dm_name_match2.group(1) if dm_name_match2 else None,
        "md_name": md_match.group(1) if md_match else "MD-SCALE",
        "ma_name": ma_match.group(1) if ma_match else "MA-SCALE-0001",
        "local_mep": mep_match.group(1) if mep_match else "2",
        "remote_mep": remote_match.group(1) if remote_match else "1",
    }


# ---------------------------------------------------------------------------
#  Main
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description="SW-237080: Y.1731 SNMP Tables Availability, Content & Consistency"
    )
    parser.add_argument("--host", required=True, help="DUT IP address")
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument("--password", default=DEFAULT_PASS)
    parser.add_argument("--dm-session", default=None,
                        help="Name of existing DM session (auto-detected if omitted)")
    parser.add_argument("--md-name", default=None, help="Override MD name")
    parser.add_argument("--ma-name", default=None, help="Override MA name")
    parser.add_argument("--local-mep", type=int, default=None)
    parser.add_argument("--remote-mep", type=int, default=None)
    parser.add_argument("--skip-setup", action="store_true",
                        help="Skip SNMP and session setup")
    parser.add_argument("--skip-ondemand", action="store_true",
                        help="Skip on-demand table tests")
    parser.add_argument("--skip-negative", action="store_true",
                        help="Skip negative tests")
    parser.add_argument("--skip-cleanup", action="store_true",
                        help="Skip cleanup phase")
    parser.add_argument("--cleanup-only", action="store_true",
                        help="Only run cleanup")
    parser.add_argument("--snmp-source", choices=["external", "dut"],
                        default="external",
                        help="Where to run snmpwalk from (default: external = this machine)")
    parser.add_argument("--community", default=SNMP_COMMUNITY,
                        help=f"SNMP community string (default: {SNMP_COMMUNITY})")
    args = parser.parse_args()

    start_time = datetime.now(timezone.utc)
    tracker = TestTracker()
    sw_version = "unknown"

    print(f"\n{'='*78}", flush=True)
    print(f"  SW-237080: SNMP Tables Availability, Content & Consistency", flush=True)
    print(f"  DUT: {args.host}", flush=True)
    print(f"  SNMP source: {args.snmp_source} (community: {args.community})", flush=True)
    print(f"  Started: {start_time.isoformat()}", flush=True)
    print(f"{'='*78}", flush=True)

    ssh = None
    chan = None
    walker = None
    created_slm = False
    created_slm_profile = False
    created_snmp = False
    md_name = args.md_name or "MD-SCALE"
    ma_name = args.ma_name or "MA-SCALE-0001"

    try:
        section("Connecting to DUT")
        ssh, chan = create_shell(args.host, args.user, args.password, "DUT")
        walker = SnmpWalker(
            mode=args.snmp_source, host=args.host,
            community=args.community, chan=chan,
        )

        version_out = run_show(chan, "show system version")
        version_match = (
            re.search(r"DNOS.*?\[([\d.]+[^\]]*)\]", version_out)
            or re.search(r"Software Version:\s*(\S+)", version_out)
            or re.search(r"[Vv]ersion\s*[:\s]*([\d.]+\S*)", version_out)
        )
        sw_version = version_match.group(1) if version_match else "unknown"
        print(f"  Software version: {sw_version}", flush=True)

        cfm = detect_cfm_config(chan)
        dm_session = args.dm_session or cfm.get("dm_session")
        md_name = args.md_name or cfm["md_name"]
        ma_name = args.ma_name or cfm["ma_name"]
        local_mep = args.local_mep or int(cfm["local_mep"])
        remote_mep = args.remote_mep or int(cfm["remote_mep"])

        print(f"  CFM config: MD={md_name}, MA={ma_name}, "
              f"local MEP={local_mep}, remote MEP={remote_mep}", flush=True)
        if dm_session:
            print(f"  DM session: {dm_session}", flush=True)

        if args.cleanup_only:
            phase_cleanup(chan, tracker,
                          created_slm=True, created_slm_profile=True,
                          created_snmp=True, community=args.community)
        else:
            # Phase 1: SNMP
            if not args.skip_setup:
                communities = run_show(chan, "show system snmp communities")
                snmp_existed = args.community in communities
                snmp_ok = phase_snmp_setup(chan, tracker, walker)
                created_snmp = not snmp_existed and snmp_ok
                if not snmp_ok:
                    print("  FATAL: SNMP not working. Cannot continue.", flush=True)
                    return 1
            else:
                print("  Skipping SNMP setup (--skip-setup).", flush=True)

            # Phase 2: Sessions
            slm_session = None
            if not args.skip_setup:
                dm_session, slm_session, created_slm, created_slm_profile = \
                    phase_session_setup(chan, tracker, dm_session, md_name, ma_name, local_mep, remote_mep)
            else:
                proactive = run_show(chan, f"show {PM_BASE} cfm tests proactive")
                slm_lines = [l for l in proactive.splitlines()
                             if "two-way-synthetic-loss" in l and "|" in l and not l.strip().startswith("+")]
                if slm_lines:
                    slm_match = re.search(r"\|\s*([\w-]+)\s*\|", slm_lines[0])
                    slm_session = slm_match.group(1).strip() if slm_match else None

            if not dm_session:
                print("  FATAL: No DM session found. Cannot continue.", flush=True)
                return 1

            # Phase 3: CLI Baseline
            dm_info, slm_info = phase_cli_baseline(chan, tracker, dm_session, slm_session)

            # Phase 4: Walk all tables
            raw_outputs = phase_snmp_walk_all(chan, tracker, walker)

            # Phase 5: Compare
            phase_compare(chan, tracker, dm_info, slm_info, raw_outputs, walker)

            # Phase 6: Consistency
            phase_consistency(chan, tracker, raw_outputs, walker)

            # Phase 7: Negative
            if not args.skip_negative:
                phase_negative(chan, tracker, slm_session, created_slm, walker)

            # Phase 8: On-demand
            if not args.skip_ondemand:
                phase_ondemand(chan, tracker, md_name, ma_name, remote_mep, walker)

            # Phase 9: Cleanup
            if not args.skip_cleanup:
                phase_cleanup(chan, tracker, created_slm, created_slm_profile,
                              created_snmp, community=args.community)

    finally:
        if ssh:
            print("\nClosing SSH session...", flush=True)
            try:
                ssh.close()
            except Exception:
                pass

    end_time = datetime.now(timezone.utc)
    elapsed = (end_time - start_time).total_seconds()

    print(f"\n{'='*78}", flush=True)
    print(f"  SW-237080 SNMP TABLES TEST RESULTS", flush=True)
    print(f"{'='*78}", flush=True)
    print(f"  Duration: {elapsed:.0f}s ({elapsed/60:.1f} min)", flush=True)
    print(f"  DUT:      {args.host} (v{sw_version})", flush=True)
    print(f"  CFM:      MD={md_name}, MA={ma_name}", flush=True)
    print(f"{'='*78}", flush=True)
    print(tracker.summary(), flush=True)

    overall = "PASS" if tracker.fail_count == 0 else "FAIL"
    print(f"\n  OVERALL: {overall}", flush=True)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(OUTPUT_DIR, "sw237080_snmp_tables_results.json")
    payload = {
        "ticket": "SW-237080",
        "start": start_time.isoformat(),
        "end": end_time.isoformat(),
        "duration_s": round(elapsed, 1),
        "host": args.host,
        "sw_version": sw_version,
        "md_name": md_name,
        "ma_name": ma_name,
        "dm_session": dm_session,
        "overall": overall,
        "pass_count": tracker.pass_count,
        "fail_count": tracker.fail_count,
        "verdicts": tracker.verdicts,
    }
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"\n  Results saved to {out_path}", flush=True)
    print(f"\n{'='*78}\n  TEST COMPLETED\n{'='*78}", flush=True)

    return 0 if tracker.fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
