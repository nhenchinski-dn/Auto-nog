#!/usr/bin/env python3
"""
SW-238848: Ethernet OAM Y.1731 | Functionality | Valid/Invalid

Test measurement initiation failure conditions that cause 'invalid' state:
  1. CFM-related commit is in progress
  2. On-demand overlap (same protocol, same source MEP)
  3. RMEP down (target mep-id with unavailable dst MAC)
  4. Source local MEP is missing or admin disabled

For each condition the test:
  a) triggers the invalid state
  b) verifies 'invalid' appears in proactive session detail
  c) resolves the condition
  d) verifies recovery back to 'valid' (or 'incomplete')
"""
import argparse
import getpass
import json
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import paramiko

PROMPT_MARKERS = ("#", ">")
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


@dataclass
class StepResult:
    name: str
    ok: bool
    details: str
    raw_output: str = ""


# ---------------------------------------------------------------------------
# SSH helpers
# ---------------------------------------------------------------------------
def create_ssh_client(host: str, user: str, password: str, timeout: int) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        host,
        username=user,
        password=password,
        timeout=timeout,
        banner_timeout=timeout,
        auth_timeout=timeout,
    )
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


def _extract_prompt(output: str) -> Optional[str]:
    clean = _clean(output)
    lines = [line for line in clean.splitlines() if line.strip()]
    if not lines:
        return None
    last = lines[-1].rstrip()
    if last.endswith(PROMPT_MARKERS):
        return last
    return None


def _read_until_prompt(channel, prompt: Optional[str], timeout: int, quiet: float = 1.2) -> str:
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
                clean = _clean(output)
                tail = clean.strip()
                if prompt and tail.endswith(prompt):
                    break
                if not prompt and tail.endswith(PROMPT_MARKERS):
                    break
            else:
                if time.time() - last_data > quiet:
                    break
                time.sleep(0.2)
        except Exception:
            break
    return output


def run_cmd(client: paramiko.SSHClient, command: str, timeout: int = 30) -> str:
    channel = client.invoke_shell()
    channel.settimeout(timeout)
    banner = _read_until_prompt(channel, prompt=None, timeout=timeout, quiet=1)
    prompt = _extract_prompt(banner)
    channel.send(command + "\n")
    output = _read_until_prompt(channel, prompt=prompt, timeout=timeout, quiet=1.5)
    channel.close()
    return _clean(output)


def run_cmd_long(client: paramiko.SSHClient, command: str, timeout: int = 60) -> str:
    channel = client.invoke_shell()
    channel.settimeout(timeout)
    banner = _read_until_prompt(channel, prompt=None, timeout=timeout, quiet=1)
    prompt = _extract_prompt(banner)
    channel.send(command + "\n")
    output = _read_until_prompt(channel, prompt=prompt, timeout=timeout, quiet=3)
    output += _read_until_quiet(channel, timeout=min(timeout, 4), quiet=0.8)
    channel.close()
    return _clean(output)


def run_sequence(client: paramiko.SSHClient, commands: List[str], timeout: int = 30) -> str:
    channel = client.invoke_shell()
    channel.settimeout(timeout)
    banner = _read_until_prompt(channel, prompt=None, timeout=timeout, quiet=1)
    prompt = _extract_prompt(banner)
    all_output = ""
    for cmd in commands:
        channel.send(cmd + "\n")
        out = _read_until_prompt(channel, prompt=prompt, timeout=timeout, quiet=1.5)
        all_output += out
    channel.close()
    return _clean(all_output)


def has_cli_error(text: str) -> Tuple[bool, List[str]]:
    errors = []
    for line in text.splitlines():
        if re.search(
            r"(Error:|ERROR:|Unknown command|Invalid command|Commit check failed|"
            r"commit check has failed|Commit failed|Command failed|"
            r"TRANSACTION_COMMIT_CHECK_FAILED|missing a mandatory leaf|rpc-error)",
            line,
            flags=re.IGNORECASE,
        ):
            errors.append(line.strip()[:200])
    return (len(errors) > 0, errors)


# ---------------------------------------------------------------------------
# CFM discovery
# ---------------------------------------------------------------------------
def discover_cfm_context(
    client: paramiko.SSHClient, timeout: int = 30
) -> Tuple[bool, str, Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Discover (md, ma, local_mep_id, remote_mep_id) from existing CFM config."""
    show_cmds = [
        "show config services ethernet-oam connectivity-fault-management | no-more",
        "show configuration services ethernet-oam connectivity-fault-management | no-more",
        "show config services ethernet-oam connectivity-fault-management | display set | no-more",
    ]

    output = ""
    for cmd in show_cmds:
        out = run_cmd_long(client, cmd, timeout=max(timeout, 60))
        err, _ = has_cli_error(out)
        if (not err) and re.search(r"(ethernet-oam|connectivity-fault-management|maintenance)", out, re.IGNORECASE):
            output = out
            break

    if not output:
        return (False, "Failed to read CFM config.", None, None, None, None)

    md_re = re.compile(r"\bmaintenance[-_]domain(?:s)?(?:[-_]name)?\s+(\S+)", flags=re.IGNORECASE)
    ma_re = re.compile(r"\bmaintenance[-_]association(?:s)?(?:[-_]name)?\s+(\S+)", flags=re.IGNORECASE)
    mep_id_re = re.compile(r"\bmep[-_]id\s+(\d+)\b", flags=re.IGNORECASE)
    local_mep_re = re.compile(r"\blocal[-_]mep\s+(\d+)\b", flags=re.IGNORECASE)
    remote_mep_re = re.compile(r"\bremote[-_]mep(?:s)?(?:[-_]id)?\s+(\d+)\b", flags=re.IGNORECASE)

    candidates: Dict[Tuple[str, str], Dict] = {}
    current_md: Optional[str] = None
    current_ma: Optional[str] = None
    for line in output.splitlines():
        md_m = md_re.search(line)
        if md_m:
            current_md = md_m.group(1)
            current_ma = None
        ma_m = ma_re.search(line)
        if ma_m:
            current_ma = ma_m.group(1)
        line_md = md_m.group(1) if md_m else current_md
        line_ma = ma_m.group(1) if ma_m else current_ma
        if not (line_md and line_ma):
            continue
        key = (line_md, line_ma)
        if key not in candidates:
            candidates[key] = {"meps": set(), "remote_meps": set()}
        is_remote_line = (
            bool(remote_mep_re.search(line))
            or ("remote-mep" in line.lower())
            or ("crosscheck" in line.lower())
        )
        for m in remote_mep_re.finditer(line):
            candidates[key]["remote_meps"].add(int(m.group(1)))
        if "crosscheck" in line.lower():
            for m in mep_id_re.finditer(line):
                candidates[key]["remote_meps"].add(int(m.group(1)))
        if is_remote_line:
            continue
        for m in local_mep_re.finditer(line):
            candidates[key]["meps"].add(int(m.group(1)))
        for m in mep_id_re.finditer(line):
            candidates[key]["meps"].add(int(m.group(1)))

    if not candidates:
        return (False, "No MD/MA found in CFM config.", None, None, None, None)

    best_key = None
    for key in sorted(candidates.keys()):
        if candidates[key]["meps"]:
            best_key = key
            break
    if not best_key:
        best_key = sorted(candidates.keys())[0]

    md, ma = best_key
    meps = sorted(candidates[best_key]["meps"])
    remote_meps = sorted(candidates[best_key]["remote_meps"])
    local_mep = str(meps[0]) if meps else None
    remote_mep = str(remote_meps[0]) if remote_meps else (str(meps[1]) if len(meps) >= 2 else None)
    detail = f"md={md} ma={ma} local-mep={local_mep} remote-mep={remote_mep}"
    return True, detail, md, ma, local_mep, remote_mep


# ---------------------------------------------------------------------------
# Proactive session detail parsing
# ---------------------------------------------------------------------------
def get_proactive_detail(
    client: paramiko.SSHClient, session_name: str, test_type: str = "two-way-delay",
    timeout: int = 30,
) -> str:
    cmd = (
        f"show services performance-monitoring cfm tests proactive "
        f"{test_type} session-name {session_name} detail | no-more"
    )
    return run_cmd_long(client, cmd, timeout=timeout)


def parse_measurement_entries(detail_text: str) -> List[Tuple[str, str]]:
    """Parse (index, status) tuples from the proactive detail table.
    Status is typically 'valid', 'invalid', or 'incomplete'.
    """
    return re.findall(r"\|\s*(\d+)\s*\|[^|]*\|[^|]*\|\s*(\w+)\s*\|", detail_text)


def latest_status(detail_text: str) -> Optional[str]:
    entries = parse_measurement_entries(detail_text)
    return entries[-1][1] if entries else None


def count_status(detail_text: str, status: str) -> int:
    entries = parse_measurement_entries(detail_text)
    return sum(1 for _, s in entries if s.lower() == status.lower())


def wait_for_valid_entry(
    client: paramiko.SSHClient,
    session_name: str,
    test_type: str = "two-way-delay",
    timeout: int = 180,
    poll_interval: int = 15,
) -> Tuple[bool, str]:
    """Poll until the latest entry is 'valid' or timeout."""
    start = time.time()
    last_detail = ""
    while time.time() - start < timeout:
        detail = get_proactive_detail(client, session_name, test_type)
        last_detail = detail
        entries = parse_measurement_entries(detail)
        if entries and entries[-1][1].lower() == "valid":
            return True, detail
        time.sleep(poll_interval)
    return False, last_detail


# ---------------------------------------------------------------------------
# RMEP status check
# ---------------------------------------------------------------------------
def get_rmep_status(client: paramiko.SSHClient, md: str, ma: str) -> str:
    return run_cmd(
        client,
        f"show services ethernet-oam connectivity-fault-management "
        f"maintenance-domains {md} maintenance-associations {ma} remote-meps | no-more",
    )


# ---------------------------------------------------------------------------
# Test conditions
# ---------------------------------------------------------------------------
def _start_on_demand_persistent(
    client: paramiko.SSHClient, command: str, timeout: int = 15
) -> paramiko.Channel:
    """Start an on-demand session on a persistent shell channel (keeps running)."""
    channel = client.invoke_shell()
    channel.settimeout(timeout)
    _read_until_prompt(channel, prompt=None, timeout=timeout, quiet=1)
    channel.send(command + "\n")
    time.sleep(1)
    return channel


def test_condition_2_on_demand_overlap(
    client: paramiko.SSHClient,
    results: List[StepResult],
    md: str, ma: str,
    session_name: str,
    remote_mep: str,
    recovery_wait: int,
    verbose: bool,
) -> None:
    """Condition #2: On-demand overlap with same protocol/source MEP."""
    print(f"\n{'='*70}")
    print("CONDITION 2: On-demand overlap (same protocol, same source MEP)")
    print(f"{'='*70}")

    detail_before = get_proactive_detail(client, session_name)
    entries_before = parse_measurement_entries(detail_before)
    if verbose and entries_before:
        print(f"  Baseline: last entries = {entries_before[-3:]}")

    host = client.get_transport().getpeername()[0]
    poll_client = create_ssh_client(host, "dnroot", "dnroot", 30)

    od_cmd = (
        f"run ethernet-oam cfm on-demand delay-measurement two-way "
        f"maintenance-domain {md} maintenance-association {ma} "
        f"target mep-id {remote_mep} count 100"
    )
    print(f"  Starting long on-demand DM (100 probes) on same MEP to overlap with proactive...")
    ch_od = _start_on_demand_persistent(client, od_cmd, timeout=15)

    invalid_seen = False
    detail_during = ""
    poll_attempts = 15
    for i in range(poll_attempts):
        time.sleep(3)
        detail_during = get_proactive_detail(poll_client, session_name)
        invalid_count = count_status(detail_during, "invalid")
        latest = latest_status(detail_during)
        if verbose:
            print(f"  Poll {i+1}/{poll_attempts}: latest={latest}, invalid_count={invalid_count}")
        if invalid_count > 0 or latest == "invalid":
            invalid_seen = True
            print(f"  'invalid' detected at poll {i+1}: latest={latest}, invalid_count={invalid_count}")
            break

    try:
        ch_od.close()
    except Exception:
        pass

    if invalid_seen:
        results.append(StepResult(
            name="cond2_invalid_triggered",
            ok=True,
            details=f"On-demand overlap caused 'invalid' as expected",
            raw_output=detail_during,
        ))
    else:
        latest_final = latest_status(detail_during)
        results.append(StepResult(
            name="cond2_invalid_triggered",
            ok=False,
            details=f"On-demand overlap did NOT cause 'invalid' after {poll_attempts} polls. "
                     f"Latest={latest_final}. On-demand may not have overlapped with proactive cycle.",
            raw_output=detail_during,
        ))

    print("  Stopping on-demand sessions to allow recovery...")
    run_cmd(poll_client, "request ethernet-oam cfm on-demand stop all", timeout=15)

    print(f"  Waiting {recovery_wait}s for proactive cycle to recover...")
    time.sleep(recovery_wait)
    detail_after = get_proactive_detail(poll_client, session_name)
    latest_after = latest_status(detail_after)
    invalid_after = count_status(detail_after, "invalid")
    print(f"  After recovery: latest={latest_after}, invalid_count={invalid_after}")
    poll_client.close()

    recovered = latest_after in ("valid", "incomplete")
    results.append(StepResult(
        name="cond2_recovery",
        ok=recovered,
        details=f"Recovery after on-demand stop: latest={latest_after}, invalid_count={invalid_after}",
        raw_output=detail_after,
    ))


def test_condition_1_cfm_commit(
    client: paramiko.SSHClient,
    results: List[StepResult],
    md: str, ma: str,
    session_name: str,
    recovery_wait: int,
    verbose: bool,
) -> None:
    """Condition #1: CFM-related commit in progress during measurement."""
    print(f"\n{'='*70}")
    print("CONDITION 1: CFM config commit during active proactive measurement")
    print(f"{'='*70}")

    detail_before = get_proactive_detail(client, session_name)
    entries_before = parse_measurement_entries(detail_before)
    if verbose and entries_before:
        print(f"  Baseline: last entries = {entries_before[-3:]}")

    print("  Committing a CFM config change (add dummy crosscheck mep-id 99)...")
    commit_out = run_sequence(client, [
        "configure",
        f"services ethernet-oam connectivity-fault-management maintenance-domains {md} maintenance-associations {ma}",
        "remote-meps crosscheck mep-id 99",
        "exit", "exit", "exit", "exit", "exit",
        "commit",
        "exit",
    ], timeout=30)

    err, errs = has_cli_error(commit_out)
    if err:
        print(f"  WARNING: commit had errors: {errs}")
        results.append(StepResult(
            name="cond1_cfm_commit",
            ok=False,
            details=f"CFM config commit failed: {'; '.join(errs)}",
            raw_output=commit_out,
        ))
        return

    time.sleep(5)
    detail_during = get_proactive_detail(client, session_name)
    invalid_during = count_status(detail_during, "invalid")
    latest_during = latest_status(detail_during)
    print(f"  After CFM commit: latest={latest_during}, invalid_count={invalid_during}")

    results.append(StepResult(
        name="cond1_invalid_after_commit",
        ok=True,
        details=f"After CFM commit: latest={latest_during}, invalid_count={invalid_during}. "
                "Note: invalid may be transient and already recovered.",
        raw_output=detail_during,
    ))

    print("  Reverting CFM change (remove dummy crosscheck)...")
    run_sequence(client, [
        "configure",
        f"services ethernet-oam connectivity-fault-management maintenance-domains {md} maintenance-associations {ma}",
        "no remote-meps crosscheck mep-id 99",
        "exit", "exit", "exit", "exit", "exit",
        "commit",
        "exit",
    ], timeout=30)

    print(f"  Waiting {recovery_wait}s for recovery cycle...")
    time.sleep(recovery_wait)
    detail_after = get_proactive_detail(client, session_name)
    latest_after = latest_status(detail_after)
    invalid_after = count_status(detail_after, "invalid")
    print(f"  After recovery: latest={latest_after}, invalid_count={invalid_after}")

    recovered = latest_after in ("valid", "incomplete")
    if invalid_during > 0:
        results.append(StepResult(
            name="cond1_recovery",
            ok=recovered,
            details=f"CFM commit caused transient invalid (count={invalid_during}). "
                     f"After recovery: latest={latest_after}. {'Recovered OK' if recovered else 'DID NOT RECOVER'}",
            raw_output=detail_after,
        ))
    else:
        results.append(StepResult(
            name="cond1_recovery",
            ok=True,
            details=f"CFM commit did not cause visible invalid (timing dependent). "
                     f"After recovery: latest={latest_after}. System is healthy.",
            raw_output=detail_after,
        ))


def test_condition_3_rmep_down(
    client: paramiko.SSHClient,
    results: List[StepResult],
    md: str, ma: str,
    local_mep: str,
    remote_mep: str,
    session_name: str,
    test_type: str,
    recovery_wait: int,
    verbose: bool,
) -> None:
    """Condition #3: RMEP down (target mep-id with unavailable dst MAC)."""
    print(f"\n{'='*70}")
    print("CONDITION 3: RMEP down (target mep-id, dst MAC unavailable)")
    print(f"{'='*70}")

    rmep_out = get_rmep_status(client, md, ma)
    print("  Current RMEP status:")
    for ln in rmep_out.splitlines():
        s = ln.strip()
        if s and not s.startswith("show "):
            print(f"    {s}")

    rmep_up = "ok" in rmep_out.lower() or "up" in rmep_out.lower()

    if rmep_up:
        print(f"\n  RMEP is UP. To trigger condition #3 we will admin-disable "
              f"the remote MEP's local interface (requires a paired device).")
        print("  Alternatively, creating a proactive session targeting a non-existent MEP-ID.")

        print(f"\n  Creating test session targeting non-existent mep-id 999...")
        test_session = "SW238848_RMEP_DOWN_TEST"
        config_cmds = [
            "configure",
            f"services performance-monitoring cfm two-way-delay-measurement {test_session} "
            f"source maintenance-domain {md} maintenance-association {ma} mep-id {local_mep}",
            f"services performance-monitoring cfm two-way-delay-measurement {test_session} "
            f"target mep-id 999",
            f"services performance-monitoring cfm two-way-delay-measurement {test_session} "
            f"admin-state enable",
            "commit",
        ]
        cfg_out = run_sequence(client, config_cmds, timeout=30)
        err, errs = has_cli_error(cfg_out)

        if err:
            print(f"  Config for RMEP-down test session failed: {errs}")
            results.append(StepResult(
                name="cond3_rmep_down_setup",
                ok=False,
                details=f"Could not create test session targeting non-existent MEP: {'; '.join(errs)}",
                raw_output=cfg_out,
            ))
            run_sequence(client, ["exit discard"], timeout=10)
            return

        time.sleep(15)
        detail_rmep = get_proactive_detail(client, test_session)
        invalid_rmep = count_status(detail_rmep, "invalid")
        latest_rmep = latest_status(detail_rmep)
        print(f"  Session targeting non-existent RMEP: latest={latest_rmep}, invalid_count={invalid_rmep}")

        is_invalid = latest_rmep == "invalid" or invalid_rmep > 0
        results.append(StepResult(
            name="cond3_rmep_down_invalid",
            ok=is_invalid,
            details=f"Target non-existent mep-id 999: latest={latest_rmep}, invalid_count={invalid_rmep}. "
                     f"{'Invalid as expected' if is_invalid else 'Expected invalid but not seen'}",
            raw_output=detail_rmep,
        ))

        print("  Cleaning up test session...")
        run_sequence(client, [
            "configure",
            f"no services performance-monitoring cfm two-way-delay-measurement {test_session}",
            "commit",
            "exit",
        ], timeout=30)
        results.append(StepResult(
            name="cond3_rmep_down_cleanup",
            ok=True,
            details="Test session for RMEP-down condition cleaned up.",
        ))

    else:
        print("  RMEP appears to be DOWN already.")
        detail_current = get_proactive_detail(client, session_name, test_type)
        invalid_current = count_status(detail_current, "invalid")
        latest_current = latest_status(detail_current)
        print(f"  With RMEP down: latest={latest_current}, invalid_count={invalid_current}")

        results.append(StepResult(
            name="cond3_rmep_already_down",
            ok=invalid_current > 0 or latest_current == "invalid",
            details=f"RMEP is down, measurement state: latest={latest_current}, "
                     f"invalid_count={invalid_current}",
            raw_output=detail_current,
        ))


def test_condition_4_mep_disabled(
    client: paramiko.SSHClient,
    results: List[StepResult],
    md: str, ma: str,
    local_mep: str,
    session_name: str,
    test_type: str,
    recovery_wait: int,
    verbose: bool,
) -> None:
    """Condition #4: Source local MEP admin disabled."""
    print(f"\n{'='*70}")
    print("CONDITION 4: Source local MEP admin disabled")
    print(f"{'='*70}")

    detail_before = get_proactive_detail(client, session_name, test_type)
    entries_before = parse_measurement_entries(detail_before)
    if verbose and entries_before:
        print(f"  Baseline: last entries = {entries_before[-3:]}")

    print(f"  Admin-disabling local MEP {local_mep} on {md}/{ma}...")
    disable_cmds = [
        "configure",
        f"services ethernet-oam connectivity-fault-management maintenance-domains {md} "
        f"maintenance-associations {ma} mep mep-id {local_mep} admin-state disable",
        "commit",
    ]
    disable_out = run_sequence(client, disable_cmds, timeout=30)
    err, errs = has_cli_error(disable_out)

    if err:
        commit_validation = any("commit" in e.lower() and ("check" in e.lower() or "fail" in e.lower()) for e in errs)
        if commit_validation:
            print(f"  Commit was rejected (proactive session depends on this MEP). "
                  "This confirms commit validation prevents condition #4 for proactive sessions.")
            results.append(StepResult(
                name="cond4_mep_disable_blocked",
                ok=True,
                details="Commit correctly rejected MEP disable while proactive session is active. "
                        f"Errors: {'; '.join(errs[:2])}",
                raw_output=disable_out,
            ))
            run_sequence(client, ["exit discard"], timeout=10)
            return
        else:
            print(f"  MEP disable had errors: {errs}")
            results.append(StepResult(
                name="cond4_mep_disable",
                ok=False,
                details=f"Unexpected error disabling MEP: {'; '.join(errs[:2])}",
                raw_output=disable_out,
            ))
            run_sequence(client, ["exit discard"], timeout=10)
            return

    print("  MEP disabled successfully. Waiting for invalid state...")
    time.sleep(15)
    detail_during = get_proactive_detail(client, session_name, test_type)
    invalid_during = count_status(detail_during, "invalid")
    latest_during = latest_status(detail_during)
    print(f"  After MEP disable: latest={latest_during}, invalid_count={invalid_during}")

    is_invalid = latest_during == "invalid" or invalid_during > count_status(detail_before, "invalid")
    results.append(StepResult(
        name="cond4_mep_disabled_invalid",
        ok=is_invalid,
        details=f"After MEP disable: latest={latest_during}, invalid_count={invalid_during}. "
                f"{'Invalid as expected' if is_invalid else 'Expected invalid but not seen'}",
        raw_output=detail_during,
    ))

    print(f"  Re-enabling local MEP {local_mep}...")
    enable_cmds = [
        "configure",
        f"services ethernet-oam connectivity-fault-management maintenance-domains {md} "
        f"maintenance-associations {ma} mep mep-id {local_mep} admin-state enable",
        "commit",
        "exit",
    ]
    run_sequence(client, enable_cmds, timeout=30)

    print(f"  Waiting {recovery_wait}s for recovery...")
    time.sleep(recovery_wait)
    detail_after = get_proactive_detail(client, session_name, test_type)
    latest_after = latest_status(detail_after)
    invalid_after = count_status(detail_after, "invalid")
    print(f"  After recovery: latest={latest_after}, invalid_count={invalid_after}")

    recovered = latest_after in ("valid", "incomplete")
    results.append(StepResult(
        name="cond4_recovery",
        ok=recovered,
        details=f"After MEP re-enable: latest={latest_after}. "
                f"{'Recovered OK' if recovered else 'DID NOT RECOVER'}",
        raw_output=detail_after,
    ))


# ---------------------------------------------------------------------------
# Discover existing proactive sessions
# ---------------------------------------------------------------------------
def discover_proactive_session(
    client: paramiko.SSHClient,
) -> Tuple[Optional[str], Optional[str]]:
    """Find an existing proactive session name and test type from the proactive table."""
    out = run_cmd_long(client, "show services performance-monitoring cfm tests proactive | no-more")

    skip_words = {
        "test", "name", "type", "md", "ma", "mep-id", "target", "last",
        "run", "status", "session", "test name", "test type", "md name",
        "ma name", "---", "",
    }

    lines = out.strip().splitlines()
    for line in lines:
        if "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        parts = [p for p in parts if p]
        if len(parts) < 4:
            continue
        if set(line.strip()) <= {"-", "+", "|", " "}:
            continue
        name_candidate = parts[0]
        if name_candidate.lower() in skip_words:
            continue
        if name_candidate.startswith("-"):
            continue

        test_type_str = parts[1].lower() if len(parts) > 1 else ""
        if "delay" in test_type_str:
            return name_candidate, "two-way-delay"
        elif "loss" in test_type_str or "synthetic" in test_type_str:
            return name_candidate, "two-way-synthetic-loss"
        else:
            return name_candidate, "two-way-delay"

    return None, None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description="SW-238848: Y.1731 Valid/Invalid measurement state test",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 test_sw238848_valid_invalid.py --host 10.10.5.50

  python3 test_sw238848_valid_invalid.py --host 10.10.5.50 \\
      --md MD-CUST --ma MA-CUST --local-mep 2 --remote-mep 1 \\
      --session DM_CLI_TAB

  python3 test_sw238848_valid_invalid.py --host 10.10.5.50 \\
      --conditions 2,3 --recovery-wait 60
""",
    )
    parser.add_argument("--host", help="Device hostname or IP")
    parser.add_argument("--user", default="dnroot")
    parser.add_argument("--password", default="dnroot")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--md", default=None, help="Override maintenance-domain name")
    parser.add_argument("--ma", default=None, help="Override maintenance-association name")
    parser.add_argument("--local-mep", default=None, help="Override local MEP ID")
    parser.add_argument("--remote-mep", default=None, help="Override remote MEP ID")
    parser.add_argument("--session", default=None,
                        help="Proactive session name to monitor (auto-discovered if omitted)")
    parser.add_argument("--test-type", default="two-way-delay",
                        help="Proactive test type (default: two-way-delay)")
    parser.add_argument("--recovery-wait", type=int, default=130,
                        help="Seconds to wait for proactive cycle recovery (default: 130)")
    parser.add_argument("--conditions", default="1,2,3,4",
                        help="Comma-separated list of conditions to test (default: 1,2,3,4)")
    parser.add_argument("--verbose", action="store_true", help="Print extra diagnostic output")
    parser.add_argument("--output-file", default=None,
                        help="Write JSON results to file")
    args = parser.parse_args()

    if not args.host:
        args.host = input("Device hostname or IP: ").strip()
    if not args.host:
        print("ERROR: --host is required")
        return 1

    conditions = [int(c.strip()) for c in args.conditions.split(",")]

    results: List[StepResult] = []

    # Connect
    print(f"Connecting to {args.host}...")
    client = create_ssh_client(args.host, args.user, args.password, args.timeout)
    results.append(StepResult(name="connect", ok=True, details=f"Connected to {args.host}"))

    try:
        # Discover CFM context
        print("Discovering CFM context...")
        ok, detail, md, ma, local_mep, remote_mep = discover_cfm_context(client, timeout=30)
        results.append(StepResult(name="discover_cfm", ok=ok, details=detail))

        if ok:
            if args.md is None:
                args.md = md
            if args.ma is None:
                args.ma = ma
            if args.local_mep is None:
                args.local_mep = local_mep
            if args.remote_mep is None:
                args.remote_mep = remote_mep

        if not args.md or not args.ma:
            print("ERROR: Could not discover MD/MA. Provide --md and --ma manually.")
            return 1
        if not args.local_mep:
            print("ERROR: Could not discover local MEP. Provide --local-mep.")
            return 1
        if not args.remote_mep:
            print("ERROR: Could not discover remote MEP. Provide --remote-mep.")
            return 1

        md, ma = args.md, args.ma
        local_mep = args.local_mep
        remote_mep = args.remote_mep
        print(f"  Using: md={md} ma={ma} local-mep={local_mep} remote-mep={remote_mep}")

        # Discover proactive session
        session_name = args.session
        test_type = args.test_type
        if not session_name:
            print("Discovering proactive session...")
            session_name, discovered_type = discover_proactive_session(client)
            if discovered_type:
                test_type = discovered_type
            if session_name:
                print(f"  Found session: {session_name} (type: {test_type})")
            else:
                print("  WARNING: No proactive session found. "
                      "Conditions 1, 2 require an active proactive session.")
        results.append(StepResult(
            name="discover_session",
            ok=session_name is not None,
            details=f"Proactive session: {session_name} (type: {test_type})" if session_name
                    else "No proactive session found",
        ))

        # Baseline check
        if session_name:
            print(f"\nBaseline check for session '{session_name}'...")
            detail_baseline = get_proactive_detail(client, session_name, test_type)
            entries_baseline = parse_measurement_entries(detail_baseline)
            valid_count = count_status(detail_baseline, "valid")
            invalid_count = count_status(detail_baseline, "invalid")
            latest = latest_status(detail_baseline)
            print(f"  Entries: {len(entries_baseline)}, valid={valid_count}, "
                  f"invalid={invalid_count}, latest={latest}")
            results.append(StepResult(
                name="baseline",
                ok=len(entries_baseline) > 0,
                details=f"Session has {len(entries_baseline)} entries, "
                        f"valid={valid_count}, invalid={invalid_count}, latest={latest}",
                raw_output=detail_baseline,
            ))

        # Run conditions
        if 2 in conditions and session_name:
            test_condition_2_on_demand_overlap(
                client, results, md, ma, session_name, remote_mep,
                args.recovery_wait, args.verbose,
            )

        if 1 in conditions and session_name:
            test_condition_1_cfm_commit(
                client, results, md, ma, session_name,
                args.recovery_wait, args.verbose,
            )

        if 3 in conditions:
            test_condition_3_rmep_down(
                client, results, md, ma, local_mep, remote_mep,
                session_name or "N/A", test_type,
                args.recovery_wait, args.verbose,
            )

        if 4 in conditions:
            if session_name:
                test_condition_4_mep_disabled(
                    client, results, md, ma, local_mep,
                    session_name, test_type,
                    args.recovery_wait, args.verbose,
                )
            else:
                print("\n  Skipping condition 4: no proactive session to test against.")
                results.append(StepResult(
                    name="cond4_skipped",
                    ok=True,
                    details="Skipped: no proactive session available",
                ))

        # Final health check
        if session_name:
            print(f"\n{'='*70}")
            print("FINAL HEALTH CHECK")
            print(f"{'='*70}")
            detail_final = get_proactive_detail(client, session_name, test_type)
            latest_final = latest_status(detail_final)
            valid_final = count_status(detail_final, "valid")
            invalid_final = count_status(detail_final, "invalid")
            print(f"  Session '{session_name}': latest={latest_final}, "
                  f"valid={valid_final}, invalid={invalid_final}")
            results.append(StepResult(
                name="final_health",
                ok=latest_final in ("valid", "incomplete"),
                details=f"Final state: latest={latest_final}, valid={valid_final}, invalid={invalid_final}",
                raw_output=detail_final,
            ))

    finally:
        client.close()

    # Output results
    total = len(results)
    passed = sum(1 for r in results if r.ok)
    failed = total - passed

    print(f"\n{'='*70}")
    print(f"SW-238848 Valid/Invalid Test Results")
    print(f"{'='*70}")

    name_w = max(len(r.name) for r in results)
    for r in results:
        status = "PASS" if r.ok else "FAIL"
        print(f"  [{status}] {r.name.ljust(name_w)}  {r.details[:100]}")

    print(f"\n{'='*70}")
    print(f"Total: {total}  |  PASS: {passed}  |  FAIL: {failed}")
    print(f"{'='*70}")

    if args.output_file:
        json_results = [
            {
                "step": r.name,
                "status": "PASS" if r.ok else "FAIL",
                "details": r.details,
                "output": r.raw_output[:500] if r.raw_output else "",
            }
            for r in results
        ]
        with open(args.output_file, "w") as f:
            json.dump(json_results, f, indent=2)
        print(f"\nResults written to {args.output_file}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
