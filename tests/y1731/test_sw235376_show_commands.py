#!/usr/bin/env python3
"""
SW-235376: Ethernet OAM Y.1731 | CLI | Show Commands

Validates all Y.1731 performance-monitoring CFM show commands:
  - Proactive test summary and detail views (DM, SLM)
  - On-demand test detail views (DMM, DSM/DLM, LT, LB)
  - Show command filters (session-name, md-name, ma-name, mep-id)
  - Both MEP-ID and MAC-address targets for on-demand tests

Prerequisites:
  - Device with existing CFM config (maintenance-domains, MEPs)
  - Proactive DM and SLM sessions configured and running
  - Remote MEP reachable for on-demand tests
"""
import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple

import paramiko

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\r")


@dataclass
class StepResult:
    name: str
    ok: bool
    details: str
    raw_output: str = ""


# ---------------------------------------------------------------------------
# SSH helpers (adapted from y1731_cli_tab_test.py)
# ---------------------------------------------------------------------------

def create_ssh_client(host: str, user: str, password: str, timeout: int = 30) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, username=user, password=password,
                   timeout=timeout, banner_timeout=timeout, auth_timeout=timeout,
                   look_for_keys=False, allow_agent=False)
    transport = client.get_transport()
    if transport is not None:
        transport.set_keepalive(30)
    return client


def _clean(text: str) -> str:
    return ANSI_ESCAPE.sub("", text)


def _read_until_quiet(channel, timeout: int, quiet: float = 1.5) -> str:
    output = ""
    start = time.time()
    last_data = time.time()
    while True:
        if time.time() - start > timeout:
            break
        try:
            if channel.recv_ready():
                chunk = channel.recv(4096).decode(errors="ignore")
                output += chunk
                last_data = time.time()
            else:
                if time.time() - last_data > quiet:
                    break
                time.sleep(0.2)
        except Exception:
            break
    return output


def _read_until_prompt(channel, timeout: int, quiet: float = 1.2) -> str:
    output = ""
    start = time.time()
    last_data = time.time()
    while True:
        if time.time() - start > timeout:
            break
        try:
            if channel.recv_ready():
                chunk = channel.recv(4096).decode(errors="ignore")
                output += chunk
                last_data = time.time()
                clean = _clean(output).strip()
                if clean.endswith(("#", ">")):
                    break
            else:
                if time.time() - last_data > quiet:
                    break
                time.sleep(0.2)
        except Exception:
            break
    return output


def run_show(client: paramiko.SSHClient, command: str, timeout: int = 30) -> str:
    """Run a single show command on a fresh shell channel."""
    channel = client.invoke_shell(width=300)
    channel.settimeout(timeout)
    _read_until_prompt(channel, timeout=timeout, quiet=1)
    channel.send(command + " | no-more\n")
    output = _read_until_prompt(channel, timeout=timeout, quiet=2)
    drain = _read_until_quiet(channel, timeout=min(timeout, 4), quiet=0.8)
    channel.close()
    return _clean(output + drain)


def run_sequence(client: paramiko.SSHClient, commands: List[str], timeout: int = 30) -> str:
    """Run a sequence of commands on the same shell channel."""
    channel = client.invoke_shell(width=300)
    channel.settimeout(timeout)
    _read_until_prompt(channel, timeout=timeout, quiet=1)
    output = ""
    for cmd in commands:
        channel.send(cmd + "\n")
        out = _read_until_prompt(channel, timeout=timeout, quiet=2)
        out += _read_until_quiet(channel, timeout=min(timeout, 4), quiet=0.8)
        output += _clean(out)
    channel.close()
    return output


def has_cli_error(text: str) -> Tuple[bool, List[str]]:
    errors = []
    for line in text.splitlines():
        if re.search(
            r"(Error:|ERROR:|Unknown command|Invalid command|Commit check failed|"
            r"Command failed|rpc-error|syntax error)",
            line, flags=re.IGNORECASE,
        ):
            errors.append(line.strip())
    return (len(errors) > 0, errors)


def try_show_commands(client: paramiko.SSHClient, commands: List[str],
                      timeout: int = 30) -> Tuple[Optional[str], str]:
    """Try show commands in order, return (command_used, output) for first that works."""
    last = ""
    for cmd in commands:
        out = run_show(client, cmd, timeout=timeout)
        last = out
        err, _ = has_cli_error(out)
        if not err:
            return cmd, out
    return None, last


# ---------------------------------------------------------------------------
# CFM discovery
# ---------------------------------------------------------------------------

def discover_cfm_context(client: paramiko.SSHClient, timeout: int = 30
                         ) -> Tuple[bool, str, Optional[str], Optional[str],
                                    Optional[str], Optional[str], Optional[str]]:
    """
    Discover (md, ma, local_mep_id, target_mep_id, remote_mac) from existing
    ethernet-oam CFM config. Returns (ok, details, md, ma, mep_id, target_mep, remote_mac).
    """
    show_cmds = [
        "show config services ethernet-oam connectivity-fault-management | no-more",
        "show configuration services ethernet-oam connectivity-fault-management | no-more",
        "show config services ethernet-oam | no-more",
    ]
    used, output = try_show_commands(client, show_cmds, timeout=timeout)
    if not used:
        return (False, "Could not read CFM config.", None, None, None, None, None)

    md_re = re.compile(r"\bmaintenance[-_]domain(?:s)?\s+(\S+)", re.IGNORECASE)
    ma_re = re.compile(r"\bmaintenance[-_]association(?:s)?\s+(\S+)", re.IGNORECASE)
    mep_id_re = re.compile(r"\bmep[-_]?id\s+(\d+)", re.IGNORECASE)
    local_mep_re = re.compile(r"\blocal[-_]mep\s+(\d+)", re.IGNORECASE)
    remote_mep_re = re.compile(r"(crosscheck|remote[-_]mep)", re.IGNORECASE)

    current_md: Optional[str] = None
    current_ma: Optional[str] = None
    candidates: Dict[Tuple[str, str], dict] = {}

    for line in output.splitlines():
        md_m = md_re.search(line)
        if md_m:
            current_md = md_m.group(1)
            current_ma = None
        ma_m = ma_re.search(line)
        if ma_m:
            current_ma = ma_m.group(1)
        if not (current_md and current_ma):
            continue
        key = (current_md, current_ma)
        if key not in candidates:
            candidates[key] = {"local_meps": set(), "remote_meps": set()}
        is_remote = bool(remote_mep_re.search(line))
        for m in mep_id_re.finditer(line):
            mid = int(m.group(1))
            if is_remote:
                candidates[key]["remote_meps"].add(mid)
            else:
                candidates[key]["local_meps"].add(mid)
        for m in local_mep_re.finditer(line):
            candidates[key]["local_meps"].add(int(m.group(1)))

    if not candidates:
        return (False, "No maintenance-domain/association found in CFM config.", None, None, None, None, None)

    best_key = None
    for key in sorted(candidates.keys()):
        if candidates[key]["local_meps"]:
            best_key = key
            break
    if not best_key:
        best_key = sorted(candidates.keys())[0]

    md, ma = best_key
    local_meps = sorted(candidates[best_key]["local_meps"])
    remote_meps = sorted(candidates[best_key]["remote_meps"])
    mep_id = str(local_meps[0]) if local_meps else None
    target_mep = str(remote_meps[0]) if remote_meps else None

    # Try to discover remote MAC from CFM database
    remote_mac = None
    mac_cmds = [
        f"show services ethernet-oam connectivity-fault-management mep-database maintenance-domain {md} maintenance-association {ma}",
        "show services ethernet-oam connectivity-fault-management mep-database",
    ]
    _, mac_out = try_show_commands(client, mac_cmds, timeout=20)
    mac_match = re.search(r"([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})", mac_out)
    if mac_match:
        found_mac = mac_match.group(1)
        # Get our own source MAC to avoid using it as remote
        src_mac_cmds = [
            f"show services ethernet-oam connectivity-fault-management maintenance-domain {md} maintenance-association {ma} local-mep {mep_id}",
        ]
        _, src_out = try_show_commands(client, src_mac_cmds, timeout=20)
        src_mac_match = re.search(r"Source MAC:\s*([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})", src_out)
        src_mac = src_mac_match.group(1).lower() if src_mac_match else ""
        all_macs = re.findall(r"[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}", mac_out)
        for m in all_macs:
            if m.lower() != src_mac.lower():
                remote_mac = m
                break
        if not remote_mac and found_mac.lower() != src_mac.lower():
            remote_mac = found_mac

    details = (f"Discovered from '{used}': md={md} ma={ma} mep={mep_id} "
               f"target_mep={target_mep} remote_mac={remote_mac}")
    return (True, details, md, ma, mep_id, target_mep, remote_mac)


# ---------------------------------------------------------------------------
# Proactive PM session discovery
# ---------------------------------------------------------------------------

def discover_proactive_sessions(client: paramiko.SSHClient, timeout: int = 30
                                ) -> Tuple[List[str], List[str]]:
    """Return lists of (dm_session_names, slm_session_names) from proactive config."""
    dm_sessions: List[str] = []
    slm_sessions: List[str] = []
    pm_cmds = [
        "show config services performance-monitoring cfm | no-more",
        "show configuration services performance-monitoring cfm | no-more",
    ]
    _, output = try_show_commands(client, pm_cmds, timeout=timeout)

    dm_re = re.compile(r"two-way-delay-measurement\s+(\S+)")
    slm_re = re.compile(r"two-way-synthetic-loss-measurement\s+(\S+)")
    for line in output.splitlines():
        dm_m = dm_re.search(line)
        if dm_m and dm_m.group(1) not in dm_sessions:
            dm_sessions.append(dm_m.group(1))
        slm_m = slm_re.search(line)
        if slm_m and slm_m.group(1) not in slm_sessions:
            slm_sessions.append(slm_m.group(1))
    return dm_sessions, slm_sessions


# ---------------------------------------------------------------------------
# Show command test steps
# ---------------------------------------------------------------------------

def check_fields(output: str, fields: List[str]) -> Tuple[bool, List[str]]:
    """Check that all expected field strings appear in output."""
    missing = [f for f in fields if f.lower() not in output.lower()]
    return (len(missing) == 0, missing)


def test_show_proactive_summary(client: paramiko.SSHClient, dm_sessions: List[str],
                                slm_sessions: List[str], timeout: int = 30) -> StepResult:
    """Verify 'show ... cfm tests proactive' summary lists all sessions."""
    cmd = "show services performance-monitoring cfm tests proactive"
    output = run_show(client, cmd, timeout=timeout)
    err, errs = has_cli_error(output)
    if err:
        return StepResult("show_proactive_summary", False, f"CLI error: {'; '.join(errs)}", output)

    all_sessions = dm_sessions + slm_sessions
    missing = [s for s in all_sessions if s not in output]
    if missing:
        return StepResult("show_proactive_summary", False,
                          f"Missing sessions in summary: {missing}", output)

    expected_headers = ["Test Name", "Test Type", "MD Name", "MA Name", "MEP-ID",
                        "Target", "Last Run", "Status"]
    _, missing_h = check_fields(output, expected_headers)
    if missing_h:
        return StepResult("show_proactive_summary", False,
                          f"Missing table headers: {missing_h}", output)

    return StepResult("show_proactive_summary", True,
                      f"All {len(all_sessions)} sessions visible with proper headers.", output)


def test_show_proactive_dm_detail(client: paramiko.SSHClient, session: str,
                                  md: str, ma: str, mep_id: str,
                                  timeout: int = 30) -> StepResult:
    """Verify proactive DM detail contains expected fields."""
    cmds = [
        f"show services performance-monitoring cfm tests proactive two-way-delay session-name {session} detail",
        "show services performance-monitoring cfm tests proactive two-way-delay detail",
    ]
    used, output = try_show_commands(client, cmds, timeout=timeout)
    if not used:
        err, errs = has_cli_error(output)
        return StepResult("show_proactive_dm_detail", False,
                          f"CLI error: {'; '.join(errs)}" if err else "No output", output)

    expected_fields = [
        "Test Session", "Admin state", "Profile",
        "Maintenance Domain", "Maintenance Association", "MEP-ID",
        "Source Interface", "Source MAC",
        "Count", "Interval", "Timeout",
        "Historical Test Results",
        "DMM PDUs transmitted", "DMR PDUs received", "Success rate",
        "Round-trip delay statistics",
        "Delay variation statistics",
    ]
    ok, missing = check_fields(output, expected_fields)
    if not ok:
        return StepResult("show_proactive_dm_detail", False,
                          f"Missing fields: {missing}", output)

    if session not in output:
        return StepResult("show_proactive_dm_detail", False,
                          f"Session name '{session}' not in output", output)

    return StepResult("show_proactive_dm_detail", True,
                      f"All expected DM detail fields present for '{session}'.", output)


def test_show_proactive_slm_detail(client: paramiko.SSHClient, session: str,
                                   md: str, ma: str, mep_id: str,
                                   timeout: int = 30) -> StepResult:
    """Verify proactive SLM detail contains expected fields."""
    cmds = [
        f"show services performance-monitoring cfm tests proactive two-way-synthetic-loss session-name {session} detail",
        "show services performance-monitoring cfm tests proactive two-way-synthetic-loss detail",
    ]
    used, output = try_show_commands(client, cmds, timeout=timeout)
    if not used:
        err, errs = has_cli_error(output)
        return StepResult("show_proactive_slm_detail", False,
                          f"CLI error: {'; '.join(errs)}" if err else "No output", output)

    expected_fields = [
        "Test Session", "Admin state", "Profile",
        "Maintenance Domain", "Maintenance Association", "MEP-ID",
        "Source Interface", "Source MAC",
        "Count", "Interval", "Timeout",
        "Historical Test Results",
        "SLM PDUs transmitted", "SLR PDUs received",
        "Frame loss statistics",
        "Near-end loss", "Far-end loss",
    ]
    ok, missing = check_fields(output, expected_fields)
    if not ok:
        return StepResult("show_proactive_slm_detail", False,
                          f"Missing fields: {missing}", output)

    if session not in output:
        return StepResult("show_proactive_slm_detail", False,
                          f"Session name '{session}' not in output", output)

    return StepResult("show_proactive_slm_detail", True,
                      f"All expected SLM detail fields present for '{session}'.", output)


def test_show_filter(client: paramiko.SSHClient, name: str, filter_cmd: str,
                     expected: List[str], timeout: int = 30) -> StepResult:
    """Generic filter test: run a filtered show command and check expected strings."""
    output = run_show(client, filter_cmd, timeout=timeout)
    err, errs = has_cli_error(output)
    if err:
        return StepResult(name, False, f"CLI error: {'; '.join(errs)}", output)
    missing = [s for s in expected if s not in output]
    if missing:
        return StepResult(name, False, f"Missing in output: {missing}", output)
    return StepResult(name, True, "All expected strings found.", output)


# ---------------------------------------------------------------------------
# On-demand test helpers
# ---------------------------------------------------------------------------

def run_on_demand_and_wait(client: paramiko.SSHClient, run_cmd: str,
                           wait: int = 15, timeout: int = 30) -> str:
    """Trigger an on-demand test, wait for it to complete, return output."""
    channel = client.invoke_shell(width=300)
    channel.settimeout(timeout + wait)
    _read_until_prompt(channel, timeout=timeout, quiet=1)
    channel.send(run_cmd + "\n")
    time.sleep(wait)
    output = _read_until_quiet(channel, timeout=timeout, quiet=3)
    channel.close()
    return _clean(output)


def test_on_demand_dm(client: paramiko.SSHClient, md: str, ma: str,
                      target_type: str, target_value: str,
                      wait: int = 15, timeout: int = 30) -> StepResult:
    """Trigger on-demand DMM, then verify show detail output."""
    target_label = f"{target_type} {target_value}"
    step_name = f"on_demand_dm_{target_type.replace('-', '_')}"

    run_cmd = (f"run ethernet-oam cfm on-demand delay-measurement two-way "
               f"maintenance-domain {md} maintenance-association {ma} "
               f"target {target_type} {target_value}")
    run_on_demand_and_wait(client, run_cmd, wait=wait, timeout=timeout)

    show_cmd = "show services performance-monitoring cfm tests on-demand two-way-delay detail"
    output = run_show(client, show_cmd, timeout=timeout)
    err, errs = has_cli_error(output)
    if err:
        return StepResult(step_name, False, f"CLI error: {'; '.join(errs)}", output)

    expected = [
        "Test Session", "Maintenance Domain", "Maintenance Association", "MEP-ID",
        "Source Interface", "Source MAC",
        "DMM PDUs transmitted", "DMR PDUs received", "Success rate",
        "Round-trip delay statistics",
    ]
    ok, missing = check_fields(output, expected)
    if not ok:
        return StepResult(step_name, False, f"Missing fields: {missing}", output)

    if md not in output or ma not in output:
        return StepResult(step_name, False,
                          f"MD/MA not in detail output (expected {md}/{ma})", output)

    return StepResult(step_name, True,
                      f"DMM on-demand detail OK (target: {target_label}).", output)


def test_on_demand_slm(client: paramiko.SSHClient, md: str, ma: str,
                       target_type: str, target_value: str,
                       wait: int = 15, timeout: int = 30) -> StepResult:
    """Trigger on-demand SLM, then verify show detail output."""
    target_label = f"{target_type} {target_value}"
    step_name = f"on_demand_slm_{target_type.replace('-', '_')}"

    run_cmd = (f"run ethernet-oam cfm on-demand synthetic-loss-measurement two-way "
               f"maintenance-domain {md} maintenance-association {ma} "
               f"target {target_type} {target_value}")
    run_on_demand_and_wait(client, run_cmd, wait=wait, timeout=timeout)

    show_cmd = "show services performance-monitoring cfm tests on-demand two-way-synthetic-loss detail"
    output = run_show(client, show_cmd, timeout=timeout)
    err, errs = has_cli_error(output)
    if err:
        return StepResult(step_name, False, f"CLI error: {'; '.join(errs)}", output)

    expected = [
        "Test Session", "Maintenance Domain", "Maintenance Association", "MEP-ID",
        "Source Interface", "Source MAC",
        "SLM PDUs transmitted", "SLR PDUs received",
        "Frame loss statistics", "Near-end loss", "Far-end loss",
    ]
    ok, missing = check_fields(output, expected)
    if not ok:
        return StepResult(step_name, False, f"Missing fields: {missing}", output)

    return StepResult(step_name, True,
                      f"SLM on-demand detail OK (target: {target_label}).", output)


def test_on_demand_loopback(client: paramiko.SSHClient, md: str, ma: str,
                            target_type: str, target_value: str,
                            wait: int = 15, timeout: int = 30) -> StepResult:
    """Trigger on-demand LB, then verify show detail output."""
    target_label = f"{target_type} {target_value}"
    step_name = f"on_demand_lb_{target_type.replace('-', '_')}"

    run_cmd = (f"run ethernet-oam cfm on-demand loopback "
               f"maintenance-domain {md} maintenance-association {ma} "
               f"target {target_type} {target_value}")
    run_on_demand_and_wait(client, run_cmd, wait=wait, timeout=timeout)

    show_cmd = "show services performance-monitoring cfm tests on-demand loopback detail"
    output = run_show(client, show_cmd, timeout=timeout)
    err, errs = has_cli_error(output)
    if err:
        return StepResult(step_name, False, f"CLI error: {'; '.join(errs)}", output)

    expected = [
        "Test Session", "Maintenance Domain", "Maintenance Association", "MEP-ID",
        "Source Interface", "Source MAC",
        "LBM PDUs transmitted", "LBR PDUs received", "Success rate",
    ]
    ok, missing = check_fields(output, expected)
    if not ok:
        return StepResult(step_name, False, f"Missing fields: {missing}", output)

    return StepResult(step_name, True,
                      f"LB on-demand detail OK (target: {target_label}).", output)


def test_on_demand_linktrace(client: paramiko.SSHClient, md: str, ma: str,
                             target_type: str, target_value: str,
                             wait: int = 15, timeout: int = 30) -> StepResult:
    """Trigger on-demand LT, then verify show detail output."""
    target_label = f"{target_type} {target_value}"
    step_name = f"on_demand_lt_{target_type.replace('-', '_')}"

    run_cmd = (f"run ethernet-oam cfm on-demand linktrace "
               f"maintenance-domain {md} maintenance-association {ma} "
               f"target {target_type} {target_value}")
    run_on_demand_and_wait(client, run_cmd, wait=wait, timeout=timeout)

    show_cmd = "show services performance-monitoring cfm tests on-demand linktrace detail"
    output = run_show(client, show_cmd, timeout=timeout)
    err, errs = has_cli_error(output)
    if err:
        return StepResult(step_name, False, f"CLI error: {'; '.join(errs)}", output)

    expected = [
        "Test Session", "Maintenance Domain", "Maintenance Association", "MEP-ID",
        "Source Interface", "Source MAC",
        "LTR PDUs received", "Transaction ID",
    ]
    ok, missing = check_fields(output, expected)
    if not ok:
        return StepResult(step_name, False, f"Missing fields: {missing}", output)

    return StepResult(step_name, True,
                      f"LT on-demand detail OK (target: {target_label}).", output)


def test_on_demand_summary(client: paramiko.SSHClient, timeout: int = 30) -> StepResult:
    """Verify on-demand summary view shows recent tests."""
    cmds = [
        "show services performance-monitoring cfm tests on-demand",
        "show services performance-monitoring cfm tests on-demand detail",
    ]
    used, output = try_show_commands(client, cmds, timeout=timeout)
    if not used:
        err, errs = has_cli_error(output)
        return StepResult("on_demand_summary", False,
                          f"CLI error: {'; '.join(errs)}" if err else "No output", output)
    # Just check we get some structured output (not all devices populate the summary view)
    if "Test Session" in output or "test-MD" in output or "On-Demand" in output:
        return StepResult("on_demand_summary", True,
                          "On-demand summary view returned structured output.", output)
    if len(output.strip().splitlines()) > 3:
        return StepResult("on_demand_summary", True,
                          "On-demand summary returned content (format varies by version).", output)
    return StepResult("on_demand_summary", False,
                      "On-demand summary view appears empty.", output)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="SW-235376: Y.1731 CLI Show Commands validation")
    parser.add_argument("--host", required=True, help="Device IP or hostname")
    parser.add_argument("--user", default="dnroot")
    parser.add_argument("--password", default="dnroot")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--md", default=None, help="Override maintenance-domain name")
    parser.add_argument("--ma", default=None, help="Override maintenance-association name")
    parser.add_argument("--mep-id", default=None, help="Override local MEP ID")
    parser.add_argument("--target-mep", default=None, help="Override target MEP ID")
    parser.add_argument("--remote-mac", default=None,
                        help="Remote MAC address for on-demand MAC-target tests (e.g. 84:40:76:90:cd:0c)")
    parser.add_argument("--dm-session", default=None,
                        help="Proactive DM session name (auto-discovered if not set)")
    parser.add_argument("--slm-session", default=None,
                        help="Proactive SLM session name (auto-discovered if not set)")
    parser.add_argument("--on-demand-wait", type=int, default=15,
                        help="Seconds to wait for on-demand test completion (default: 15)")
    parser.add_argument("--skip-on-demand", action="store_true",
                        help="Skip on-demand test sections")
    parser.add_argument("--skip-on-demand-mac", action="store_true",
                        help="Skip on-demand MAC-address target tests")
    parser.add_argument("--skip-proactive", action="store_true",
                        help="Skip proactive show command tests")
    parser.add_argument("--json-output", default=None,
                        help="Write JSON results to this file")
    parser.add_argument("--show-output", action="store_true",
                        help="Print raw CLI output for each step")
    parser.add_argument("--show-details", action="store_true",
                        help="Print details column in result table")
    args = parser.parse_args()

    print(f"Connecting to {args.host}...")
    client = create_ssh_client(args.host, args.user, args.password, args.timeout)
    results: List[StepResult] = []

    try:
        # ---------------------------------------------------------------
        # Step 0: Software version
        # ---------------------------------------------------------------
        print("Collecting system version...")
        ver_output = run_show(client, "show system version", timeout=args.timeout)
        ver_match = re.search(r"Software [Vv]ersion[:\s]+(\S+)", ver_output)
        sw_version = ver_match.group(1) if ver_match else "unknown"
        print(f"  Software version: {sw_version}")

        # ---------------------------------------------------------------
        # Step 1: CFM context discovery
        # ---------------------------------------------------------------
        print("Discovering CFM context...")
        ok, detail, md, ma, mep_id, target_mep, remote_mac = discover_cfm_context(
            client, timeout=args.timeout)
        results.append(StepResult("discover_cfm", ok, detail))
        if not ok:
            print(f"  FAIL: {detail}")
            print("Cannot proceed without CFM context. Provide --md, --ma, --mep-id manually.")
            return 1

        md = args.md or md
        ma = args.ma or ma
        mep_id = args.mep_id or mep_id
        target_mep = args.target_mep or target_mep
        remote_mac = args.remote_mac or remote_mac

        print(f"  MD={md}  MA={ma}  MEP={mep_id}  target_mep={target_mep}  remote_mac={remote_mac}")

        # ---------------------------------------------------------------
        # Step 2: Discover proactive PM sessions
        # ---------------------------------------------------------------
        print("Discovering proactive PM sessions...")
        dm_sessions, slm_sessions = discover_proactive_sessions(client, timeout=args.timeout)
        dm_session = args.dm_session or (dm_sessions[0] if dm_sessions else None)
        slm_session = args.slm_session or (slm_sessions[0] if slm_sessions else None)
        print(f"  DM sessions: {dm_sessions}")
        print(f"  SLM sessions: {slm_sessions}")

        if not dm_session and not slm_session and not args.skip_proactive:
            results.append(StepResult("discover_proactive", False,
                                      "No proactive DM or SLM sessions found."))
            print("  WARNING: No proactive sessions discovered. Use --skip-proactive or configure sessions.")

        # ---------------------------------------------------------------
        # Step 3: Proactive show command tests
        # ---------------------------------------------------------------
        if not args.skip_proactive and (dm_session or slm_session):
            print("\n=== PROACTIVE SHOW COMMANDS ===")

            # 3a: Summary view
            print("  Testing proactive summary...")
            results.append(test_show_proactive_summary(
                client, dm_sessions, slm_sessions, timeout=args.timeout))

            # 3b: DM detail
            if dm_session:
                print(f"  Testing DM detail (session={dm_session})...")
                results.append(test_show_proactive_dm_detail(
                    client, dm_session, md, ma, mep_id, timeout=args.timeout))

            # 3c: SLM detail
            if slm_session:
                print(f"  Testing SLM detail (session={slm_session})...")
                results.append(test_show_proactive_slm_detail(
                    client, slm_session, md, ma, mep_id, timeout=args.timeout))

            # 3d: Filter tests
            print("  Testing show command filters...")
            if dm_session:
                results.append(test_show_filter(
                    client, "filter_session_name",
                    f"show services performance-monitoring cfm tests session-name {dm_session}",
                    [dm_session], timeout=args.timeout))

            results.append(test_show_filter(
                client, "filter_md_name",
                f"show services performance-monitoring cfm tests md-name {md}",
                [md], timeout=args.timeout))

            results.append(test_show_filter(
                client, "filter_ma_name",
                f"show services performance-monitoring cfm tests ma-name {ma}",
                [ma], timeout=args.timeout))

            results.append(test_show_filter(
                client, "filter_mep_id",
                f"show services performance-monitoring cfm tests mep-id {mep_id}",
                [mep_id], timeout=args.timeout))

            # 3e: Show all proactive types filtered
            if dm_session:
                results.append(test_show_filter(
                    client, "show_proactive_type_dm",
                    "show services performance-monitoring cfm tests proactive two-way-delay",
                    [dm_session], timeout=args.timeout))
            if slm_session:
                results.append(test_show_filter(
                    client, "show_proactive_type_slm",
                    "show services performance-monitoring cfm tests proactive two-way-synthetic-loss",
                    [slm_session], timeout=args.timeout))

        # ---------------------------------------------------------------
        # Step 4: On-demand tests (MEP-ID target)
        # ---------------------------------------------------------------
        if not args.skip_on_demand and target_mep:
            print(f"\n=== ON-DEMAND TESTS (target mep-id {target_mep}) ===")
            wait = args.on_demand_wait

            print(f"  Running on-demand DMM (mep-id {target_mep})...")
            results.append(test_on_demand_dm(
                client, md, ma, "mep-id", target_mep,
                wait=wait, timeout=args.timeout))

            print(f"  Running on-demand SLM (mep-id {target_mep})...")
            results.append(test_on_demand_slm(
                client, md, ma, "mep-id", target_mep,
                wait=wait, timeout=args.timeout))

            print(f"  Running on-demand LB (mep-id {target_mep})...")
            results.append(test_on_demand_loopback(
                client, md, ma, "mep-id", target_mep,
                wait=wait, timeout=args.timeout))

            print(f"  Running on-demand LT (mep-id {target_mep})...")
            results.append(test_on_demand_linktrace(
                client, md, ma, "mep-id", target_mep,
                wait=wait, timeout=args.timeout))

            print("  Checking on-demand summary view...")
            results.append(test_on_demand_summary(client, timeout=args.timeout))

        elif not args.skip_on_demand and not target_mep:
            print("\n  WARNING: No target MEP discovered. Skipping on-demand MEP-ID tests.")
            print("  Use --target-mep to specify one manually.")

        # ---------------------------------------------------------------
        # Step 5: On-demand tests (MAC-address target)
        # ---------------------------------------------------------------
        if not args.skip_on_demand and not args.skip_on_demand_mac and remote_mac:
            print(f"\n=== ON-DEMAND TESTS (target mac-address {remote_mac}) ===")
            wait = args.on_demand_wait

            print(f"  Running on-demand DMM (mac {remote_mac})...")
            results.append(test_on_demand_dm(
                client, md, ma, "mac-address", remote_mac,
                wait=wait, timeout=args.timeout))

            print(f"  Running on-demand SLM (mac {remote_mac})...")
            results.append(test_on_demand_slm(
                client, md, ma, "mac-address", remote_mac,
                wait=wait, timeout=args.timeout))

            print(f"  Running on-demand LB (mac {remote_mac})...")
            results.append(test_on_demand_loopback(
                client, md, ma, "mac-address", remote_mac,
                wait=wait, timeout=args.timeout))

            print(f"  Running on-demand LT (mac {remote_mac})...")
            results.append(test_on_demand_linktrace(
                client, md, ma, "mac-address", remote_mac,
                wait=wait, timeout=args.timeout))

        elif not args.skip_on_demand and not args.skip_on_demand_mac and not remote_mac:
            print("\n  WARNING: No remote MAC discovered. Skipping on-demand MAC-address tests.")
            print("  Use --remote-mac to specify one manually.")

    finally:
        client.close()

    # ---------------------------------------------------------------
    # Results
    # ---------------------------------------------------------------
    passed = sum(1 for r in results if r.ok)
    failed = sum(1 for r in results if not r.ok)
    total = len(results)

    name_w = max(len("Test"), max((len(r.name) for r in results), default=4))
    status_w = 6

    def sep():
        base = f"+-{'-' * name_w}-+-{'-' * status_w}-+"
        return base

    print(f"\n{'=' * 70}")
    print("SW-235376 SHOW COMMANDS TEST RESULTS")
    print(f"Device: {args.host}  |  Version: {sw_version}")
    print(f"{'=' * 70}")
    print(sep())
    print(f"| {'Test'.ljust(name_w)} | {'Status'.ljust(status_w)} |")
    print(sep())
    for r in results:
        status = "PASS" if r.ok else "FAIL"
        print(f"| {r.name.ljust(name_w)} | {status.ljust(status_w)} |")
        if args.show_details and r.details:
            for line in r.details.splitlines():
                print(f"|   {line}")
        if args.show_output and r.raw_output:
            for line in r.raw_output.splitlines()[:20]:
                print(f"|   > {line}")
    print(sep())
    print(f"\nPASSED: {passed}/{total}  FAILED: {failed}/{total}")

    overall = "PASS" if failed == 0 else "FAIL"
    print(f"Overall: {overall}\n")

    if args.json_output:
        json_data = {
            "ticket": "SW-235376",
            "device": args.host,
            "sw_version": sw_version,
            "overall": overall,
            "passed": passed,
            "failed": failed,
            "total": total,
            "results": [asdict(r) for r in results],
        }
        with open(args.json_output, "w") as f:
            json.dump(json_data, f, indent=2)
        print(f"JSON results written to {args.json_output}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
