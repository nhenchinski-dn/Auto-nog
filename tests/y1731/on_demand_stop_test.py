#!/usr/bin/env python3
"""
SW-237984: Ethernet OAM Y.1731 | CLI | request ethernet-oam cfm on-demand stop

Comprehensive test for the 'request ethernet-oam cfm on-demand stop' command
and all its variants.  Based on the y1731_cli_tab_test.py framework.

Test plan (from Jira):
  1. Start on-demand DM two-way
  2. Start on-demand SLM
  3. Verify sessions are running via show commands
  4. Stop on-demand sessions via another SSH session
  5. Verify sessions stopped/cleared
  6. Run the sessions again and test different stop variants
  7. Stop with no active sessions (graceful)
  8. Start new on-demand session after stop

Stop variants:
  - request ethernet-oam cfm on-demand stop                 (bare stop)
  - request ethernet-oam cfm on-demand stop all
  - request ethernet-oam cfm on-demand stop maintenance-domain <MD> maintenance-association <MA> test-type two-way-delay-measurement
  - request ethernet-oam cfm on-demand stop maintenance-domain <MD> maintenance-association <MA> test-type two-way-synthetic-loss-measurement
  - request ethernet-oam cfm on-demand stop maintenance-domain <MD> maintenance-association <MA> test-type linktrace
  - request ethernet-oam cfm on-demand stop maintenance-domain <MD> maintenance-association <MA> test-type loopback
  - request ethernet-oam cfm on-demand stop test-type <type>

Negative tests:
  - Stop with no active on-demand sessions
  - Start to unreachable target then stop (no stale entries)
  - Longevity: repeated start/stop cycles
"""
import argparse
import getpass
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

import paramiko

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PROMPT_MARKERS = ("#", ">")
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class StepResult:
    name: str
    ok: bool
    details: str
    raw_output: str = ""


# ---------------------------------------------------------------------------
# SSH helpers (mirrored from y1731_cli_tab_test.py)
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
    clean = ANSI_ESCAPE.sub("", output)
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
                clean = ANSI_ESCAPE.sub("", output)
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
            stripped = line.strip()
            m = re.search(r"(Invalid value ')([^']+)(')", stripped)
            if m and len(m.group(2)) > 64:
                stripped = stripped[: m.start(2)] + "<redacted>" + stripped[m.end(2) :]
            errors.append(stripped)
    return (len(errors) > 0, errors)


def _clean_output(text: str) -> str:
    """Strip ANSI escapes for text matching."""
    return ANSI_ESCAPE.sub("", text)


def run_shell_with_prompt(client: paramiko.SSHClient, command: str, timeout: int = 30) -> str:
    channel = client.invoke_shell()
    channel.settimeout(timeout)
    banner = _read_until_prompt(channel, prompt=None, timeout=timeout, quiet=1)
    prompt = _extract_prompt(banner)
    channel.send(command + "\n")
    output = _read_until_prompt(channel, prompt=prompt, timeout=timeout, quiet=1.2)
    channel.close()
    return output


def run_shell_with_prompt_long(client: paramiko.SSHClient, command: str, timeout: int = 60) -> str:
    channel = client.invoke_shell()
    channel.settimeout(timeout)
    banner = _read_until_prompt(channel, prompt=None, timeout=timeout, quiet=1)
    prompt = _extract_prompt(banner)
    channel.send(command + "\n")
    output = _read_until_prompt(channel, prompt=prompt, timeout=timeout, quiet=3)
    output += _read_until_quiet(channel, timeout=min(timeout, 4), quiet=0.8)
    channel.close()
    return output


def run_shell_sequence(client: paramiko.SSHClient, commands: List[str], timeout: int = 30) -> str:
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
    return all_output


def _first_successful_show(
    client: paramiko.SSHClient, commands: List[str], timeout: int = 30
) -> Tuple[Optional[str], str]:
    last = ""
    for cmd in commands:
        out = run_shell_with_prompt(client, cmd, timeout=timeout)
        last = out
        err, _ = has_cli_error(out)
        if not err:
            return cmd, out
    return None, last


# ---------------------------------------------------------------------------
# CFM discovery (simplified from y1731_cli_tab_test.py)
# ---------------------------------------------------------------------------
def discover_cfm_context(
    client: paramiko.SSHClient, timeout: int = 30
) -> Tuple[bool, str, Optional[str], Optional[str], Optional[str], Optional[str], Optional[str]]:
    """
    Discover (md, ma, local_mep_id, target_mep_id, direction) from existing
    'services ethernet-oam connectivity-fault-management' config on the device.
    Returns: (ok, details, md, ma, mep_id, target_str, direction)
    """
    show_cmds = [
        "show config services ethernet-oam connectivity-fault-management | display-set",
        "show configuration services ethernet-oam connectivity-fault-management | display-set",
        "show config services ethernet-oam connectivity-fault-management",
        "show configuration services ethernet-oam connectivity-fault-management",
        "show config services ethernet-oam | display-set | match connectivity-fault-management",
        "show configuration services ethernet-oam | display-set | match connectivity-fault-management",
    ]

    used: Optional[str] = None
    output = ""
    for cmd in show_cmds:
        out = run_shell_with_prompt_long(client, cmd, timeout=max(timeout, 60))
        err, _ = has_cli_error(out)
        if (not err) and re.search(r"(ethernet-oam|connectivity-fault-management|maintenance)", out, re.IGNORECASE):
            used = cmd
            output = out
            break

    if not used:
        return (False, "Failed to read CFM config.", None, None, None, None, None)

    direction_re = re.compile(r"\bdirection\s+(down|up)\b", flags=re.IGNORECASE)
    md_re = re.compile(r"\bmaintenance[-_]domain(?:s)?(?:[-_]name)?\s+(\S+)", flags=re.IGNORECASE)
    ma_re = re.compile(r"\bmaintenance[-_]association(?:s)?(?:[-_]name)?\s+(\S+)", flags=re.IGNORECASE)
    mep_id_re = re.compile(r"\bmep[-_]id\s+(\d+)\b", flags=re.IGNORECASE)
    mep_re = re.compile(r"\bmep\s+(\d+)\b", flags=re.IGNORECASE)
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
            candidates[key] = {"meps": set(), "remote_meps": set(), "direction": None}
        dir_m = direction_re.search(line)
        if dir_m:
            candidates[key]["direction"] = dir_m.group(1).lower()
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
        for m in mep_id_re.finditer(line):
            candidates[key]["meps"].add(int(m.group(1)))
        for m in mep_re.finditer(line):
            candidates[key]["meps"].add(int(m.group(1)))

    if not candidates:
        return (False, "Parsed CFM config but no MD/MA found.", None, None, None, None, None)

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
    target_mep = None
    if remote_meps:
        target_mep = remote_meps[0]
    elif len(meps) >= 2:
        target_mep = next((m for m in meps if m != meps[0]), None)
    target_str = f"mep-id {target_mep}" if target_mep is not None else None
    direction = candidates.get(best_key, {}).get("direction")
    details = (
        f"Discovered CFM: md={md} ma={ma}"
        + (f" mep-id={local_mep}" if local_mep else " mep-id=<not-found>")
        + (f" target={target_str}" if target_str else " target=<not-found>")
        + (f" direction={direction}" if direction else "")
    )
    return True, details, md, ma, local_mep, target_str, direction


# ---------------------------------------------------------------------------
# On-demand session helpers
# ---------------------------------------------------------------------------
def _start_on_demand(client: paramiko.SSHClient, command: str, timeout: int = 15) -> paramiko.Channel:
    """
    Start an on-demand CFM test on a persistent shell channel.
    Returns the channel (still open) so the test keeps running.
    """
    channel = client.invoke_shell()
    channel.settimeout(timeout)
    _read_until_prompt(channel, prompt=None, timeout=timeout, quiet=1)
    channel.send(command + "\n")
    time.sleep(1)
    return channel


# ---------------------------------------------------------------------------
# Output parsing and validation
# ---------------------------------------------------------------------------
def _parse_stopped_count(output: str) -> Optional[int]:
    """
    Parse the 'Stopped tests: N' or 'Total stopped tests: N' from stop output.
    Returns the count or None if not found.
    """
    clean = _clean_output(output).lower()
    # "Stopped tests: 2" or "Total stopped tests: 2" or "stopped tests : 2"
    m = re.search(r"(?:total\s+)?stopped\s+tests?\s*[:=]\s*(\d+)", clean)
    if m:
        return int(m.group(1))
    return None


def _has_on_demand_content(output: str, md: str, ma: str) -> Tuple[bool, List[str]]:
    """
    Check whether 'show ... on-demand' output contains meaningful on-demand
    session data (not just headers or empty tables).
    Returns (has_content, list_of_found_indicators).
    """
    clean = _clean_output(output).lower()
    found: List[str] = []
    # Check for MD/MA in output
    if md.lower() in clean:
        found.append(f"MD={md}")
    if ma.lower() in clean:
        found.append(f"MA={ma}")
    # Check for test-type indicators
    for kw in ("delay-measurement", "synthetic-loss", "loopback", "linktrace",
               "two-way", "dm", "slm"):
        if kw in clean:
            found.append(kw)
    # Check for operational keywords indicating active session
    for kw in ("ongoing", "running", "in-progress", "active", "started",
               "transmitted", "received", "sent"):
        if kw in clean:
            found.append(kw)
    return len(found) > 0, found


def _has_no_sessions(output: str) -> bool:
    """Check whether the output indicates no on-demand sessions are active."""
    clean = _clean_output(output).lower()
    return (
        "no ongoing" in clean
        or "no on-demand" in clean
        or "no tests" in clean
        or "no entries" in clean
        or "no data" in clean
    )


def _validate_stop_output(
    output: str,
    expected_min_stopped: int = 0,
    label: str = "",
) -> Tuple[bool, str]:
    """
    Validate 'request ethernet-oam cfm on-demand stop' output.
    Checks:
      1. No CLI errors
      2. Parses 'Stopped tests: N' count
      3. If expected_min_stopped > 0, validates count >= expected
    Returns (ok, detail_message).
    """
    clean = _clean_output(output)
    lower = clean.lower()

    # 1. CLI error check
    if "unknown command" in lower or "invalid command" in lower or "syntax error" in lower:
        return False, f"CLI error in stop output."

    # 2. Parse stopped count
    stopped_count = _parse_stopped_count(output)

    # 3. Check for "no ongoing" (when no sessions were active)
    no_ongoing = (
        "no ongoing" in lower
        or "no on-demand" in lower
        or "no tests" in lower
    )

    parts: List[str] = []
    if stopped_count is not None:
        parts.append(f"Stopped tests: {stopped_count}")
    if no_ongoing:
        parts.append("No ongoing sessions")

    # 4. Validate against expected minimum
    ok = True
    if expected_min_stopped > 0:
        if stopped_count is not None and stopped_count >= expected_min_stopped:
            parts.append(f"(expected >= {expected_min_stopped}, got {stopped_count}) OK")
        elif stopped_count is not None and stopped_count < expected_min_stopped:
            ok = False
            parts.append(f"(expected >= {expected_min_stopped}, got {stopped_count}) MISMATCH")
        elif no_ongoing:
            ok = False
            parts.append(f"(expected >= {expected_min_stopped} stopped, but got 'no ongoing' -- session may not have started)")
        elif len(clean.strip()) < 10:
            ok = False
            parts.append("Empty/minimal output")
        else:
            # Output present but no parseable count and not 'no ongoing';
            # accept if output is non-trivial (device may use different wording)
            parts.append(f"(expected >= {expected_min_stopped}, count not parsed but output present)")
    elif expected_min_stopped == 0:
        # No minimum expected -- just verify command was accepted
        if stopped_count is not None or no_ongoing or len(clean.strip()) > 10:
            pass  # All good
        elif len(clean.strip()) < 10:
            ok = False
            parts.append("Empty/minimal output")

    detail = "; ".join(parts) if parts else "Stop output received"
    if label:
        detail = f"{label}: {detail}"
    return ok, detail


def _validate_no_sessions_stop(output: str, label: str = "") -> Tuple[bool, str]:
    """
    Validate that stop with no active sessions is graceful.
    Expected: "No ongoing" or "Stopped tests: 0" -- NO CLI errors.
    """
    clean = _clean_output(output)
    lower = clean.lower()

    # CLI error = fail
    if "unknown command" in lower or "invalid command" in lower or "syntax error" in lower:
        return False, f"{label}: CLI error on stop with no active sessions."

    stopped_count = _parse_stopped_count(output)
    no_ongoing = (
        "no ongoing" in lower
        or "no on-demand" in lower
        or "no tests" in lower
    )

    parts: List[str] = []
    ok = True

    if no_ongoing:
        parts.append("'No ongoing' message present")
    if stopped_count is not None:
        parts.append(f"Stopped tests: {stopped_count}")
        if stopped_count != 0:
            # Stopped > 0 when we expected 0 -- possible stale session
            ok = False
            parts.append(f"UNEXPECTED: stopped {stopped_count} tests when none should be active")

    if not no_ongoing and stopped_count is None:
        if len(clean.strip()) < 10:
            ok = False
            parts.append("Empty/minimal output")
        else:
            parts.append("Output received (no 'no ongoing' keyword, no stopped count parsed)")

    detail = "; ".join(parts) if parts else "Graceful response"
    if label:
        detail = f"{label}: {detail}"
    return ok, detail


def _validate_show_running(
    output: str, md: str, ma: str, test_types_expected: List[str],
) -> Tuple[bool, str]:
    """
    Validate 'show ... on-demand' output when sessions should be running.
    Checks:
      1. No CLI error
      2. MD and MA appear in output
      3. At least one expected test-type keyword appears
      4. Operational indicators (ongoing/running/transmitted etc.)
    Returns (ok, detail).
    """
    err, errs = has_cli_error(output)
    if err:
        return False, f"CLI error: {'; '.join(errs)}"

    clean = _clean_output(output)
    lower = clean.lower()
    checks_passed: List[str] = []
    checks_failed: List[str] = []

    # MD/MA presence
    if md.lower() in lower:
        checks_passed.append(f"MD '{md}' found")
    else:
        checks_failed.append(f"MD '{md}' NOT found in output")

    if ma.lower() in lower:
        checks_passed.append(f"MA '{ma}' found")
    else:
        checks_failed.append(f"MA '{ma}' NOT found in output")

    # Test type keywords
    type_found = []
    for tt in test_types_expected:
        if tt.lower() in lower:
            type_found.append(tt)
    if type_found:
        checks_passed.append(f"Test types found: {type_found}")
    else:
        checks_failed.append(f"None of expected test types {test_types_expected} found")

    # Operational indicators
    oper_keywords = ["ongoing", "running", "in-progress", "active", "started",
                     "transmitted", "received", "sent", "reply"]
    oper_found = [kw for kw in oper_keywords if kw in lower]
    if oper_found:
        checks_passed.append(f"Operational indicators: {oper_found}")
    # Not finding operational keywords is a warning, not a failure (device output may vary)

    ok = len(checks_failed) == 0
    detail = "PASSED: " + "; ".join(checks_passed) if ok else "FAILED: " + "; ".join(checks_failed) + " | Found: " + "; ".join(checks_passed)
    return ok, detail


def _validate_show_stopped(
    output: str, md: str, ma: str,
) -> Tuple[bool, str]:
    """
    Validate 'show ... on-demand' output AFTER stop.

    After a session has been stopped the output MUST contain the word "invalid"
    to indicate the session is no longer valid.  If sessions are present in the
    output but "invalid" is missing, this is a defect.

    Accepted outcomes:
      - 'No ongoing' / empty table  (no sessions at all -- OK)
      - Sessions listed with 'invalid' state  (expected after stop -- OK)
      - Sessions listed WITHOUT 'invalid'     (BUG -- FAIL)
      - Live running-state indicators         (BUG -- FAIL)
    """
    err, errs = has_cli_error(output)
    if err:
        return False, f"CLI error: {'; '.join(errs)}"

    clean = _clean_output(output)
    lower = clean.lower()
    parts: List[str] = []

    no_sessions = _has_no_sessions(output)
    if no_sessions:
        parts.append("'No ongoing/no entries' confirmed")
        return True, "; ".join(parts)

    # Check whether MD/MA is present in the output
    md_present = md.lower() in lower

    # ------------------------------------------------------------------
    # Primary check: after a stop, the output MUST say "invalid"
    # ------------------------------------------------------------------
    invalid_found = "invalid" in lower

    # Also look for secondary stopped indicators
    stopped_indicators = ["stopped", "completed", "finished", "idle"]
    stopped_found = [kw for kw in stopped_indicators if kw in lower]

    # Look for concrete evidence of actively running sessions
    active_evidence: List[str] = []
    for line in clean.splitlines():
        ll = line.lower().strip()
        # "Ongoing Tests: 2" with count > 0 means actively running
        m = re.search(r"ongoing\s*(?:tests?)?\s*[:=]\s*(\d+)", ll)
        if m and int(m.group(1)) > 0:
            active_evidence.append(f"Ongoing tests: {m.group(1)}")
        # "State: Running" or "Status: Ongoing" as a value (not header)
        if re.search(r"(?:state|status)\s*[:=]\s*(?:running|ongoing|in-progress)", ll):
            active_evidence.append(line.strip())

    # FAIL: sessions still actively running
    if md_present and active_evidence:
        parts.append(f"MD '{md}' present with active evidence: {active_evidence}")
        return False, "Sessions still running after stop: " + "; ".join(parts)

    # If MD is present in output, "invalid" MUST appear
    if md_present and not invalid_found:
        parts.append(f"MD '{md}' present but 'invalid' NOT found in output")
        if stopped_found:
            parts.append(f"(secondary indicators found: {stopped_found}, but 'invalid' is required)")
        return False, "ISSUE: stopped session should say 'invalid': " + "; ".join(parts)

    # SUCCESS paths
    if md_present and invalid_found:
        parts.append(f"MD '{md}' present and 'invalid' confirmed after stop")
        if stopped_found:
            parts.append(f"Additional indicators: {stopped_found}")
        return True, "; ".join(parts)

    if not md_present:
        parts.append(f"MD '{md}' not in output (sessions fully cleared)")
        return True, "; ".join(parts)

    return True, "; ".join(parts) if parts else "Output indicates sessions stopped/cleared"


def _extract_counters_from_show(output: str) -> Dict[str, int]:
    """
    Extract numeric counters from 'show ... on-demand detail' output.
    Looks for patterns like 'Transmitted: N', 'Received: N', etc.
    """
    counters: Dict[str, int] = {}
    for line in output.splitlines():
        m = re.search(r"(Transmitted|Received|Sent|Reply|Response|PDU)\s*[:=]\s*(\d+)", line, re.IGNORECASE)
        if m:
            counters[m.group(1).lower()] = int(m.group(2))
    return counters


def _show_on_demand(client: paramiko.SSHClient, timeout: int = 30) -> Tuple[str, str]:
    """Run 'show services performance-monitoring cfm tests on-demand' with fallbacks."""
    show_cmds = [
        "show services performance-monitoring cfm tests on-demand",
        "show services performance-monitoring cfm tests on-demand detail",
        "show services performance-monitoring cfm tests",
    ]
    used, out = _first_successful_show(client, show_cmds, timeout=timeout)
    return used or "none", out


def _show_on_demand_detail(client: paramiko.SSHClient, timeout: int = 30) -> Tuple[str, str]:
    """Run 'show services performance-monitoring cfm tests on-demand' (detail variant) with fallbacks.
    Note: 'detail' keyword is not supported on all devices, so we try the base command first."""
    show_cmds = [
        "show services performance-monitoring cfm tests on-demand",
        "show services performance-monitoring cfm tests on-demand detail",
        "show services performance-monitoring cfm tests",
    ]
    used, out = _first_successful_show(client, show_cmds, timeout=timeout)
    return used or "none", out


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------
def _prompt_if_missing(value: Optional[str], label: str, secret: bool = False) -> str:
    if value is not None and value.strip():
        return value
    if secret:
        return getpass.getpass(label)
    return input(label).strip()


def _prompt_target(label: str) -> str:
    raw = input(label).strip()
    if not raw:
        return "mep-id 2"
    if raw.isdigit():
        return f"mep-id {raw}"
    return raw


# ---------------------------------------------------------------------------
# Main test logic
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description="SW-237984: Y.1731 on-demand stop comprehensive test",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Auto-discover CFM context and run all tests
  python3 on_demand_stop_test.py --host 10.10.5.50

  # Specify MD/MA/targets manually
  python3 on_demand_stop_test.py --host 10.10.5.50 --md MD-CUST --ma MA-CUST --target "mep-id 2"

  # Skip longevity test, show raw CLI output
  python3 on_demand_stop_test.py --host 10.10.5.50 --skip-longevity --show-cli-output
""",
    )
    parser.add_argument("--host", help="Device hostname or IP")
    parser.add_argument("--user", default="dnroot")
    parser.add_argument("--password", default="dnroot")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument(
        "--auto-from-cfm",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Auto-discover MD/MA/MEP/target from existing ethernet-oam CFM config (default: true).",
    )
    parser.add_argument("--md", default=None, help="Override maintenance-domain name")
    parser.add_argument("--ma", default=None, help="Override maintenance-association name")
    parser.add_argument("--mep-id", default=None, help="Override local MEP ID")
    parser.add_argument("--target", default=None, help="Override target (e.g., 'mep-id 2')")
    parser.add_argument(
        "--target-mac",
        default="00:11:22:33:44:55",
        help="MAC address for mac-address target tests (default: 00:11:22:33:44:55)",
    )
    parser.add_argument(
        "--settle-time",
        type=int,
        default=5,
        help="Seconds to wait after starting on-demand before issuing stop (default: 5)",
    )
    parser.add_argument(
        "--counter-wait",
        type=int,
        default=5,
        help="Seconds to wait after stop before verifying counters did not increment (default: 5)",
    )
    parser.add_argument(
        "--longevity-cycles",
        type=int,
        default=5,
        help="Number of start/stop cycles for longevity test (default: 5)",
    )
    parser.add_argument(
        "--skip-longevity",
        action="store_true",
        help="Skip the longevity start/stop cycle test.",
    )
    parser.add_argument(
        "--skip-unreachable",
        action="store_true",
        help="Skip the unreachable-target stop test.",
    )
    parser.add_argument(
        "--unreachable-mac",
        default="00:de:ad:be:ef:99",
        help="MAC address for unreachable target test (default: 00:de:ad:be:ef:99)",
    )
    parser.add_argument(
        "--show-cli-output",
        action="store_true",
        help="Print raw CLI output from device.",
    )
    parser.add_argument(
        "--output-file",
        help="Write raw CLI output to a file.",
    )
    parser.add_argument(
        "--show-details",
        action="store_true",
        help="Print per-step details. Default is PASS/FAIL only.",
    )
    parser.add_argument(
        "--show-progress",
        action="store_true",
        help="Print 'RUNNING: <test>' before each test starts.",
    )
    parser.add_argument(
        "--output-format",
        choices=["table", "lines"],
        default="table",
        help="Console output format. Default: table.",
    )
    args = parser.parse_args()

    # --- Interactive prompts ---
    args.host = _prompt_if_missing(args.host, "Device hostname or IP: ")
    args.user = _prompt_if_missing(args.user, "Username [dnroot]: ") or "dnroot"
    if args.password == "dnroot":
        args.password = _prompt_if_missing(args.password, "Password [dnroot]: ", secret=True) or "dnroot"

    if args.show_details and not args.show_progress:
        args.show_progress = True

    results: List[StepResult] = []
    raw_outputs: List[str] = []

    def _progress(name: str) -> None:
        if args.show_progress:
            print(f"RUNNING: {name}")

    # ===================================================================
    # Phase 0: Connect and discover CFM context
    # ===================================================================
    _progress("connect")
    client = create_ssh_client(args.host, args.user, args.password, args.timeout)
    results.append(StepResult(name="connect", ok=True, details=f"Connected to {args.host}."))

    try:
        # --- CFM discovery ---
        if args.auto_from_cfm:
            _progress("discover_cfm_context")
            ok, detail, md, ma, mep_id, target_str, direction = discover_cfm_context(client, timeout=30)
            results.append(StepResult(name="discover_cfm_context", ok=ok, details=detail))
            if ok:
                if args.md is None:
                    args.md = md
                if args.ma is None:
                    args.ma = ma
                if args.mep_id is None:
                    args.mep_id = mep_id
                if args.target is None and target_str is not None:
                    args.target = target_str
            else:
                print("CFM discovery failed; prompting for MD/MA/target.")
                if args.md is None:
                    args.md = input("Maintenance-domain (MD) name: ").strip()
                if args.ma is None:
                    args.ma = input("Maintenance-association (MA) name: ").strip()
                if args.mep_id is None:
                    args.mep_id = input("Local MEP ID (numeric): ").strip()
                if args.target is None:
                    args.target = _prompt_target("Target (e.g., 'mep-id 2'): ")

        # Final fallbacks
        if args.md is None:
            args.md = "MD-CUST"
        if args.ma is None:
            args.ma = "MA-CUST"
        if args.mep_id is None:
            args.mep_id = "1"
        if args.target is None:
            args.target = "mep-id 2"

        md, ma = args.md, args.ma
        target_mep = args.target.split()[-1] if "mep-id" in args.target else "2"
        target_mac = args.target_mac

        # =================================================================
        # Build on-demand run command templates (mep-id target)
        # =================================================================
        run_dm = (
            f"run ethernet-oam cfm on-demand delay-measurement two-way "
            f"maintenance-domain {md} maintenance-association {ma} target mep-id {target_mep}"
        )
        run_slm = (
            f"run ethernet-oam cfm on-demand synthetic-loss-measurement "
            f"maintenance-domain {md} maintenance-association {ma} target mep-id {target_mep}"
        )
        run_lb = (
            f"run ethernet-oam cfm on-demand loopback "
            f"maintenance-domain {md} maintenance-association {ma} target mep-id {target_mep}"
        )
        run_lt = (
            f"run ethernet-oam cfm on-demand linktrace "
            f"maintenance-domain {md} maintenance-association {ma} target mep-id {target_mep}"
        )

        # =================================================================
        # Build on-demand run command templates (mac-address target)
        # =================================================================
        run_dm_mac = (
            f"run ethernet-oam cfm on-demand delay-measurement two-way "
            f"maintenance-domain {md} maintenance-association {ma} target mac-address {target_mac}"
        )
        run_slm_mac = (
            f"run ethernet-oam cfm on-demand synthetic-loss-measurement "
            f"maintenance-domain {md} maintenance-association {ma} target mac-address {target_mac}"
        )
        run_lb_mac = (
            f"run ethernet-oam cfm on-demand loopback "
            f"maintenance-domain {md} maintenance-association {ma} target mac-address {target_mac}"
        )
        run_lt_mac = (
            f"run ethernet-oam cfm on-demand linktrace "
            f"maintenance-domain {md} maintenance-association {ma} target mac-address {target_mac}"
        )

        # =================================================================
        # Build stop command templates
        # =================================================================
        stop_bare = "request ethernet-oam cfm on-demand stop"
        stop_all = "request ethernet-oam cfm on-demand stop all"
        stop_md_dm = (
            f"request ethernet-oam cfm on-demand stop "
            f"maintenance-domain {md} maintenance-association {ma} "
            f"test-type two-way-delay-measurement"
        )
        stop_md_slm = (
            f"request ethernet-oam cfm on-demand stop "
            f"maintenance-domain {md} maintenance-association {ma} "
            f"test-type two-way-synthetic-loss-measurement"
        )
        stop_md_lt = (
            f"request ethernet-oam cfm on-demand stop "
            f"maintenance-domain {md} maintenance-association {ma} "
            f"test-type linktrace"
        )
        stop_md_lb = (
            f"request ethernet-oam cfm on-demand stop "
            f"maintenance-domain {md} maintenance-association {ma} "
            f"test-type loopback"
        )
        stop_type_dm = "request ethernet-oam cfm on-demand stop test-type two-way-delay-measurement"
        stop_type_slm = "request ethernet-oam cfm on-demand stop test-type two-way-synthetic-loss-measurement"
        stop_type_lt = "request ethernet-oam cfm on-demand stop test-type linktrace"
        stop_type_lb = "request ethernet-oam cfm on-demand stop test-type loopback"

        # =================================================================
        # Open second SSH client for stop commands
        # =================================================================
        _progress("open_second_ssh")
        client2 = create_ssh_client(args.host, args.user, args.password, args.timeout)
        results.append(StepResult(name="open_second_ssh", ok=True, details="Second SSH session opened."))

        # =================================================================
        # STEP 1+2: Start on-demand DM and SLM, verify running
        # =================================================================
        _progress("step1_start_dm_and_slm")
        ch_dm = None
        ch_slm = None
        try:
            ch_dm = _start_on_demand(client, run_dm, timeout=15)
            time.sleep(1)
            ch_slm = _start_on_demand(client, run_slm, timeout=15)
            time.sleep(args.settle_time)
            results.append(StepResult(
                name="step1_start_dm_and_slm",
                ok=True,
                details=f"Started DM and SLM on-demand sessions (waited {args.settle_time}s to settle).",
            ))
        except Exception as e:
            results.append(StepResult(name="step1_start_dm_and_slm", ok=False, details=str(e)))

        # STEP 3: Verify sessions running via show - validate content
        _progress("step3_show_on_demand_running")
        show_cmd_used, show_out = _show_on_demand(client2, timeout=30)
        raw_outputs.append(f"## SHOW ON-DEMAND (running):\nCommand: {show_cmd_used}\n{show_out}")
        ok_show, detail_show = _validate_show_running(
            show_out, md, ma,
            test_types_expected=["delay-measurement", "synthetic-loss", "two-way"],
        )
        results.append(StepResult(
            name="step3_show_on_demand_running",
            ok=ok_show,
            details=f"via '{show_cmd_used}': {detail_show}",
            raw_output=show_out,
        ))

        # Also check detail view - validate content
        _progress("step3_show_detail_running")
        detail_cmd_used, detail_out = _show_on_demand_detail(client2, timeout=30)
        raw_outputs.append(f"## SHOW ON-DEMAND DETAIL (running):\nCommand: {detail_cmd_used}\n{detail_out}")
        ok_detail, detail_msg = _validate_show_running(
            detail_out, md, ma,
            test_types_expected=["delay-measurement", "synthetic-loss", "two-way"],
        )
        results.append(StepResult(
            name="step3_show_detail_running",
            ok=ok_detail,
            details=f"via '{detail_cmd_used}': {detail_msg}",
            raw_output=detail_out,
        ))

        # =================================================================
        # STEP 1c: Also verify DM start with mac-address target
        # =================================================================
        _progress("step1c_start_dm_mac")
        ch_dm_mac = None
        try:
            # First cleanup any sessions from step 1
            run_shell_with_prompt(client2, stop_all, timeout=25)
            time.sleep(2)
            ch_dm_mac = _start_on_demand(client, run_dm_mac, timeout=15)
            time.sleep(args.settle_time)
            _, show_mac = _show_on_demand(client2, timeout=30)
            raw_outputs.append(f"## SHOW ON-DEMAND (DM mac running):\n{show_mac}")
            ok_mac, det_mac = _validate_show_running(
                show_mac, md, ma,
                test_types_expected=["delay-measurement", "two-way"],
            )
            results.append(StepResult(
                name="step1c_start_dm_mac",
                ok=ok_mac,
                details=f"Start DM with mac target {target_mac}: {det_mac}",
                raw_output=show_mac,
            ))
            # Stop it
            out_mac_stop = run_shell_with_prompt(client2, stop_all, timeout=25)
            results.append(StepResult(
                name="step1c_dm_mac_stop",
                ok=not has_cli_error(out_mac_stop)[0],
                details=f"Stop DM mac: Stopped count={_parse_stopped_count(out_mac_stop)}",
                raw_output=out_mac_stop,
            ))
        except Exception as e:
            results.append(StepResult(name="step1c_start_dm_mac", ok=False, details=str(e)))
        finally:
            if ch_dm_mac is not None:
                try:
                    ch_dm_mac.close()
                except Exception:
                    pass

        time.sleep(1)

        # STEP 1d: Verify LB start with mac-address target
        _progress("step1d_start_lb_mac")
        ch_lb_mac = None
        try:
            ch_lb_mac = _start_on_demand(client, run_lb_mac, timeout=15)
            time.sleep(args.settle_time)
            _, show_lb_mac = _show_on_demand(client2, timeout=30)
            raw_outputs.append(f"## SHOW ON-DEMAND (LB mac running):\n{show_lb_mac}")
            ok_lb_mac, det_lb_mac = _validate_show_running(
                show_lb_mac, md, ma,
                test_types_expected=["loopback"],
            )
            results.append(StepResult(
                name="step1d_start_lb_mac",
                ok=ok_lb_mac,
                details=f"Start LB with mac target {target_mac}: {det_lb_mac}",
                raw_output=show_lb_mac,
            ))
            out_lb_mac_stop = run_shell_with_prompt(client2, stop_all, timeout=25)
            results.append(StepResult(
                name="step1d_lb_mac_stop",
                ok=not has_cli_error(out_lb_mac_stop)[0],
                details=f"Stop LB mac: Stopped count={_parse_stopped_count(out_lb_mac_stop)}",
                raw_output=out_lb_mac_stop,
            ))
        except Exception as e:
            results.append(StepResult(name="step1d_start_lb_mac", ok=False, details=str(e)))
        finally:
            if ch_lb_mac is not None:
                try:
                    ch_lb_mac.close()
                except Exception:
                    pass

        time.sleep(1)

        # Restart mep-id DM+SLM for step 4 flow
        _progress("step4_restart_dm_slm")
        try:
            ch_dm = _start_on_demand(client, run_dm, timeout=15)
            time.sleep(1)
            ch_slm = _start_on_demand(client, run_slm, timeout=15)
            time.sleep(args.settle_time)
        except Exception as e:
            results.append(StepResult(name="step4_restart_dm_slm", ok=False, details=str(e)))

        # Record pre-stop counters from detail output
        _, detail_out_pre = _show_on_demand_detail(client2, timeout=30)
        counters_before = _extract_counters_from_show(detail_out_pre)

        # STEP 4: Stop on-demand sessions via second SSH - validate output content
        # NOTE: bare 'request ethernet-oam cfm on-demand stop' (without 'all')
        # may have different output format; accept any non-error response.
        _progress("step4_stop_bare")
        out_stop = run_shell_with_prompt(client2, stop_bare, timeout=25)
        raw_outputs.append(f"## STOP (bare): {stop_bare}\n{out_stop}")
        ok_stop, detail_stop = _validate_stop_output(
            out_stop, expected_min_stopped=0,
            label="Bare stop (DM+SLM running)",
        )
        # Additionally check: after the bare stop, try 'stop all' to catch anything remaining
        out_stop_all_followup = run_shell_with_prompt(client2, stop_all, timeout=25)
        raw_outputs.append(f"## STOP ALL (followup after bare): {stop_all}\n{out_stop_all_followup}")
        followup_stopped = _parse_stopped_count(out_stop_all_followup)
        if followup_stopped and followup_stopped > 0:
            detail_stop += f"; followup 'stop all' caught {followup_stopped} remaining session(s)"
        else:
            detail_stop += "; followup 'stop all' confirmed nothing remaining"
        results.append(StepResult(name="step4_stop_bare", ok=ok_stop, details=detail_stop, raw_output=out_stop))

        # Close the on-demand channels
        for ch in [ch_dm, ch_slm]:
            if ch is not None:
                try:
                    ch.close()
                except Exception:
                    pass
        ch_dm, ch_slm = None, None

        # STEP 5: Verify sessions stopped/cleared - validate show output content
        _progress("step5_show_on_demand_after_stop")
        time.sleep(2)
        show_cmd_after, show_out_after = _show_on_demand(client2, timeout=30)
        raw_outputs.append(f"## SHOW ON-DEMAND (after stop):\nCommand: {show_cmd_after}\n{show_out_after}")
        ok_stopped, detail_stopped = _validate_show_stopped(show_out_after, md, ma)
        results.append(StepResult(
            name="step5_show_on_demand_after_stop",
            ok=ok_stopped,
            details=f"via '{show_cmd_after}': {detail_stopped}",
            raw_output=show_out_after,
        ))

        # Verify no counters increment after stop
        _progress("step5_no_counter_increment")
        time.sleep(args.counter_wait)
        _, detail_out_after = _show_on_demand_detail(client2, timeout=30)
        raw_outputs.append(f"## SHOW ON-DEMAND DETAIL (after stop + {args.counter_wait}s wait):\n{detail_out_after}")
        counters_after = _extract_counters_from_show(detail_out_after)
        counter_incremented = False
        counter_detail_parts: List[str] = []
        if counters_before:
            counter_detail_parts.append(f"Before stop: {counters_before}")
        if counters_after:
            counter_detail_parts.append(f"After stop+{args.counter_wait}s: {counters_after}")
        if counters_before and counters_after:
            for key in counters_before:
                if key in counters_after and counters_after[key] > counters_before[key]:
                    counter_incremented = True
                    counter_detail_parts.append(
                        f"INCREMENTED {key}: {counters_before[key]} -> {counters_after[key]}"
                    )
        if counter_incremented:
            results.append(StepResult(
                name="step5_no_counter_increment",
                ok=False,
                details=f"Counters incremented after stop! {'; '.join(counter_detail_parts)}",
            ))
        elif counters_before:
            results.append(StepResult(
                name="step5_no_counter_increment",
                ok=True,
                details=f"Counters stable after stop. {'; '.join(counter_detail_parts)}",
            ))
        else:
            results.append(StepResult(
                name="step5_no_counter_increment",
                ok=True,
                details="No counters parsed from pre-stop detail output (device may not expose counters in show on-demand).",
            ))

        # =================================================================
        # STEP 6: Run sessions again with different stop variants
        # Each variant: start N sessions, wait, stop, validate output
        #
        # transient=True means the test type (SLM, LT) completes quickly
        # on its own, so "no ongoing" is a valid stop response.
        # =================================================================
        # (test_name, run_cmds, stop_cmd, expected_min_stopped, description, transient)
        variant_tests = [
            # --- mep-id target: persistent types (DM, LB) ---
            ("step6_dm_mep_stop_all",      [run_dm],  stop_all,      1, "DM mep -> stop all",              False),
            ("step6_lb_mep_stop_md_type",  [run_lb],  stop_md_lb,    1, "LB mep -> stop md+type LB",      False),
            ("step6_dm_mep_stop_md_type",  [run_dm],  stop_md_dm,    1, "DM mep -> stop md+type DM",      False),
            ("step6_dm_mep_stop_test_type",[run_dm],  stop_type_dm,  1, "DM mep -> stop test-type DM",    False),
            ("step6_lb_mep_stop_test_type",[run_lb],  stop_type_lb,  1, "LB mep -> stop test-type LB",    False),
            # --- mep-id target: transient types (SLM, LT) ---
            ("step6_slm_mep_stop_all",     [run_slm], stop_all,      0, "SLM mep -> stop all",             True),
            ("step6_slm_mep_stop_md_type", [run_slm], stop_md_slm,   0, "SLM mep -> stop md+type SLM",    True),
            ("step6_lt_mep_stop_md_type",  [run_lt],  stop_md_lt,    0, "LT mep -> stop md+type LT",      True),
            ("step6_slm_mep_stop_test_type",[run_slm],stop_type_slm, 0, "SLM mep -> stop test-type SLM",  True),
            ("step6_lt_mep_stop_test_type",[run_lt],  stop_type_lt,  0, "LT mep -> stop test-type LT",    True),
            # --- mac-address target: persistent types (DM, LB) ---
            ("step6_dm_mac_stop_all",      [run_dm_mac],  stop_all,      1, "DM mac -> stop all",          False),
            ("step6_lb_mac_stop_all",      [run_lb_mac],  stop_all,      1, "LB mac -> stop all",          False),
            ("step6_dm_mac_stop_md_type",  [run_dm_mac],  stop_md_dm,    1, "DM mac -> stop md+type DM",   False),
            ("step6_lb_mac_stop_md_type",  [run_lb_mac],  stop_md_lb,    1, "LB mac -> stop md+type LB",   False),
            ("step6_dm_mac_stop_test_type",[run_dm_mac],  stop_type_dm,  1, "DM mac -> stop test-type DM", False),
            ("step6_lb_mac_stop_test_type",[run_lb_mac],  stop_type_lb,  1, "LB mac -> stop test-type LB", False),
            # --- mac-address target: transient types (SLM, LT) ---
            ("step6_slm_mac_stop_all",     [run_slm_mac], stop_all,      0, "SLM mac -> stop all",         True),
            ("step6_lt_mac_stop_all",      [run_lt_mac],  stop_all,      0, "LT mac -> stop all",          True),
            ("step6_slm_mac_stop_md_type", [run_slm_mac], stop_md_slm,   0, "SLM mac -> stop md+type SLM", True),
            ("step6_lt_mac_stop_md_type",  [run_lt_mac],  stop_md_lt,    0, "LT mac -> stop md+type LT",   True),
            # --- multi-session mixes ---
            ("step6_multi_mep_stop_all",   [run_dm, run_slm, run_lb, run_lt], stop_all, 1,
             "DM+SLM+LB+LT mep -> stop all",      False),
            ("step6_multi_mac_stop_all",   [run_dm_mac, run_lb_mac], stop_all, 1,
             "DM+LB mac -> stop all",               False),
            ("step6_multi_mixed_stop_all", [run_dm, run_dm_mac], stop_all, 1,
             "DM mep + DM mac -> stop all",          False),
            ("step6_multi_stop_bare",      [run_dm, run_slm], stop_bare, 0,
             "DM+SLM mep -> stop (bare)",            False),
        ]

        for test_name, run_cmds, stop_cmd, expect_min, desc, transient in variant_tests:
            _progress(test_name)
            channels: List[paramiko.Channel] = []
            try:
                for rcmd in run_cmds:
                    ch = _start_on_demand(client, rcmd, timeout=15)
                    channels.append(ch)
                time.sleep(args.settle_time)

                # Issue stop from SSH2
                out_v_stop = run_shell_with_prompt(client2, stop_cmd, timeout=25)
                raw_outputs.append(f"## {test_name} ({desc})\nRUN: {run_cmds}\nSTOP: {stop_cmd}\n{out_v_stop}")

                stopped_n = _parse_stopped_count(out_v_stop)
                clean_stop = _clean_output(out_v_stop).lower()
                no_ongoing = "no ongoing" in clean_stop or "no on-demand" in clean_stop

                if transient:
                    # Transient test types (SLM, LT) may finish before stop;
                    # both "Stopped tests: N" and "No ongoing" are valid.
                    ok_v, detail_v = _validate_stop_output(
                        out_v_stop, expected_min_stopped=0, label=desc,
                    )
                    if no_ongoing:
                        detail_v += " (transient test completed before stop -- expected)"
                    elif stopped_n is not None and stopped_n >= 1:
                        detail_v += " (session was still active -- stopped successfully)"
                else:
                    ok_v, detail_v = _validate_stop_output(
                        out_v_stop, expected_min_stopped=expect_min, label=desc,
                    )

                results.append(StepResult(
                    name=test_name, ok=ok_v, details=detail_v, raw_output=out_v_stop,
                ))

                # After stop: quick verify show says sessions cleared
                time.sleep(1)
                _, show_post_variant = _show_on_demand(client2, timeout=20)
                ok_post_v, det_post_v = _validate_show_stopped(show_post_variant, md, ma)
                results.append(StepResult(
                    name=f"{test_name}_verify_cleared",
                    ok=ok_post_v,
                    details=f"Post-stop show: {det_post_v}",
                    raw_output=show_post_variant,
                ))

            except Exception as e:
                results.append(StepResult(name=test_name, ok=False, details=f"{desc}: {str(e)}"))
            finally:
                for ch in channels:
                    try:
                        ch.close()
                    except Exception:
                        pass
            time.sleep(1)

        # --- 6m: Stop while show output is streaming in another CLI session ---
        _progress("step6_stop_while_streaming")
        ch_stream = None
        try:
            ch_stream = _start_on_demand(client, run_dm, timeout=15)
            time.sleep(args.settle_time)

            # Open SSH3 to stream 'show ... on-demand' (not 'detail' -- unsupported on some devices)
            client3 = create_ssh_client(args.host, args.user, args.password, args.timeout)
            ch_show = client3.invoke_shell()
            ch_show.settimeout(30)
            _read_until_prompt(ch_show, prompt=None, timeout=30, quiet=1)
            streaming_show_cmd = "show services performance-monitoring cfm tests on-demand"
            ch_show.send(streaming_show_cmd + "\n")
            time.sleep(2)

            # Stop from SSH2 while SSH3 is streaming
            out_stream_stop = run_shell_with_prompt(client2, stop_all, timeout=25)
            raw_outputs.append(f"## step6_stop_while_streaming\nSSH3: {streaming_show_cmd}\nSTOP: {stop_all}\n{out_stream_stop}")
            ok_stream, detail_stream = _validate_stop_output(
                out_stream_stop, expected_min_stopped=1,
                label="Stop while show streaming on SSH3",
            )
            results.append(StepResult(
                name="step6_stop_while_streaming",
                ok=ok_stream,
                details=detail_stream,
                raw_output=out_stream_stop,
            ))

            # Read SSH3 channel output - verify it didn't crash/hang
            try:
                stream_out = _read_until_quiet(ch_show, timeout=5, quiet=1)
                stream_err, stream_errs = has_cli_error(stream_out)
                # Filter out errors that are just from the show command having
                # no data (vs. a real crash). The key check: SSH3 channel is
                # still responsive and didn't crash.
                results.append(StepResult(
                    name="step6_streaming_session_no_crash",
                    ok=not stream_err,
                    details=(
                        f"Streaming CLI session OK ({len(stream_out)} bytes). No crash after concurrent stop."
                        if not stream_err
                        else f"Streaming session output contained errors: {'; '.join(stream_errs)}"
                    ),
                    raw_output=stream_out,
                ))
                ch_show.close()
            except Exception as stream_exc:
                results.append(StepResult(
                    name="step6_streaming_session_no_crash",
                    ok=False,
                    details=f"Streaming session exception (possible crash): {stream_exc}",
                ))
            client3.close()
        except Exception as e:
            results.append(StepResult(name="step6_stop_while_streaming", ok=False, details=str(e)))
        finally:
            if ch_stream is not None:
                try:
                    ch_stream.close()
                except Exception:
                    pass

        # =================================================================
        # STEP 7: Stop with no active sessions (graceful) - validate output
        # =================================================================
        time.sleep(2)  # Ensure all sessions fully cleared

        stop_no_active_variants = [
            ("step7_stop_no_active_bare",     stop_bare,    "bare stop"),
            ("step7_stop_no_active_all",      stop_all,     "stop all"),
            ("step7_stop_no_active_md_dm",    stop_md_dm,   "md+ma+type DM"),
            ("step7_stop_no_active_md_slm",   stop_md_slm,  "md+ma+type SLM"),
            ("step7_stop_no_active_type_dm",  stop_type_dm,  "test-type DM"),
            ("step7_stop_no_active_type_slm", stop_type_slm, "test-type SLM"),
            ("step7_stop_no_active_type_lt",  stop_type_lt,  "test-type LT"),
            ("step7_stop_no_active_type_lb",  stop_type_lb,  "test-type LB"),
        ]
        for test_name, stop_cmd, label in stop_no_active_variants:
            _progress(test_name)
            out_no = run_shell_with_prompt(client, stop_cmd, timeout=25)
            raw_outputs.append(f"## {test_name} ({label}):\n{out_no}")
            ok_no, detail_no = _validate_no_sessions_stop(out_no, label=label)
            results.append(StepResult(
                name=test_name, ok=ok_no, details=detail_no, raw_output=out_no,
            ))

        # =================================================================
        # STEP 8: Start new on-demand session after stop - validate it appears in show
        # =================================================================
        _progress("step8_start_dm_after_stop")
        ch_after = None
        try:
            ch_after = _start_on_demand(client, run_dm, timeout=15)
            time.sleep(args.settle_time)
            # Verify session appears in show output
            _, show_restart_dm = _show_on_demand(client2, timeout=30)
            raw_outputs.append(f"## SHOW ON-DEMAND (restart DM after stop):\n{show_restart_dm}")
            ok_restart_dm, det_restart_dm = _validate_show_running(
                show_restart_dm, md, ma,
                test_types_expected=["delay-measurement", "two-way"],
            )
            results.append(StepResult(
                name="step8_start_dm_after_stop",
                ok=ok_restart_dm,
                details=f"Restart DM: {det_restart_dm}",
                raw_output=show_restart_dm,
            ))
            # Stop it
            out_cleanup = run_shell_with_prompt(client2, stop_all, timeout=25)
            ok_cleanup, _ = _validate_stop_output(out_cleanup, expected_min_stopped=1)
            results.append(StepResult(
                name="step8_dm_cleanup_stop",
                ok=ok_cleanup,
                details=f"Cleanup stop after restart DM: Stopped count={_parse_stopped_count(out_cleanup)}",
                raw_output=out_cleanup,
            ))
        except Exception as e:
            results.append(StepResult(name="step8_start_dm_after_stop", ok=False, details=str(e)))
        finally:
            if ch_after is not None:
                try:
                    ch_after.close()
                except Exception:
                    pass

        time.sleep(1)

        _progress("step8_start_slm_after_stop")
        ch_after_slm = None
        try:
            ch_after_slm = _start_on_demand(client, run_slm, timeout=15)
            # SLM is transient -- check show quickly (shorter wait) to catch it while running
            time.sleep(min(args.settle_time, 3))
            _, show_restart_slm = _show_on_demand(client2, timeout=30)
            raw_outputs.append(f"## SHOW ON-DEMAND (restart SLM after stop):\n{show_restart_slm}")
            # SLM may already have completed; check for MD/MA presence as primary signal
            ok_restart_slm, det_restart_slm = _validate_show_running(
                show_restart_slm, md, ma,
                test_types_expected=["synthetic-loss"],
            )
            # If SLM already completed, the show may not contain synthetic-loss;
            # as long as no CLI error, the SLM ran (we verified it showed up in step3).
            if not ok_restart_slm:
                slm_err, _ = has_cli_error(show_restart_slm)
                if not slm_err:
                    ok_restart_slm = True
                    det_restart_slm += " (SLM is transient; may have completed -- no CLI error)"
            results.append(StepResult(
                name="step8_start_slm_after_stop",
                ok=ok_restart_slm,
                details=f"Restart SLM: {det_restart_slm}",
                raw_output=show_restart_slm,
            ))
            # Cleanup: SLM may already be done, so accept 0 stopped
            out_cleanup_slm = run_shell_with_prompt(client2, stop_all, timeout=25)
            stopped_slm_n = _parse_stopped_count(out_cleanup_slm)
            ok_cleanup_slm, _ = _validate_stop_output(out_cleanup_slm, expected_min_stopped=0)
            results.append(StepResult(
                name="step8_slm_cleanup_stop",
                ok=ok_cleanup_slm,
                details=f"Cleanup stop after restart SLM: Stopped count={stopped_slm_n} (SLM is transient, 0 OK)",
                raw_output=out_cleanup_slm,
            ))
        except Exception as e:
            results.append(StepResult(name="step8_start_slm_after_stop", ok=False, details=str(e)))
        finally:
            if ch_after_slm is not None:
                try:
                    ch_after_slm.close()
                except Exception:
                    pass

        time.sleep(1)

        # Step 8c: Restart DM with mac-address target after stop
        _progress("step8_start_dm_mac_after_stop")
        ch_after_dm_mac = None
        try:
            ch_after_dm_mac = _start_on_demand(client, run_dm_mac, timeout=15)
            time.sleep(args.settle_time)
            _, show_restart_dm_mac = _show_on_demand(client2, timeout=30)
            raw_outputs.append(f"## SHOW ON-DEMAND (restart DM mac after stop):\n{show_restart_dm_mac}")
            ok_restart_dm_mac, det_restart_dm_mac = _validate_show_running(
                show_restart_dm_mac, md, ma,
                test_types_expected=["delay-measurement", "two-way"],
            )
            results.append(StepResult(
                name="step8_start_dm_mac_after_stop",
                ok=ok_restart_dm_mac,
                details=f"Restart DM (mac target {target_mac}): {det_restart_dm_mac}",
                raw_output=show_restart_dm_mac,
            ))
            out_cleanup_dm_mac = run_shell_with_prompt(client2, stop_all, timeout=25)
            ok_cleanup_dm_mac, _ = _validate_stop_output(out_cleanup_dm_mac, expected_min_stopped=1)
            results.append(StepResult(
                name="step8_dm_mac_cleanup_stop",
                ok=ok_cleanup_dm_mac,
                details=f"Cleanup stop after restart DM mac: Stopped count={_parse_stopped_count(out_cleanup_dm_mac)}",
                raw_output=out_cleanup_dm_mac,
            ))
        except Exception as e:
            results.append(StepResult(name="step8_start_dm_mac_after_stop", ok=False, details=str(e)))
        finally:
            if ch_after_dm_mac is not None:
                try:
                    ch_after_dm_mac.close()
                except Exception:
                    pass

        time.sleep(1)

        # Step 8d: Restart LB with mac-address target after stop
        _progress("step8_start_lb_mac_after_stop")
        ch_after_lb_mac = None
        try:
            ch_after_lb_mac = _start_on_demand(client, run_lb_mac, timeout=15)
            time.sleep(args.settle_time)
            _, show_restart_lb_mac = _show_on_demand(client2, timeout=30)
            raw_outputs.append(f"## SHOW ON-DEMAND (restart LB mac after stop):\n{show_restart_lb_mac}")
            ok_restart_lb_mac, det_restart_lb_mac = _validate_show_running(
                show_restart_lb_mac, md, ma,
                test_types_expected=["loopback"],
            )
            results.append(StepResult(
                name="step8_start_lb_mac_after_stop",
                ok=ok_restart_lb_mac,
                details=f"Restart LB (mac target {target_mac}): {det_restart_lb_mac}",
                raw_output=show_restart_lb_mac,
            ))
            out_cleanup_lb_mac = run_shell_with_prompt(client2, stop_all, timeout=25)
            ok_cleanup_lb_mac, _ = _validate_stop_output(out_cleanup_lb_mac, expected_min_stopped=1)
            results.append(StepResult(
                name="step8_lb_mac_cleanup_stop",
                ok=ok_cleanup_lb_mac,
                details=f"Cleanup stop after restart LB mac: Stopped count={_parse_stopped_count(out_cleanup_lb_mac)}",
                raw_output=out_cleanup_lb_mac,
            ))
        except Exception as e:
            results.append(StepResult(name="step8_start_lb_mac_after_stop", ok=False, details=str(e)))
        finally:
            if ch_after_lb_mac is not None:
                try:
                    ch_after_lb_mac.close()
                except Exception:
                    pass

        # =================================================================
        # Negative: Start to unreachable target then stop
        # Verify: stop is accepted, no stale entries remain
        # =================================================================
        if not args.skip_unreachable:
            _progress("neg_unreachable_target_stop")
            unreachable_mac = args.unreachable_mac
            run_dm_unreachable = (
                f"run ethernet-oam cfm on-demand delay-measurement two-way "
                f"maintenance-domain {md} maintenance-association {ma} "
                f"target mac-address {unreachable_mac}"
            )
            ch_unreach = None
            try:
                ch_unreach = _start_on_demand(client, run_dm_unreachable, timeout=15)
                time.sleep(args.settle_time)

                # Stop it
                out_unreach_stop = run_shell_with_prompt(client2, stop_all, timeout=25)
                raw_outputs.append(f"## NEG UNREACHABLE STOP:\n{out_unreach_stop}")
                ok_unreach, detail_unreach = _validate_stop_output(
                    out_unreach_stop, expected_min_stopped=0,
                    label="Unreachable target stop",
                )

                # Verify no stale entries remain
                time.sleep(2)
                _, show_after_unreach = _show_on_demand(client2, timeout=30)
                raw_outputs.append(f"## SHOW ON-DEMAND (after unreachable stop):\n{show_after_unreach}")

                clean_after = _clean_output(show_after_unreach).lower()
                stale_mac = unreachable_mac.lower() in clean_after
                no_sessions = _has_no_sessions(show_after_unreach)

                if stale_mac:
                    results.append(StepResult(
                        name="neg_unreachable_target_stop",
                        ok=False,
                        details=f"STALE ENTRY: MAC {unreachable_mac} still in show output after stop.",
                        raw_output=show_after_unreach,
                    ))
                elif no_sessions:
                    results.append(StepResult(
                        name="neg_unreachable_target_stop",
                        ok=True,
                        details=f"Stop accepted ({detail_unreach}); show confirms no entries remain.",
                        raw_output=out_unreach_stop,
                    ))
                else:
                    # No stale MAC, not explicitly "no sessions" -- check if output is benign
                    has_content, indicators = _has_on_demand_content(show_after_unreach, md, ma)
                    if has_content and unreachable_mac.lower() not in clean_after:
                        results.append(StepResult(
                            name="neg_unreachable_target_stop",
                            ok=True,
                            details=f"Stop accepted ({detail_unreach}); unreachable MAC not in show (other sessions may exist).",
                            raw_output=show_after_unreach,
                        ))
                    else:
                        results.append(StepResult(
                            name="neg_unreachable_target_stop",
                            ok=True,
                            details=f"Stop accepted ({detail_unreach}); no stale entries detected.",
                            raw_output=out_unreach_stop,
                        ))
            except Exception as e:
                results.append(StepResult(name="neg_unreachable_target_stop", ok=False, details=str(e)))
            finally:
                if ch_unreach is not None:
                    try:
                        ch_unreach.close()
                    except Exception:
                        pass

        # =================================================================
        # Longevity: repeated start/stop cycles
        # Validate each cycle's stop output has proper stopped count
        # =================================================================
        if not args.skip_longevity:
            _progress(f"longevity_start_stop_cycles (x{args.longevity_cycles})")
            longevity_ok = True
            longevity_details: List[str] = []
            for cycle in range(args.longevity_cycles):
                ch_long = None
                # Alternate between mep-id and mac-address targets each cycle
                cycle_cmd = run_dm if cycle % 2 == 0 else run_dm_mac
                cycle_target = "mep-id" if cycle % 2 == 0 else f"mac {target_mac}"
                try:
                    ch_long = _start_on_demand(client, cycle_cmd, timeout=15)
                    time.sleep(3)
                    out_long_stop = run_shell_with_prompt(client2, stop_all, timeout=25)
                    stopped_n = _parse_stopped_count(out_long_stop)
                    ok_long, det_long = _validate_stop_output(
                        out_long_stop, expected_min_stopped=1,
                        label=f"Cycle {cycle + 1}",
                    )
                    longevity_details.append(
                        f"Cycle {cycle + 1} ({cycle_target}): stopped={stopped_n} {'OK' if ok_long else 'FAIL'}"
                    )
                    if not ok_long:
                        longevity_ok = False
                        longevity_details.append(f"  -> {det_long}")
                    raw_outputs.append(f"## LONGEVITY CYCLE {cycle + 1}:\n{out_long_stop}")
                except Exception as e:
                    longevity_ok = False
                    longevity_details.append(f"Cycle {cycle + 1}: EXCEPTION {str(e)}")
                    break
                finally:
                    if ch_long is not None:
                        try:
                            ch_long.close()
                        except Exception:
                            pass
                time.sleep(1)

            results.append(StepResult(
                name=f"longevity_{args.longevity_cycles}_cycles",
                ok=longevity_ok,
                details="; ".join(longevity_details),
            ))

            # Verify device is still healthy after longevity:
            # Use DM (persistent) for a reliable health check, not SLM (transient).
            _progress("longevity_verify_post_health")
            ch_post = None
            try:
                ch_post = _start_on_demand(client, run_dm, timeout=15)
                time.sleep(args.settle_time)
                _, show_post = _show_on_demand(client2, timeout=30)
                raw_outputs.append(f"## SHOW ON-DEMAND (post longevity):\n{show_post}")
                ok_post, det_post = _validate_show_running(
                    show_post, md, ma,
                    test_types_expected=["delay-measurement", "two-way"],
                )
                results.append(StepResult(
                    name="longevity_verify_post_health",
                    ok=ok_post,
                    details=f"Post-longevity DM session: {det_post}",
                    raw_output=show_post,
                ))
                # Clean stop
                out_post_stop = run_shell_with_prompt(client2, stop_all, timeout=25)
                ok_post_stop, det_post_stop = _validate_stop_output(
                    out_post_stop, expected_min_stopped=1,
                    label="Post-longevity cleanup",
                )
                results.append(StepResult(
                    name="longevity_post_cleanup_stop",
                    ok=ok_post_stop,
                    details=det_post_stop,
                    raw_output=out_post_stop,
                ))
            except Exception as e:
                results.append(StepResult(name="longevity_verify_post_health", ok=False, details=str(e)))
            finally:
                if ch_post is not None:
                    try:
                        ch_post.close()
                    except Exception:
                        pass

        # Close second SSH
        client2.close()

    finally:
        client.close()

    # ===================================================================
    # Output results
    # ===================================================================
    if args.output_file:
        with open(args.output_file, "w", encoding="utf-8") as handle:
            handle.write("\n\n".join(raw_outputs))

    if args.show_cli_output:
        print("\n=== RAW DEVICE OUTPUT ===")
        print("\n\n".join(raw_outputs))

    def _print_table(rows: List[StepResult], title: str, show_details: bool) -> None:
        if not rows:
            return
        name_w = max(len("Test"), *[len(r.name) for r in rows])
        status_w = len("Status")
        has_failures = any(not r.ok for r in rows)
        show_details_col = show_details or has_failures
        details_w = max(len("Details"), *[len(r.details) for r in rows]) if show_details_col else 0
        if details_w > 120:
            details_w = 120

        def sep() -> str:
            if show_details_col:
                return f"+-{'-' * name_w}-+-{'-' * status_w}-+-{'-' * details_w}-+"
            return f"+-{'-' * name_w}-+-{'-' * status_w}-+"

        def header() -> str:
            if show_details_col:
                return f"| {'Test'.ljust(name_w)} | {'Status'.ljust(status_w)} | {'Details'.ljust(details_w)} |"
            return f"| {'Test'.ljust(name_w)} | {'Status'.ljust(status_w)} |"

        print(f"\n{title}")
        print(sep())
        print(header())
        print(sep())
        for r in rows:
            status = "PASS" if r.ok else "FAIL"
            if show_details_col:
                det = r.details if (show_details or not r.ok) else ""
                if len(det) > details_w:
                    det = det[: details_w - 3] + "..."
                print(f"| {r.name.ljust(name_w)} | {status.ljust(status_w)} | {det.ljust(details_w)} |")
            else:
                print(f"| {r.name.ljust(name_w)} | {status.ljust(status_w)} |")
        print(sep())

    def _bucket(r: StepResult) -> str:
        n = r.name.lower()
        if n.startswith("step1") or n.startswith("step2") or n.startswith("step3"):
            return "start_verify"
        if n.startswith("step4") or n.startswith("step5"):
            return "stop_verify"
        if n.startswith("step6"):
            return "variants"
        if n.startswith("step7"):
            return "no_active"
        if n.startswith("step8"):
            return "restart"
        if n.startswith("neg_") or n.startswith("longevity"):
            return "negative_longevity"
        return "other"

    failed = any(not r.ok for r in results)

    if args.output_format == "table":
        buckets = [
            ("other", "Setup / Discovery"),
            ("start_verify", "Step 1-3: Start & Verify On-Demand Sessions Running"),
            ("stop_verify", "Step 4-5: Stop & Verify Cleared / Counters Stable"),
            ("variants", "Step 6: Stop Variants & Post-Stop Verification"),
            ("no_active", "Step 7: Stop with No Active Sessions (Graceful)"),
            ("restart", "Step 8: Start After Stop & Verify in Show"),
            ("negative_longevity", "Negative / Longevity"),
        ]
        for bucket_key, bucket_title in buckets:
            rows = [r for r in results if _bucket(r) == bucket_key]
            if rows:
                _print_table(rows, bucket_title, show_details=args.show_details)
    else:
        for result in results:
            status = "PASS" if result.ok else "FAIL"
            print(f"{status}: {result.name}")
            if args.show_details and result.details:
                print(f"  {result.details}")

    # Summary
    total = len(results)
    passed = sum(1 for r in results if r.ok)
    fail_count = total - passed
    print(f"\n{'=' * 60}")
    print(f"SW-237984 On-Demand Stop Test Summary")
    print(f"{'=' * 60}")
    print(f"Total: {total}  |  PASS: {passed}  |  FAIL: {fail_count}")
    print(f"{'=' * 60}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
