#!/usr/bin/env python3
"""
Y.1731 DM/SLM CLI and TAB completion test (SW-235373, SW-235927, SW-235372).
This script does not use 'rollback 0': discovery, validation, cleanup, and
commit-check sequences tear down only the PM sessions/profiles they create,
so your candidate config is preserved.

Device note: rollback 0 only rolls back the candidate (config you are about to
commit). To revert older committed configs use rollback 1, 2, etc.; use
'show config committed' (sh con com) to see commit history.
"""
import argparse
import getpass
import re
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

import paramiko


PROMPT_MARKERS = ("#", ">")
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


@dataclass
class StepResult:
    name: str
    ok: bool
    details: str
    raw_output: str = ""


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
        # DNOS commonly formats errors like:
        # - "ERROR: Unknown word: '...'."
        # - "Error: ..."
        # - "Commit check failed"
        # Avoid \b anchors because "ERROR:" ends with ':' (non-word char).
        if re.search(
            r"(Error:|ERROR:|Unknown command|Invalid command|Commit check failed|commit check has failed|Commit failed|Command failed|TRANSACTION_COMMIT_CHECK_FAILED|missing a mandatory leaf|rpc-error)",
            line,
            flags=re.IGNORECASE,
        ):
            stripped = line.strip()
            # Redact very long "Invalid value '<...>'" strings.
            m = re.search(r"(Invalid value ')([^']+)(')", stripped)
            if m and len(m.group(2)) > 64:
                stripped = stripped[: m.start(2)] + "<redacted>" + stripped[m.end(2) :]
            errors.append(stripped)
    return (len(errors) > 0, errors)


def _read_until_prompt_then_drain(channel, prompt: Optional[str], timeout: int) -> str:
    """
    Read until we see a prompt, then drain any delayed output.
    This helps catch commit/commit-check failures printed after the prompt.
    Drain uses a short timeout so we don't hang for another full timeout if the device keeps sending.
    """
    output = _read_until_prompt(channel, prompt=prompt, timeout=timeout, quiet=2)
    drain_timeout = min(timeout, 4)
    output += _read_until_quiet(channel, timeout=drain_timeout, quiet=0.8)
    return output


def _first_successful_show(
    client: paramiko.SSHClient, commands: List[str], timeout: int = 30
) -> Tuple[Optional[str], str]:
    """
    Try show commands in order and return (command_used, output) for the first one that
    does not look like a CLI error. Returns (None, last_output) if all fail.
    """
    last = ""
    for cmd in commands:
        out = run_shell_with_prompt(client, cmd, timeout=timeout)
        last = out
        err, _ = has_cli_error(out)
        if not err:
            return cmd, out
    return None, last


def show_config_contains(client: paramiko.SSHClient, match_text: str, timeout: int = 30) -> Tuple[bool, str]:
    """
    Best-effort 'show config' check that the running config contains match_text.
    Tries with paging disabled first so full output is captured (avoids truncation at '-- More --').
    """
    show_cmd = "show config services performance-monitoring | no-more"
    for prefix in ("set cli screen-length 0", "terminal length 0"):
        try:
            out = run_shell_sequence(client, [prefix, show_cmd], timeout=timeout + 15)
            err, _ = has_cli_error(out)
            if not err and match_text in out:
                return True, f"Found '{match_text}' in '{show_cmd}'."
        except Exception:
            continue
    show_cmds = [
        "show config services performance-monitoring | display-set | no-more",
        "show configuration services performance-monitoring | display-set | no-more",
        "show config services performance-monitoring | no-more",
        "show configuration services performance-monitoring | no-more",
    ]
    used, out = _first_successful_show(client, show_cmds, timeout=timeout)
    if not used:
        return False, "Unable to run show config for services performance-monitoring."
    if match_text in out:
        return True, f"Found '{match_text}' in '{used}'."
    sample = "\n".join(out.splitlines()[:40]).strip()
    return False, f"Did not find '{match_text}' in '{used}'.\n--- Output sample ---\n{sample}"


def discover_cfm_context(
    client: paramiko.SSHClient, timeout: int = 30
) -> Tuple[bool, str, Optional[str], Optional[str], Optional[str], Optional[str], Optional[str]]:
    """
    Discover (md, ma, local_mep_id, target_mep_id, direction) from existing
    'services ethernet-oam connectivity-fault-management' config on the device.
    When CFM has two sessions (one down MEP, one up MEP), direction is down or up.

    Returns: (ok, details, md, ma, mep_id, target_str, direction)
    """
    show_cmds = [
        # Preferred narrow view
        "show config services ethernet-oam connectivity-fault-management | display-set",
        "show configuration services ethernet-oam connectivity-fault-management | display-set",
        "show config services ethernet-oam connectivity-fault-management",
        "show configuration services ethernet-oam connectivity-fault-management",
        # Broader fallbacks (some CLIs don't support sub-tree show the same way)
        "show config services ethernet-oam | display-set | match connectivity-fault-management",
        "show configuration services ethernet-oam | display-set | match connectivity-fault-management",
        "show config services ethernet-oam | match connectivity-fault-management",
        "show configuration services ethernet-oam | match connectivity-fault-management",
    ]

    used: Optional[str] = None
    output = ""
    # Use run_shell_with_prompt_long for hierarchical config output that may
    # be large (multiple maintenance-domains). Normal quiet=1.2s can truncate.
    for cmd in show_cmds:
        out = run_shell_with_prompt_long(client, cmd, timeout=max(timeout, 60))
        err, _ = has_cli_error(out)
        # Accept only if:
        # - Not an error AND
        # - Has some relevant content (not just prompt/echo)
        if (not err) and re.search(r"(ethernet-oam|connectivity-fault-management|maintenance)", out, re.IGNORECASE):
            used = cmd
            output = out
            break

    if not used:
        return (
            False,
            "Failed to read connectivity-fault-management config (show command not accepted).",
            None,
            None,
            None,
            None,
            None,
        )

    direction_re = re.compile(r"\bdirection\s+(down|up)\b", flags=re.IGNORECASE)
    md_re = re.compile(
        r"\bmaintenance[-_]domain(?:s)?(?:[-_]name)?\s+(\S+)", flags=re.IGNORECASE
    )
    ma_re = re.compile(
        r"\bmaintenance[-_]association(?:s)?(?:[-_]name)?\s+(\S+)", flags=re.IGNORECASE
    )
    mep_id_re = re.compile(r"\bmep[-_]id\s+(\d+)\b", flags=re.IGNORECASE)
    mep_re = re.compile(r"\bmep\s+(\d+)\b", flags=re.IGNORECASE)
    remote_mep_re = re.compile(
        r"\bremote[-_]mep(?:s)?(?:[-_]id)?\s+(\d+)\b", flags=re.IGNORECASE
    )

    # Collect candidates keyed by (md, ma).
    # Support both "display-set" output (md/ma in same line) and hierarchical output
    # (md/ma on separate indented lines).
    candidates: Dict[Tuple[str, str], Dict[str, Set[int]]] = {}
    current_md: Optional[str] = None
    current_ma: Optional[str] = None
    for line in output.splitlines():
        md_m = md_re.search(line)
        if md_m:
            current_md = md_m.group(1)
            # New MD resets MA context.
            current_ma = None

        ma_m = ma_re.search(line)
        if ma_m:
            current_ma = ma_m.group(1)

        # If md/ma are present in the same line, prefer those for this line.
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

        # Remote MEP IDs frequently appear under the same md/ma context; don't treat them
        # as local MEP IDs (otherwise we might pick a remote MEP as the "source" MEP).
        # "crosscheck mep-id N" is a remote MEP; only "local-mep N" is the local MEP.
        is_remote_line = (
            bool(remote_mep_re.search(line))
            or ("remote-mep" in line.lower())
            or ("remote_mep" in line.lower())
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
        # Some configs may use "local-mep N" (mep_re matches "mep N" in "local-mep N").
        for m in mep_re.finditer(line):
            candidates[key]["meps"].add(int(m.group(1)))

    if not candidates:
        sample = "\n".join(output.splitlines()[:30]).strip()
        return (
            False,
            f"Read CFM config via '{used}', but couldn't find maintenance-domain/association lines to parse."
            + (f"\n--- Output sample ---\n{sample}" if sample else ""),
            None,
            None,
            None,
            None,
            None,
        )

    # Pick a deterministic candidate:
    # - Prefer first md/ma alphabetically that has at least one MEP
    # - Otherwise fall back to first md/ma alphabetically (so we can prompt for MEP only)
    best_key: Optional[Tuple[str, str]] = None
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

    target_mep: Optional[int] = None
    if remote_meps:
        target_mep = remote_meps[0]
    elif len(meps) >= 2:
        target_mep = next((m for m in meps if m != meps[0]), None)

    target_str = f"mep-id {target_mep}" if target_mep is not None else None
    direction = candidates.get(best_key, {}).get("direction")
    details = f"Discovered CFM context from '{used}': md={md} ma={ma}" + (
        f" mep-id={local_mep}" if local_mep else " mep-id=<not-found>"
    ) + (f" target={target_str}" if target_str else " target=<not-found>") + (
        f" direction={direction}" if direction else ""
    )
    return True, details, md, ma, local_mep, target_str, direction


def discover_all_local_meps(
    client: paramiko.SSHClient, timeout: int = 30
) -> Tuple[bool, str, List[Tuple[str, str, str, Optional[str], Optional[str]]]]:
    """
    Discover all local MEPs from existing ethernet-oam CFM config.
    Returns (ok, details, list of (md, ma, mep_id, direction, target_str)).
    """
    show_cmds = [
        # Try display-set first (flat format, easier to parse) with no-more to disable paging
        "show config services ethernet-oam connectivity-fault-management | display-set | no-more",
        "show configuration services ethernet-oam connectivity-fault-management | display-set | no-more",
        # Fallback to hierarchical with no-more
        "show config services ethernet-oam connectivity-fault-management | no-more",
        "show configuration services ethernet-oam connectivity-fault-management | no-more",
        # Without no-more (in case it's not supported on older versions)
        "show config services ethernet-oam connectivity-fault-management | display-set",
        "show configuration services ethernet-oam connectivity-fault-management | display-set",
        "show config services ethernet-oam connectivity-fault-management",
        "show configuration services ethernet-oam connectivity-fault-management",
        # Broader fallbacks
        "show config services ethernet-oam | display-set | match connectivity-fault-management | no-more",
        "show config services ethernet-oam | match connectivity-fault-management | no-more",
        # Last resort: full services config
        "show config services | display-set | match \"maintenance-domain\\|local-mep\" | no-more",
    ]
    used: Optional[str] = None
    output = ""
    # Use run_shell_with_prompt_long for hierarchical config output that may
    # be large (multiple maintenance-domains). The normal quiet=1.2s can
    # truncate the output if the device pauses between sections.
    for cmd in show_cmds:
        out = run_shell_with_prompt_long(client, cmd, timeout=max(timeout, 60))
        err, _ = has_cli_error(out)
        if (not err) and re.search(r"(ethernet-oam|connectivity-fault-management|maintenance|local-mep)", out, re.IGNORECASE):
            used = cmd
            output = out
            break
    if not used:
        return (
            False,
            "Failed to read connectivity-fault-management config (show command not accepted).",
            [],
        )
    
    # Debug: Log which command worked and output size
    output_lines = len(output.splitlines())
    output_chars = len(output)
    print(f"  [DEBUG] Discovery used: {used}")
    print(f"  [DEBUG] Output: {output_lines} lines, {output_chars} chars")
    
    # DEBUG: Save raw output to file for inspection
    with open("/tmp/y1731_discovery_output.txt", "w") as f:
        f.write(f"Command: {used}\n")
        f.write(f"Output length: {output_lines} lines, {output_chars} chars\n")
        f.write("="*70 + "\n")
        f.write(output)
    print(f"  [DEBUG] Raw output saved to /tmp/y1731_discovery_output.txt")
    
    direction_re = re.compile(r"\bdirection\s+(down|up)\b", flags=re.IGNORECASE)
    md_re = re.compile(r"\bmaintenance[-_]domain(?:s)?(?:[-_]name)?\s+(\S+)", flags=re.IGNORECASE)
    ma_re = re.compile(r"\bmaintenance[-_]association(?:s)?(?:[-_]name)?\s+(\S+)", flags=re.IGNORECASE)
    mep_id_re = re.compile(r"\bmep[-_]id\s+(\d+)\b", flags=re.IGNORECASE)
    # Match "local-mep N" specifically (not just "mep N")
    local_mep_re = re.compile(r"\blocal[-_]mep\s+(\d+)\b", flags=re.IGNORECASE)
    mep_re = re.compile(r"\bmep\s+(\d+)\b", flags=re.IGNORECASE)
    remote_mep_re = re.compile(r"\bremote[-_]mep(?:s)?(?:[-_]id)?\s+(\d+)\b", flags=re.IGNORECASE)
    candidates: Dict[Tuple[str, str], Dict[str, Set[int]]] = {}
    current_md: Optional[str] = None
    current_ma: Optional[str] = None
    
    # Debug: Track what we find
    found_mds = set()
    found_mas = set()
    found_local_meps = []
    
    for line in output.splitlines():
        md_m = md_re.search(line)
        if md_m:
            current_md = md_m.group(1)
            found_mds.add(current_md)
            current_ma = None
        ma_m = ma_re.search(line)
        if ma_m:
            current_ma = ma_m.group(1)
            found_mas.add(current_ma)
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
        # "crosscheck mep-id N" is remote; only "local-mep N" is local.
        is_remote_line = (
            bool(remote_mep_re.search(line))
            or ("remote-mep" in line.lower())
            or ("remote_mep" in line.lower())
            or ("crosscheck" in line.lower())
        )
        for m in remote_mep_re.finditer(line):
            candidates[key]["remote_meps"].add(int(m.group(1)))
        if "crosscheck" in line.lower():
            for m in mep_id_re.finditer(line):
                candidates[key]["remote_meps"].add(int(m.group(1)))
        if is_remote_line:
            continue
        # For local MEPs: prioritize "local-mep N" pattern
        for m in local_mep_re.finditer(line):
            mep_num = int(m.group(1))
            candidates[key]["meps"].add(mep_num)
            found_local_meps.append(f"{line_md}/{line_ma}/MEP{mep_num}")
        # Also check for "mep-id N" in non-remote lines (PM session source)
        for m in mep_id_re.finditer(line):
            candidates[key]["meps"].add(int(m.group(1)))
        # Only use generic "mep N" if not in a remote context
        for m in mep_re.finditer(line):
            # Skip if this looks like it's part of "local-mep" or "remote-mep"
            if "local-mep" not in line.lower() and "remote-mep" not in line.lower():
                candidates[key]["meps"].add(int(m.group(1)))
    
    # Debug: Show what was found during parsing
    print(f"  [DEBUG] Found MDs: {sorted(found_mds)}")
    print(f"  [DEBUG] Found MAs: {sorted(found_mas)}")
    print(f"  [DEBUG] Found local-meps: {found_local_meps}")
    
    if not candidates:
        sample = "\n".join(output.splitlines()[:30]).strip()
        return (
            False,
            f"Read CFM config via '{used}', but couldn't find maintenance-domain/association lines."
            + (f"\n--- Output sample ---\n{sample}" if sample else ""),
            [],
        )
    result_list: List[Tuple[str, str, str, Optional[str], Optional[str]]] = []
    for key in sorted(candidates.keys()):
        md, ma = key
        c = candidates[key]
        meps = sorted(c["meps"])
        remote_meps = sorted(c["remote_meps"])
        target_str: Optional[str] = f"mep-id {remote_meps[0]}" if remote_meps else None
        direction = c.get("direction")
        for mep_id in meps:
            result_list.append((md, ma, str(mep_id), direction, target_str))
    details = f"Discovered {len(result_list)} local MEP(s) from '{used}'."
    return True, details, result_list


def redact_text(text: str) -> str:
    """Redact long Invalid value payloads from raw CLI output."""
    out_lines: List[str] = []
    for line in text.splitlines():
        m = re.search(r"(Invalid value ')([^']+)(')", line)
        if m and len(m.group(2)) > 64:
            line = line[: m.start(2)] + "<redacted>" + line[m.end(2) :]
        out_lines.append(line)
    return "\n".join(out_lines)


def build_dm_profile_commands(profile: str) -> List[str]:
    # Minimal valid profile so session commit can succeed.
    return [
        f"services performance-monitoring profiles cfm two-way-delay-measurement {profile}",
        "inform-test-results enabled",
        "test-duration probes probe-count 5 probe-interval 1 repeat-interval 10",
        "thresholds delay-rtt-min 100",
        "thresholds delay-rtt-avg 1000",
        "thresholds delay-rtt-max 2000",
        "thresholds jitter-rtt-avg 500",
        "thresholds jitter-rtt-max 1000",
        "thresholds success-rate 90",
        "exit",
    ]


def exit_profiles_to_cfg_root() -> List[str]:
    # After exiting the DM profile submode, the prompt is typically under
    # services -> performance-monitoring -> profiles -> cfm (cfg-pm-profiles-cfm).
    # We need to return to (cfg)# so that "services performance-monitoring cfm ..."
    # is accepted.
    return ["exit", "exit", "exit", "exit"]


def teardown_dm_profile_commands(profile_name: str) -> List[str]:
    """Return commands to remove a DM profile and leave configure (no rollback 0).
    Profile is 5 levels under configure; use 5 exits to reach configure before 'no services ...'.
    """
    return (
        ["exit", "exit", "exit", "exit", "exit"]
        + [f"no services performance-monitoring profiles cfm two-way-delay-measurement {profile_name}"]
        + ["exit"]
    )


def teardown_slm_profile_commands(profile_name: str) -> List[str]:
    """Return commands to remove an SLM profile and leave configure (no rollback 0).
    Profile is 5 levels under configure; use 5 exits to reach configure before 'no services ...'.
    """
    return (
        ["exit", "exit", "exit", "exit", "exit"]
        + [f"no services performance-monitoring profiles cfm two-way-synthetic-loss-measurement {profile_name}"]
        + ["exit"]
    )


def teardown_dm_session_commands(session_name: str) -> List[str]:
    """Return commands to remove a DM session and leave configure (no rollback 0)."""
    return (
        ["exit", "exit", "exit", "exit"]
        + [f"no services performance-monitoring cfm two-way-delay-measurement {session_name}"]
        + ["exit"]
    )


def teardown_slm_session_commands(session_name: str) -> List[str]:
    """Return commands to remove an SLM session and leave configure (no rollback 0)."""
    return (
        ["exit", "exit", "exit", "exit"]
        + [f"no services performance-monitoring cfm two-way-synthetic-loss-measurement {session_name}"]
        + ["exit"]
    )


def teardown_dm_session_and_profile(session_name: str, profile_name: str, from_session_context: bool = True) -> List[str]:
    """Teardown for negative tests that created both DM session and profile (no rollback 0).
    If from_session_context is True, we're inside the session (4 exits to configure).
    If False, we're already one level up e.g. after 'exit' from session (3 exits to configure).
    """
    n_exits = 4 if from_session_context else 3
    return (
        ["exit"] * n_exits
        + [f"no services performance-monitoring cfm two-way-delay-measurement {session_name}"]
        + [f"no services performance-monitoring profiles cfm two-way-delay-measurement {profile_name}"]
        + ["exit"]
    )


def teardown_slm_session_and_profile(session_name: str, profile_name: str) -> List[str]:
    """Teardown for negative tests that created both SLM session and profile (no rollback 0)."""
    return (
        ["exit", "exit", "exit", "exit"]
        + [f"no services performance-monitoring cfm two-way-synthetic-loss-measurement {session_name}"]
        + [f"no services performance-monitoring profiles cfm two-way-synthetic-loss-measurement {profile_name}"]
        + ["exit"]
    )


def build_slm_profile_commands(profile: str, pcp: int = 5) -> List[str]:
    # Minimal valid profile so session commit can succeed.
    return [
        f"services performance-monitoring profiles cfm two-way-synthetic-loss-measurement {profile}",
        f"pcp {pcp}",
        "inform-test-results enabled",
        "test-duration probes probe-count 5 probe-interval 1 repeat-interval 10",
        "thresholds near-end-loss 1",
        "thresholds far-end-loss 1",
        "exit",
    ]


def run_shell_with_prompt(client: paramiko.SSHClient, command: str, timeout: int = 30) -> str:
    channel = client.invoke_shell()
    channel.settimeout(timeout)
    banner = _read_until_prompt(channel, prompt=None, timeout=timeout, quiet=1)
    channel.send(command + "\n")
    # Don't lock to a specific prompt string because the prompt changes between
    # operational/config/submodes. Just wait until we see any prompt marker again.
    output = _read_until_prompt_then_drain(channel, prompt=None, timeout=timeout)
    channel.close()
    return redact_text(banner + output)


def run_shell_with_prompt_long(client: paramiko.SSHClient, command: str, timeout: int = 60) -> str:
    """Like run_shell_with_prompt but with longer timeout for large output.
    Best used with '| no-more' to disable paging."""
    channel = client.invoke_shell()
    channel.settimeout(timeout)
    banner = _read_until_prompt(channel, prompt=None, timeout=timeout, quiet=2)
    channel.send(command + "\n")
    
    # Simple read with generous quiet threshold since no-more disables paging
    output = _read_until_prompt(channel, prompt=None, timeout=timeout, quiet=5)
    
    # Extra drain to catch any stragglers
    time.sleep(1)
    while channel.recv_ready():
        try:
            output += channel.recv(4096).decode(errors="ignore")
        except:
            break
    
    channel.close()
    return redact_text(banner + output)


def run_shell_sequence(client: paramiko.SSHClient, commands: List[str], timeout: int = 30) -> str:
    channel = client.invoke_shell()
    channel.settimeout(timeout)
    banner = _read_until_prompt(channel, prompt=None, timeout=timeout, quiet=1)
    output = banner
    for cmd in commands:
        channel.send(cmd + "\n")
        output += _read_until_prompt_then_drain(channel, prompt=None, timeout=timeout)
    channel.close()
    return redact_text(output)


def run_shell_sequence_detailed(
    client: paramiko.SSHClient, commands: List[str], timeout: int = 30
) -> List[Tuple[str, str]]:
    channel = client.invoke_shell()
    channel.settimeout(timeout)
    banner = _read_until_prompt(channel, prompt=None, timeout=timeout, quiet=1)
    results: List[Tuple[str, str]] = []
    for cmd in commands:
        channel.send(cmd + "\n")
        output = _read_until_prompt_then_drain(channel, prompt=None, timeout=timeout)
        results.append((cmd, redact_text(banner + output)))
    channel.close()
    return results


def run_shell_sequence_detailed_safe(
    client: paramiko.SSHClient, commands: List[str], timeout: int = 30
) -> List[Tuple[str, str]]:
    """
    "Safe" variant: keeps one shell session, but can be retried by caller.
    (Do NOT open a new shell per command; that breaks config-mode sequences.)
    """
    return run_shell_sequence_detailed(client, commands, timeout=timeout)


def _start_on_demand(client: paramiko.SSHClient, command: str, timeout: int = 10) -> paramiko.Channel:
    """
    Start an on-demand CFM test on a persistent shell channel.
    Returns the channel (still open) so the test keeps running.
    The caller is responsible for closing the channel after the stop test.
    """
    channel = client.invoke_shell()
    channel.settimeout(timeout)
    _read_until_prompt(channel, prompt=None, timeout=timeout, quiet=1)
    channel.send(command + "\n")
    # Give the device a moment to start processing, but don't wait for prompt
    # (on-demand commands may not return a prompt until they finish).
    time.sleep(1)
    return channel


def _validate_stop_output(output: str) -> Tuple[bool, str]:
    """
    Validate that 'request ethernet-oam cfm on-demand stop' output is correct.
    Pass if output contains 'Stopped tests' or 'Total stopped tests' or 'No ongoing'.
    Fail only on CLI errors or completely empty output.
    """
    lower = output.lower()
    if "unknown command" in lower or "invalid command" in lower or "syntax error" in lower:
        return False, "CLI error in stop command."
    if "stopped tests" in lower or "total stopped" in lower:
        return True, "Stop command returned stopped tests."
    if "no ongoing" in lower or "no on-demand" in lower or "no tests" in lower:
        return True, "No ongoing tests to stop (command accepted)."
    if len(output.strip()) < 10:
        return False, "Stop command returned empty/minimal output."
    # Accept any non-error output (device may have different wording)
    return True, "Stop command accepted (output received)."


def run_on_demand_stop_tests(
    client: paramiko.SSHClient,
    args,
    results: list,
    raw_outputs: list,
    _progress,
) -> None:
    """
    Run on-demand start+stop test matrix.
    1. Disable proactive DM/SLM sessions (admin-state disabled)
    2. For each test: start on-demand on SSH1, stop from SSH2
    3. Re-enable proactive DM/SLM sessions
    """
    target_mep = args.target.split()[-1] if "mep-id" in args.target else "1"
    md, ma = args.md, args.ma

    # -- Step A: Disable proactive sessions so on-demand can run --
    _progress("on_demand_disable_proactive")
    disable_cmds = [
        "configure",
        f"services performance-monitoring cfm two-way-delay-measurement {args.session}",
        "admin-state disabled",
        "exit", "exit", "exit", "exit",
        f"services performance-monitoring cfm two-way-synthetic-loss-measurement {args.slm_session}",
        "admin-state disabled",
        "exit", "exit", "exit", "exit",
        "commit",
        "exit",
    ]
    out_disable = run_shell_sequence(client, disable_cmds, timeout=30)
    err_dis, errs_dis = has_cli_error(out_disable)
    results.append(StepResult(
        name="on_demand_disable_proactive",
        ok=not err_dis,
        details="Proactive sessions disabled." if not err_dis else "\n".join(errs_dis),
    ))
    if err_dis:
        # If we can't disable proactive, skip on-demand tests
        results.append(StepResult(name="on_demand_tests", ok=False, details="Skipped: could not disable proactive sessions."))
        return

    # -- Step B: Open second SSH client for stop commands --
    client2 = create_ssh_client(args.host, args.user, args.password, args.timeout)

    # On-demand run command templates
    run_dm_mep = f"run ethernet-oam cfm on-demand delay-measurement two-way maintenance-domain {md} maintenance-association {ma} target mep-id {target_mep}"
    run_dm_mac = f"run ethernet-oam cfm on-demand delay-measurement two-way maintenance-domain {md} maintenance-association {ma} target mac-address 00:11:22:33:44:55"
    run_slm_mep = f"run ethernet-oam cfm on-demand synthetic-loss-measurement two-way maintenance-domain {md} maintenance-association {ma} target mep-id {target_mep}"
    run_slm_mac = f"run ethernet-oam cfm on-demand synthetic-loss-measurement two-way maintenance-domain {md} maintenance-association {ma} target mac-address 00:11:22:33:44:55"
    run_lb_mep = f"run ethernet-oam cfm on-demand loopback maintenance-domain {md} maintenance-association {ma} target mep-id {target_mep}"
    run_lb_mac = f"run ethernet-oam cfm on-demand loopback maintenance-domain {md} maintenance-association {ma} target mac-address 00:11:22:33:44:55"
    run_lt_mep = f"run ethernet-oam cfm on-demand linktrace maintenance-domain {md} maintenance-association {ma} target mep-id {target_mep}"
    run_lt_mac = f"run ethernet-oam cfm on-demand linktrace maintenance-domain {md} maintenance-association {ma} target mac-address 00:11:22:33:44:55"

    # Stop command templates
    stop_all = "request ethernet-oam cfm on-demand stop all"
    stop_md = f"request ethernet-oam cfm on-demand stop maintenance-domain {md} maintenance-association {ma}"
    stop_type_dm = "request ethernet-oam cfm on-demand stop test-type delay-measurement"
    stop_type_slm = "request ethernet-oam cfm on-demand stop test-type synthetic-loss-measurement"
    stop_type_lb = "request ethernet-oam cfm on-demand stop test-type loopback"
    stop_type_lt = "request ethernet-oam cfm on-demand stop test-type linktrace"

    # Test matrix: (test_name, run_cmd, stop_cmd)
    test_matrix = [
        # Stop all
        ("on_demand_dm_mep_stop_all", run_dm_mep, stop_all),
        ("on_demand_dm_mac_stop_all", run_dm_mac, stop_all),
        # Stop per MD/MA
        ("on_demand_slm_mep_stop_md", run_slm_mep, stop_md),
        ("on_demand_slm_mac_stop_md", run_slm_mac, stop_md),
        ("on_demand_lt_mep_stop_md", run_lt_mep, stop_md),
        # Stop per test-type
        ("on_demand_dm_mep_stop_type", run_dm_mep, stop_type_dm),
        ("on_demand_slm_mep_stop_type", run_slm_mep, stop_type_slm),
        ("on_demand_lb_mep_stop_type", run_lb_mep, stop_type_lb),
        ("on_demand_lb_mac_stop_type", run_lb_mac, stop_type_lb),
        ("on_demand_lt_mep_stop_type", run_lt_mep, stop_type_lt),
        ("on_demand_lt_mac_stop_type", run_lt_mac, stop_type_lt),
    ]

    try:
        for test_name, run_cmd, stop_cmd in test_matrix:
            _progress(test_name)
            channel = None
            try:
                # SSH1: start on-demand test (non-blocking channel)
                channel = _start_on_demand(client, run_cmd, timeout=15)
                # Give device time to register the on-demand test
                time.sleep(3)
                # SSH2: send stop command
                out_stop = run_shell_with_prompt(client2, stop_cmd, timeout=25)
                raw_outputs.append(f"## ON-DEMAND STOP: {test_name}\nRUN: {run_cmd}\nSTOP: {stop_cmd}\n{out_stop}")
                ok, detail = _validate_stop_output(out_stop)
                results.append(StepResult(name=test_name, ok=ok, details=detail, raw_output=out_stop))
            except Exception as e:
                results.append(StepResult(name=test_name, ok=False, details=str(e)))
            finally:
                if channel is not None:
                    try:
                        channel.close()
                    except Exception:
                        pass

        # Combined test: start all 4 types, stop all, verify count
        _progress("on_demand_all_stop_all")
        channels = []
        try:
            for cmd in [run_dm_mep, run_slm_mep, run_lb_mep, run_lt_mep]:
                ch = _start_on_demand(client, cmd, timeout=15)
                channels.append(ch)
            time.sleep(4)
            out_stop_all = run_shell_with_prompt(client2, stop_all, timeout=25)
            raw_outputs.append(f"## ON-DEMAND STOP: on_demand_all_stop_all\n{out_stop_all}")
            ok_all, detail_all = _validate_stop_output(out_stop_all)
            results.append(StepResult(
                name="on_demand_all_stop_all",
                ok=ok_all,
                details=detail_all,
                raw_output=out_stop_all,
            ))
        except Exception as e:
            results.append(StepResult(name="on_demand_all_stop_all", ok=False, details=str(e)))
        finally:
            for ch in channels:
                try:
                    ch.close()
                except Exception:
                    pass
    finally:
        client2.close()

    # -- Step D: Re-enable proactive sessions --
    _progress("on_demand_reenable_proactive")
    enable_cmds = [
        "configure",
        f"services performance-monitoring cfm two-way-delay-measurement {args.session}",
        "admin-state enabled",
        "exit", "exit", "exit", "exit",
        f"services performance-monitoring cfm two-way-synthetic-loss-measurement {args.slm_session}",
        "admin-state enabled",
        "exit", "exit", "exit", "exit",
        "commit",
        "exit",
    ]
    out_enable = run_shell_sequence(client, enable_cmds, timeout=30)
    err_en, errs_en = has_cli_error(out_enable)
    results.append(StepResult(
        name="on_demand_reenable_proactive",
        ok=not err_en,
        details="Proactive sessions re-enabled." if not err_en else "\n".join(errs_en),
    ))


def run_tab_completion(client: paramiko.SSHClient, prefix: str, timeout: int = 10) -> str:
    channel = client.invoke_shell()
    channel.settimeout(timeout)
    banner = _read_until_prompt(channel, prompt=None, timeout=timeout, quiet=1)
    prompt = _extract_prompt(banner)
    channel.send(prefix)
    channel.send("\t")
    output = _read_until_prompt(channel, prompt=prompt, timeout=timeout, quiet=1.5)
    # Clear line
    channel.send("\n")
    _read_until_prompt(channel, prompt=prompt, timeout=timeout, quiet=1.0)
    channel.close()
    return redact_text(banner + output)


def _extract_numeric_completions(output: str) -> List[int]:
    """
    Best-effort parser for TAB completion output that lists numeric options.
    We only trust lines that contain no letters (avoid parsing timestamps/words).
    """
    nums: Set[int] = set()
    for line in ANSI_ESCAPE.sub("", output).splitlines():
        s = line.strip()
        if not s:
            continue
        # Ignore any line containing letters to avoid picking up IDs from unrelated text.
        if re.search(r"[A-Za-z]", s):
            continue
        for tok in re.findall(r"\b\d+\b", s):
            nums.add(int(tok))
    return sorted(nums)


def discover_valid_source_mep_ids(
    client: paramiko.SSHClient,
    md: str,
    ma: str,
    timeout: int = 20,
    direction: Optional[str] = None,
) -> List[int]:
    """
    Ask the device (via TAB completion) what source MEP IDs are valid for the given md/ma.
    direction is accepted for API consistency (not used in TAB prefix; validation uses it).
    """
    # Enter a temporary DM session context and TAB-complete the mep-id value.
    tmp_session = "__DISC_DM_SRC__"
    prefix = f"source maintenance-domain {md} maintenance-association {ma} mep-id "
    channel = client.invoke_shell()
    channel.settimeout(timeout)
    _read_until_prompt(channel, prompt=None, timeout=timeout, quiet=1)
    channel.send("configure\n")
    _read_until_prompt_then_drain(channel, prompt=None, timeout=timeout)
    channel.send(f"services performance-monitoring cfm two-way-delay-measurement {tmp_session}\n")
    _read_until_prompt_then_drain(channel, prompt=None, timeout=timeout)
    channel.send(prefix)
    channel.send("\t")
    out = _read_until_prompt_then_drain(channel, prompt=None, timeout=timeout)
    # Teardown: remove only the temp session (do not rollback 0 - preserves user's candidate).
    channel.send("\n")
    _read_until_prompt_then_drain(channel, prompt=None, timeout=timeout)
    for _ in range(4):
        channel.send("exit\n")
        _read_until_prompt_then_drain(channel, prompt=None, timeout=timeout)
    channel.send("no services performance-monitoring cfm two-way-delay-measurement __DISC_DM_SRC__\n")
    _read_until_prompt_then_drain(channel, prompt=None, timeout=timeout)
    channel.send("exit\n")
    _read_until_prompt_then_drain(channel, prompt=None, timeout=timeout)
    channel.close()
    return _extract_numeric_completions(out)


def discover_valid_target_mep_ids_dm(
    client: paramiko.SSHClient, timeout: int = 20
) -> List[int]:
    """
    Ask the device (via TAB completion) what DM target MEP IDs are available in context.
    """
    tmp_session = "__DISC_DM_TGT__"
    prefix = "target mep-id "
    channel = client.invoke_shell()
    channel.settimeout(timeout)
    _read_until_prompt(channel, prompt=None, timeout=timeout, quiet=1)
    channel.send("configure\n")
    _read_until_prompt_then_drain(channel, prompt=None, timeout=timeout)
    channel.send(f"services performance-monitoring cfm two-way-delay-measurement {tmp_session}\n")
    _read_until_prompt_then_drain(channel, prompt=None, timeout=timeout)
    channel.send(prefix)
    channel.send("\t")
    out = _read_until_prompt_then_drain(channel, prompt=None, timeout=timeout)
    # Teardown: remove only the temp session (do not rollback 0 - preserves user's candidate).
    channel.send("\n")
    _read_until_prompt_then_drain(channel, prompt=None, timeout=timeout)
    for _ in range(4):
        channel.send("exit\n")
        _read_until_prompt_then_drain(channel, prompt=None, timeout=timeout)
    channel.send("no services performance-monitoring cfm two-way-delay-measurement __DISC_DM_TGT__\n")
    _read_until_prompt_then_drain(channel, prompt=None, timeout=timeout)
    channel.send("exit\n")
    _read_until_prompt_then_drain(channel, prompt=None, timeout=timeout)
    channel.close()
    return _extract_numeric_completions(out)


def validate_dm_source_mep_id(
    client: paramiko.SSHClient,
    md: str,
    ma: str,
    mep_id: str,
    timeout: int = 20,
    direction: Optional[str] = None,
) -> Tuple[bool, str]:
    """
    Best-effort validation that the PM CLI accepts the given source mep-id
    (helps avoid using remote-mep IDs as local source MEP).
    direction (down/up) is applied when device has two CFM sessions per MEP.
    """
    tmp_session = "__DISC_DM_SRC_VALIDATE__"
    cmd = _source_line(md, ma, mep_id, direction)
    channel = client.invoke_shell()
    channel.settimeout(timeout)
    _read_until_prompt(channel, prompt=None, timeout=timeout, quiet=1)
    channel.send("configure\n")
    _read_until_prompt_then_drain(channel, prompt=None, timeout=timeout)
    channel.send(f"services performance-monitoring cfm two-way-delay-measurement {tmp_session}\n")
    _read_until_prompt_then_drain(channel, prompt=None, timeout=timeout)
    channel.send(cmd + "\n")
    out = _read_until_prompt_then_drain(channel, prompt=None, timeout=timeout)
    # Teardown: remove only the temp session (do not rollback 0 - preserves user's candidate).
    for _ in range(4):
        channel.send("exit\n")
        _read_until_prompt_then_drain(channel, prompt=None, timeout=timeout)
    channel.send("no services performance-monitoring cfm two-way-delay-measurement __DISC_DM_SRC_VALIDATE__\n")
    _read_until_prompt_then_drain(channel, prompt=None, timeout=timeout)
    channel.send("exit\n")
    _read_until_prompt_then_drain(channel, prompt=None, timeout=timeout)
    channel.close()
    out = redact_text(out)
    err, errs = has_cli_error(out)
    return (not err, "\n".join(errs) if err else "Accepted by CLI.")


def discover_local_mep_ids_from_ethernet_oam(
    client: paramiko.SSHClient, md: str, ma: str, timeout: int = 30
) -> List[int]:
    """
    Read local (non-remote) MEP IDs for a specific MD/MA from:
      show config services ethernet-oam
    This is what you asked for: "show config services ethernet-oam" and derive parameters from there.
    """
    show_cmds = [
        "show config services ethernet-oam | display-set",
        "show configuration services ethernet-oam | display-set",
        "show config services ethernet-oam",
        "show configuration services ethernet-oam",
    ]
    used, output = _first_successful_show(client, show_cmds, timeout=timeout)
    if not used:
        return []

    # Be tolerant to plural/leaf-name variants.
    md_re = re.compile(r"\bmaintenance[-_]domain(?:s)?(?:[-_]name)?\s+(\S+)", flags=re.IGNORECASE)
    ma_re = re.compile(r"\bmaintenance[-_]association(?:s)?(?:[-_]name)?\s+(\S+)", flags=re.IGNORECASE)
    mep_id_re = re.compile(r"\bmep[-_]id\s+(\d+)\b", flags=re.IGNORECASE)
    mep_re = re.compile(r"\bmep\s+(\d+)\b", flags=re.IGNORECASE)
    remote_mep_re = re.compile(r"\bremote[-_]mep", flags=re.IGNORECASE)

    current_md: Optional[str] = None
    current_ma: Optional[str] = None
    meps: Set[int] = set()
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
        if line_md != md or line_ma != ma:
            continue

        # Skip any line that is clearly about remote MEPs.
        if remote_mep_re.search(line):
            continue

        for m in mep_id_re.finditer(line):
            meps.add(int(m.group(1)))
        for m in mep_re.finditer(line):
            meps.add(int(m.group(1)))

    return sorted(meps)


def _source_line(md: str, ma: str, mep_id: str, direction: Optional[str] = None) -> str:
    """Build PM source CLI line; append 'direction down|up' when device has two CFM sessions per MEP."""
    line = f"source maintenance-domain {md} maintenance-association {ma} mep-id {mep_id}"
    if direction in ("down", "up"):
        line += f" direction {direction}"
    return line


def build_commands(
    session: str,
    profile: str,
    md: str,
    ma: str,
    mep_id: str,
    target: str,
    description: str,
    direction: Optional[str] = None,
) -> List[str]:
    source = _source_line(md, ma, mep_id, direction)
    return [
        f"services performance-monitoring cfm two-way-delay-measurement {session}",
        f"profile {profile}",
        "admin-state enabled",
        f"description {description}",
        source,
        f"target {target}",
        "exit",
    ]


def build_slm_session_commands(
    session: str,
    profile: str,
    md: str,
    ma: str,
    mep_id: str,
    target: str,
    description: str,
    direction: Optional[str] = None,
) -> List[str]:
    source = _source_line(md, ma, mep_id, direction)
    return [
        f"services performance-monitoring cfm two-way-synthetic-loss-measurement {session}",
        f"profile {profile}",
        "admin-state enabled",
        f"description {description}",
        source,
        f"target {target}",
        "exit",
    ]


def _run_commit_check_sequence(
    client: paramiko.SSHClient, name: str, commands: List[str], timeout: int = 25
) -> StepResult:
    """
    Run commands in one shell session and evaluate PASS/FAIL based on CLI error detection.
    Intended for SW-235372 CLI coverage checks. Uses a shorter timeout (25s) per command
    so the script does not appear stuck on slow or chatty devices.
    """
    cmd_outputs = run_shell_sequence_detailed(client, commands, timeout=timeout)
    failed_cmd = None
    failed_errs: List[str] = []
    for cmd, output in cmd_outputs:
        err, errs = has_cli_error(output)
        if err:
            failed_cmd = cmd
            failed_errs = errs
            break
    if failed_cmd:
        return StepResult(
            name=name,
            ok=False,
            details=f"Failed command: {failed_cmd}\n" + "\n".join(failed_errs),
        )
    return StepResult(name=name, ok=True, details="Commands accepted; commit check passed.")


def _run_show_command_test(
    client: paramiko.SSHClient, name: str, command: str,
    expected_strings: List[str], timeout: int = 30
) -> StepResult:
    """Run a show command and verify expected strings appear in the output."""
    output = run_shell_with_prompt(client, command, timeout=timeout)
    err, errs = has_cli_error(output)
    if err:
        return StepResult(name=name, ok=False, details=f"CLI error: {'; '.join(errs)}", raw_output=output)
    missing = [s for s in expected_strings if s not in output]
    if missing:
        return StepResult(name=name, ok=False, details=f"Missing in output: {missing}", raw_output=output)
    return StepResult(name=name, ok=True, details="All expected strings found.", raw_output=output)


def _run_show_command_test_fallback(
    client: paramiko.SSHClient, name: str, commands: List[str],
    expected_strings: List[str], timeout: int = 30
) -> StepResult:
    """Try multiple show command variants; PASS if any succeeds."""
    last_result: Optional[StepResult] = None
    for cmd in commands:
        result = _run_show_command_test(client, name, cmd, expected_strings, timeout=timeout)
        if result.ok:
            return result
        last_result = result
    return last_result or StepResult(name=name, ok=False, details="No show command variants to try.")


def _check_system_event(
    client: paramiko.SSHClient, event_name: str, timeout: int = 30
) -> Tuple[bool, str, str]:
    """Check syslog/events for a specific event name. Returns (found, details, raw_output).
    NOTE: This is the legacy fallback. Prefer _open_logging_channel / _read_logging_channel
    which use 'set logging terminal' for real-time event capture."""
    show_cmds = [
        f"show system event-log | match {event_name}",
        f"show system events | match {event_name}",
        f"show log messages | match {event_name}",
    ]
    all_output = ""
    for cmd in show_cmds:
        out = run_shell_with_prompt(client, cmd, timeout=timeout)
        all_output += f"\n--- {cmd} ---\n{out}"
        err, _ = has_cli_error(out)
        if not err and event_name in out:
            return True, f"Found '{event_name}' via '{cmd}'.", all_output
    return False, f"'{event_name}' not found in any event/log command.", all_output


def _open_logging_channel(
    client: paramiko.SSHClient, timeout: int = 30
) -> paramiko.Channel:
    """Open a dedicated SSH shell channel with 'set logging terminal' enabled.
    Events/syslog messages will stream to this channel in real-time.
    Caller must close the channel when done."""
    channel = client.invoke_shell()
    channel.settimeout(timeout)
    # Wait for initial prompt
    _read_until_prompt(channel, prompt=None, timeout=timeout, quiet=1)
    # Enable terminal logging
    channel.send("set logging terminal\n")
    # Wait for command to be accepted and drain any immediate output
    time.sleep(1)
    _read_until_quiet(channel, timeout=3, quiet=1)
    return channel


def _read_logging_channel(
    channel: paramiko.Channel, event_name: str, timeout: int = 30
) -> Tuple[bool, str, str]:
    """Read accumulated output from a logging channel and search for an event.
    Returns (found, details, raw_output)."""
    output = _read_until_quiet(channel, timeout=timeout, quiet=2)
    clean = ANSI_ESCAPE.sub("", output)
    if event_name in clean:
        return True, f"Found '{event_name}' via 'set logging terminal'.", clean
    return False, f"'{event_name}' not found in logging terminal output.", clean


def build_commit_sequence(commit_cmd: str) -> List[str]:
    # Keep for legacy callers (unused for base now).
    return ["exit", "exit", commit_cmd, "exit"]


def run_commit_check(client: paramiko.SSHClient) -> str:
    output = run_shell_with_prompt(client, "commit check", timeout=60)
    if "Unknown command" in output:
        return output
    return output


def run_commit(client: paramiko.SSHClient) -> str:
    output = run_shell_with_prompt(client, "commit", timeout=120)
    if "Unknown command" in output:
        return output
    return output


def run_rollback(client: paramiko.SSHClient) -> str:
    return run_shell_with_prompt(client, "rollback 0", timeout=30)


def extract_conflicting_session_name(error_messages: List[str]) -> Optional[str]:
    """
    Extract the existing session name from "in use with session <NAME>" error.
    Returns the session name if found, None otherwise.
    """
    for err in error_messages:
        if "in use with session" in err.lower():
            # Example: "ERROR: Source MD MD-CUST MA MA-CUST LMEP 2 in use with session DM_CLI_TAB."
            match = re.search(r"in use with session\s+(\S+)", err, re.IGNORECASE)
            if match:
                session_name = match.group(1).rstrip(".")
                return session_name
    return None


def delete_existing_pm_session(
    client: paramiko.SSHClient, session_name: str, timeout: int = 60
) -> Tuple[bool, str]:
    """
    Delete an existing PM session (DM or SLM) by name.
    Returns (success, details_message).
    """
    # Try both DM and SLM deletion (one will work, one will fail with "Unknown word")
    commands = [
        "configure",
        f"no services performance-monitoring cfm two-way-delay-measurement {session_name}",
        f"no services performance-monitoring cfm two-way-synthetic-loss-measurement {session_name}",
        "commit",
        "exit",
    ]
    try:
        cmd_outputs = run_shell_sequence_detailed_safe(client, commands, timeout=timeout)
    except Exception as exc:
        return False, f"Exception during deletion: {exc}"
    
    commit_success = False
    for cmd, output in cmd_outputs:
        if cmd == "commit":
            err, errs = has_cli_error(output)
            if not err:
                commit_success = True
            else:
                return False, f"Commit failed during deletion: {'; '.join(errs)}"
        # Ignore "Unknown word" errors for the delete commands (expected for one of DM/SLM)
        elif cmd.startswith("no services"):
            err, errs = has_cli_error(output)
            if err and not any("Unknown word" in e for e in errs):
                return False, f"Failed to delete session: {'; '.join(errs)}"
    
    if commit_success:
        return True, f"Deleted existing session '{session_name}' successfully."
    return False, "Commit command not found in output."


def cleanup_config(
    host: str,
    user: str,
    password: str,
    session: str,
    profile: str,
    slm_session: Optional[str] = None,
    slm_profile: Optional[str] = None,
) -> Tuple[bool, str]:
    # Use a fresh connection for cleanup (more reliable). Do NOT use rollback 0:
    # that would discard the user's entire candidate config (e.g. ethernet-oam CFM).
    # Only remove the PM sessions created by this script (not profiles), then commit.
    # Profiles are left in place so other sessions (e.g. test-func) that reference
    # the same profile name do not break on commit.
    commands = [
        "configure",
        f"no services performance-monitoring cfm two-way-delay-measurement {session}",
        *(  # noqa: C400
            [f"no services performance-monitoring cfm two-way-synthetic-loss-measurement {slm_session}"]
            if slm_session
            else []
        ),
        "commit",
        "exit",
    ]
    cleanup_client = create_ssh_client(host=host, user=user, password=password, timeout=30)
    try:
        try:
            # Commit may take longer than regular CLI commands.
            cmd_outputs = run_shell_sequence_detailed_safe(cleanup_client, commands, timeout=120)
        except OSError as exc:
            if "Socket is closed" not in str(exc):
                return False, str(exc)
            # reconnect once
            try:
                cleanup_client.close()
            except Exception:
                pass
            cleanup_client = create_ssh_client(host=host, user=user, password=password, timeout=30)
            cmd_outputs = run_shell_sequence_detailed_safe(cleanup_client, commands, timeout=120)
    finally:
        cleanup_client.close()
    for cmd, output in cmd_outputs:
        err, errs = has_cli_error(output)
        if err:
            # If the error is "Unknown word" for a session/profile deletion,
            # it means it doesn't exist (likely because commit failed earlier).
            # Treat this as a warning, not a hard failure.
            if any("Unknown word" in e for e in errs) and cmd.startswith("no services"):
                continue  # Session/profile already doesn't exist, continue cleanup
            # For other errors, fail cleanup
            return (
                False,
                f"Failed cleanup command: {cmd}\n"
                + "\n".join(errs)
                + "\n\n--- Raw device output ---\n"
                + output.strip(),
            )
    return True, "Cleanup committed."


def _prompt_if_missing(value: Optional[str], label: str, secret: bool = False) -> str:
    if value:
        return value
    if secret:
        return getpass.getpass(label).strip()
    return input(label).strip()


def _prompt_yes_no(prompt: str, default: bool = True) -> bool:
    suffix = " [Y/n]: " if default else " [y/N]: "
    while True:
        raw = input(prompt + suffix).strip().lower()
        if raw == "":
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print("Please answer y/n.")


def _prompt_numeric(value: Optional[str], label: str) -> str:
    while True:
        raw = _prompt_if_missing(value, label, secret=False)
        if raw.isdigit():
            return raw
        print("Please enter a numeric value.")


def _prompt_nonempty(label: str) -> str:
    while True:
        raw = input(label).strip()
        if raw:
            return raw
        print("Value cannot be empty.")


def _prompt_target(label: str) -> str:
    """
    Accept:
    - "<N>" => "mep-id <N>"
    - "mep-id <N>"
    - "mac-address <MAC>"
    """
    while True:
        raw = input(label).strip()
        if not raw:
            print("Value cannot be empty.")
            continue
        if raw.isdigit():
            return f"mep-id {raw}"
        low = raw.lower()
        if low.startswith("mep-id"):
            parts = raw.split()
            if len(parts) == 2 and parts[1].isdigit():
                return f"mep-id {parts[1]}"
            print("Enter 'mep-id <N>' (e.g., 'mep-id 2') or just '<N>'.")
            continue
        if low.startswith("mac-address"):
            parts = raw.split()
            if len(parts) == 2 and parts[1]:
                return f"mac-address {parts[1]}"
            print("Enter 'mac-address <MAC>' (e.g., 'mac-address aa:bb:cc:dd:ee:ff').")
            continue
        print("Enter 'mep-id <N>' or 'mac-address <MAC>' (or just '<N>').")


def main() -> int:
    parser = argparse.ArgumentParser(description="Y.1731 DM + SLM CLI + TAB validation")
    parser.add_argument("--host", help="Device hostname or IP")
    parser.add_argument("--user", default="dnroot")
    parser.add_argument("--password", default="dnroot")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--session", default="DM_CLI_TAB")
    parser.add_argument("--profile", default="DM_PROF_CLI")
    # SW-235927: SLM session CLI coverage
    parser.add_argument("--slm-session", default="SLM_CLI_TAB")
    parser.add_argument("--slm-profile", default="SLM_PROF_CLI")
    parser.add_argument("--slm-target", default=None, help="Override SLM target (e.g., 'mep-id 2')")
    parser.add_argument("--slm-description", default="cli_tab_test_slm")
    parser.add_argument("--slm-pcp", type=int, default=5)
    parser.add_argument(
        "--auto-from-cfm",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Auto-discover MD/MA/MEP/target from existing ethernet-oam CFM config (default: true).",
    )
    parser.add_argument(
        "--all-meps",
        action="store_true",
        help="Discover all local MEPs and run the full test suite (DM+SLM) for each (e.g. MEP 2 up, then MEP 4 down). Implies --auto-from-cfm.",
    )
    parser.add_argument("--md", default=None, help="Override maintenance-domain name (otherwise auto-discovered)")
    parser.add_argument("--ma", default=None, help="Override maintenance-association name (otherwise auto-discovered)")
    parser.add_argument("--mep-id", default=None, help="Override local MEP ID (otherwise auto-discovered)")
    parser.add_argument(
        "--mep-direction",
        choices=["down", "up"],
        default=None,
        help="CFM MEP direction (down or up). Auto-discovered from config if not set. Use when device has two CFM sessions (one per direction).",
    )
    parser.add_argument("--target", default=None, help="Override DM target (e.g., 'mep-id 2')")
    parser.add_argument("--description", default="cli_tab_test")
    # Defaults intentionally oversized to trigger validation if length limits exist.
    parser.add_argument("--long-name", default="DM_" + ("X" * 220))
    parser.add_argument("--long-desc", default="desc_" + ("x" * 512))
    parser.add_argument("--bad-md", default="MD_BAD")
    parser.add_argument("--bad-ma", default="MA_BAD")
    parser.add_argument(
        "--show-cli-output",
        action="store_true",
        help="Print raw CLI output from device",
    )
    parser.add_argument(
        "--output-file",
        help="Write raw CLI output to a file",
    )
    parser.add_argument(
        "--show-details",
        action="store_true",
        help="Print per-step details (errors/notes). Default is PASS/FAIL only.",
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
    parser.add_argument(
        "--cleanup",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Remove created session/profile at end (prompted if omitted)",
    )
    parser.add_argument(
        "--skip-show-proactive",
        action="store_true",
        help="Skip show proactive DM/SLM tests (use if device returns unexpected output).",
    )
    parser.add_argument(
        "--skip-on-demand-stop",
        action="store_true",
        help="Skip request ethernet-oam cfm on-demand stop test (use if device returns unexpected output).",
    )
    parser.add_argument(
        "--wait-for-results",
        type=int,
        default=30,
        help="Seconds to wait for proactive session results before checking (default: 30)",
    )
    parser.add_argument(
        "--skip-event-test",
        action="store_true",
        help="Skip the system event CFM_PROACTIVE_TEST_FAILURE test (requires waiting for probes)",
    )
    parser.add_argument(
        "--low-threshold-wait",
        type=int,
        default=20,
        help="Seconds to wait for low-threshold violation event (default: 20)",
    )
    args = parser.parse_args()

    args.host = _prompt_if_missing(args.host, "Device hostname or IP: ")
    args.user = _prompt_if_missing(args.user, "Username [dnroot]: ") or "dnroot"
    if args.password == "dnroot":
        args.password = _prompt_if_missing(args.password, "Password [dnroot]: ", secret=True) or "dnroot"
    else:
        args.password = _prompt_if_missing(args.password, "Password: ", secret=True)

    if args.cleanup is None:
        args.cleanup = _prompt_yes_no("Cleanup (remove created config) at end?", default=True)

    # If user asked for details, they typically want to see progress too.
    if args.show_details and not args.show_progress:
        args.show_progress = True

    results: List[StepResult] = []
    raw_outputs: List[str] = []
    client = create_ssh_client(args.host, args.user, args.password, args.timeout)
    try:
        abort = False
        cleanup_done = False

        def _progress(name: str) -> None:
            if args.show_progress:
                print(f"RUNNING: {name}")

        # Optional: discover all local MEPs and run full suite per MEP (--all-meps).
        mep_list: Optional[List[Tuple[str, str, str, Optional[str], Optional[str]]]] = None
        if getattr(args, "all_meps", False):
            args.auto_from_cfm = True
            _progress("discover_all_local_meps")
            ok_all, detail_all, list_all = discover_all_local_meps(client, timeout=30)
            results.append(
                StepResult(
                    name="discover_all_local_meps",
                    ok=ok_all,
                    details=detail_all,
                )
            )
            if ok_all and list_all:
                mep_list = list_all
                # Display discovered MEPs to user
                print(f"\n{'='*70}")
                print(f"DISCOVERED {len(list_all)} LOCAL MEP(S):")
                print(f"{'='*70}")
                for idx, (md, ma, mep_id, direction, target) in enumerate(list_all, 1):
                    print(f"  {idx}. MEP {mep_id}")
                    print(f"     MD/MA: {md}/{ma}")
                    print(f"     Direction: {direction or 'N/A'}")
                    print(f"     Target: {target or 'N/A'}")
                print(f"{'='*70}\n")

        # Auto-discover MD/MA/MEP/target from existing ethernet-oam CFM config (single MEP).
        if mep_list is None and args.auto_from_cfm:
            _progress("discover_cfm_context")
            ok, detail, md, ma, mep_id, target, direction = discover_cfm_context(client, timeout=30)
            results.append(StepResult(name="discover_cfm_context", ok=ok, details=detail))
            if ok:
                if args.md is None:
                    args.md = md
                if args.ma is None:
                    args.ma = ma
                if args.mep_id is None:
                    args.mep_id = mep_id
                if args.target is None and target is not None:
                    args.target = target
                if args.slm_target is None and target is not None:
                    args.slm_target = target
                if getattr(args, "mep_direction", None) is None and direction is not None:
                    args.mep_direction = direction

                # If discovery found MD/MA but couldn't confidently determine MEP/targets,
                # prompt for the missing pieces instead of silently falling back.
                #
                # Also validate that a discovered mep-id is actually accepted by the PM "source ... mep-id <X>"
                # completion list; some devices show remote-meps in the CFM tree and the naive parser can pick those.
                if args.md and args.ma and args.mep_id:
                    candidates = discover_valid_source_mep_ids(
                        client, args.md, args.ma, timeout=20,
                        direction=getattr(args, "mep_direction", None),
                    )
                    if candidates and int(args.mep_id) not in candidates:
                        results.append(
                            StepResult(
                                name="discover_source_mep_id",
                                ok=True,
                                details=(
                                    f"Discovered mep-id {args.mep_id} not in completion candidates {candidates}; "
                                    f"using {candidates[0]} instead."
                                ),
                            )
                        )
                        args.mep_id = str(candidates[0])
                    elif not candidates:
                        mep_dir = getattr(args, "mep_direction", None)
                        ok_src, why = validate_dm_source_mep_id(
                            client, args.md, args.ma, args.mep_id, timeout=20,
                            direction=mep_dir,
                        )
                        if not ok_src and mep_dir:
                            # Some devices don't support "direction" in PM source; retry without it.
                            ok_src_no_dir, _ = validate_dm_source_mep_id(
                                client, args.md, args.ma, args.mep_id, timeout=20,
                                direction=None,
                            )
                            if ok_src_no_dir:
                                ok_src, why = True, "Accepted without direction (device may not support direction in PM source)."
                                args.mep_direction = None
                        if ok_src:
                            results.append(
                                StepResult(
                                    name="discover_source_mep_id",
                                    ok=True,
                                    details=why,
                                )
                            )
                        elif not ok_src:
                            # Try to select a local MEP ID from ethernet-oam CFM config.
                            cfg_meps = discover_local_mep_ids_from_ethernet_oam(client, args.md, args.ma, timeout=30)
                            picked: Optional[int] = None
                            for candidate in cfg_meps:
                                ok_cand, _ = validate_dm_source_mep_id(
                                    client, args.md, args.ma, str(candidate), timeout=20,
                                    direction=getattr(args, "mep_direction", None),
                                )
                                if not ok_cand and getattr(args, "mep_direction", None):
                                    ok_cand, _ = validate_dm_source_mep_id(
                                        client, args.md, args.ma, str(candidate), timeout=20,
                                        direction=None,
                                    )
                                    if ok_cand:
                                        args.mep_direction = None
                                if ok_cand:
                                    picked = candidate
                                    break
                            if picked is not None:
                                results.append(
                                    StepResult(
                                        name="discover_source_mep_id",
                                        ok=True,
                                        details=(
                                            f"Discovered mep-id {args.mep_id} rejected; selected local mep-id {picked} "
                                            f"from 'show config services ethernet-oam' candidates {cfg_meps}."
                                        ),
                                    )
                                )
                                args.mep_id = str(picked)
                            else:
                                print("Discovered source mep-id is not accepted by CLI; prompting.")
                                results.append(
                                    StepResult(
                                        name="discover_source_mep_id",
                                        ok=False,
                                        details=f"Discovered mep-id {args.mep_id} rejected ({why}).",
                                    )
                                )
                                args.mep_id = _prompt_numeric(None, "Local MEP ID (numeric): ")
                if args.mep_id is None:
                    # Try to discover valid MEP IDs from CLI completion first.
                    candidates = discover_valid_source_mep_ids(
                        client, args.md, args.ma, timeout=20,
                        direction=getattr(args, "mep_direction", None),
                    )
                    if candidates:
                        args.mep_id = str(candidates[0])
                        results.append(
                            StepResult(
                                name="discover_source_mep_id",
                                ok=True,
                                details=f"Selected source mep-id {args.mep_id} from CLI completion candidates {candidates}.",
                            )
                        )
                    else:
                        # Fall back to ethernet-oam config parsing (what you asked for).
                        cfg_meps = discover_local_mep_ids_from_ethernet_oam(client, args.md, args.ma, timeout=30)
                        picked: Optional[int] = None
                        for candidate in cfg_meps:
                            ok_cand, _ = validate_dm_source_mep_id(
                                client, args.md, args.ma, str(candidate), timeout=20,
                                direction=getattr(args, "mep_direction", None),
                            )
                            if ok_cand:
                                picked = candidate
                                break
                        if picked is not None:
                            args.mep_id = str(picked)
                            results.append(
                                StepResult(
                                    name="discover_source_mep_id",
                                    ok=True,
                                    details=(
                                        f"Selected source mep-id {args.mep_id} from 'show config services ethernet-oam' "
                                        f"candidates {cfg_meps}."
                                    ),
                                )
                            )
                        else:
                            print("CFM discovery did not find a local MEP ID; prompting.")
                            if args.show_details and cfg_meps:
                                print(f"NOTE: ethernet-oam candidates found but none accepted by CLI: {cfg_meps}")
                            args.mep_id = _prompt_numeric(None, "Local MEP ID (numeric): ")
                if args.target is None:
                    candidates = discover_valid_target_mep_ids_dm(client, timeout=20)
                    if candidates:
                        args.target = f"mep-id {candidates[0]}"
                        results.append(
                            StepResult(
                                name="discover_dm_target_mep_id",
                                ok=True,
                                details=f"Selected DM target {args.target} from CLI completion candidates {candidates}.",
                            )
                        )
                    else:
                        print("CFM discovery did not find a DM target; prompting.")
                        args.target = _prompt_target("DM target (e.g., 'mep-id 2' / 'mac-address aa:bb:..' / '2'): ")
                if args.slm_target is None:
                    print("CFM discovery did not find an SLM target; prompting.")
                    args.slm_target = _prompt_target("SLM target (e.g., 'mep-id 2' / 'mac-address aa:bb:..' / '2'): ")
            else:
                # No ethernet-oam CFM config found/parsable; prompt for required params (or use overrides).
                if args.show_details:
                    print(detail)
                print("CFM discovery failed; prompting for MD/MA/MEP/targets.")
                if args.md is None:
                    args.md = _prompt_nonempty("Maintenance-domain (MD) name: ")
                if args.ma is None:
                    args.ma = _prompt_nonempty("Maintenance-association (MA) name: ")
                if args.mep_id is None:
                    args.mep_id = _prompt_numeric(None, "Local MEP ID (numeric): ")
                if args.target is None:
                    args.target = _prompt_target("DM target (e.g., 'mep-id 2' / 'mac-address aa:bb:..' / '2'): ")
                if args.slm_target is None:
                    args.slm_target = _prompt_target("SLM target (e.g., 'mep-id 2' / 'mac-address aa:bb:..' / '2'): ")
                results.append(
                    StepResult(
                        name="manual_cfm_context",
                        ok=True,
                        details="Used manually provided MD/MA/MEP/target because ethernet-oam CFM config was not found.",
                    )
                )

        # Final fallbacks (if auto-discovery disabled or target not found).
        if args.md is None:
            args.md = "MD-CUST"
        if args.ma is None:
            args.ma = "MA-CUST"
        if args.mep_id is None:
            args.mep_id = "1"
        if args.target is None:
            args.target = "mep-id 2"
        if args.slm_target is None:
            args.slm_target = "mep-id 2"

        # SW-235372: profile variant tests are system-wide (not per-MEP), so run them once.
        if not abort:
            _progress("sw235372_dm_profile_variants")
            dm_prof_372 = f"{args.profile}_SW235372"
            dm_prof_base = [
                "configure",
                f"services performance-monitoring profiles cfm two-way-delay-measurement {dm_prof_372}",
                "inform-test-results enabled",
                "thresholds delay-rtt-min 100",
                "thresholds delay-rtt-avg 1000",
                "thresholds delay-rtt-max 2000",
                "thresholds jitter-rtt-avg 500",
                "thresholds jitter-rtt-max 1000",
                "thresholds success-rate 90",
            ]
            results.append(
                _run_commit_check_sequence(
                    client,
                    "sw235372_dm_profile_probes",
                    dm_prof_base
                    + ["test-duration probes probe-count 5 probe-interval 1 repeat-interval 10", "commit check"]
                    + teardown_dm_profile_commands(dm_prof_372),
                )
            )
            results.append(
                _run_commit_check_sequence(
                    client,
                    "sw235372_dm_profile_time_frame",
                    dm_prof_base
                    + ["test-duration time-frame minutes 1 probe-interval 1 repeat-interval 120", "commit check"]
                    + teardown_dm_profile_commands(dm_prof_372),
                )
            )
            results.append(
                _run_commit_check_sequence(
                    client,
                    "sw235372_dm_profile_non_stop",
                    dm_prof_base
                    + ["test-duration non-stop probe-interval 1 computation-interval 10", "commit check"]
                    + teardown_dm_profile_commands(dm_prof_372),
                )
            )
            for probe_count in (1, 10):
                pname = f"{dm_prof_372}_probes_{probe_count}"
                pbase = [
                    "configure",
                    f"services performance-monitoring profiles cfm two-way-delay-measurement {pname}",
                    "inform-test-results enabled",
                    "thresholds delay-rtt-min 100",
                    "thresholds delay-rtt-avg 1000",
                    "thresholds delay-rtt-max 2000",
                    "thresholds jitter-rtt-avg 500",
                    "thresholds jitter-rtt-max 1000",
                    "thresholds success-rate 90",
                ]
                results.append(
                    _run_commit_check_sequence(
                        client,
                        f"sw235372_dm_profile_probes_count_{probe_count}",
                        pbase
                        + [f"test-duration probes probe-count {probe_count} probe-interval 1 repeat-interval 10", "commit check"]
                        + teardown_dm_profile_commands(pname),
                    )
                )
            for probe_int, repeat_int in [(10, 60), (1, 30)]:
                pname = f"{dm_prof_372}_probes_i{probe_int}_r{repeat_int}"
                pbase = [
                    "configure",
                    f"services performance-monitoring profiles cfm two-way-delay-measurement {pname}",
                    "inform-test-results enabled",
                    "thresholds delay-rtt-min 100",
                    "thresholds delay-rtt-avg 1000",
                    "thresholds delay-rtt-max 2000",
                    "thresholds jitter-rtt-avg 500",
                    "thresholds jitter-rtt-max 1000",
                    "thresholds success-rate 90",
                ]
                results.append(
                    _run_commit_check_sequence(
                        client,
                        f"sw235372_dm_profile_probes_interval_{probe_int}_{repeat_int}",
                        pbase
                        + [f"test-duration probes probe-count 5 probe-interval {probe_int} repeat-interval {repeat_int}", "commit check"]
                        + teardown_dm_profile_commands(pname),
                    )
                )
            for mins in (5,):
                pname = f"{dm_prof_372}_tf_{mins}m"
                pbase = [
                    "configure",
                    f"services performance-monitoring profiles cfm two-way-delay-measurement {pname}",
                    "inform-test-results enabled",
                    "thresholds delay-rtt-min 100",
                    "thresholds delay-rtt-avg 1000",
                    "thresholds delay-rtt-max 2000",
                    "thresholds jitter-rtt-avg 500",
                    "thresholds jitter-rtt-max 1000",
                    "thresholds success-rate 90",
                ]
                results.append(
                    _run_commit_check_sequence(
                        client,
                        f"sw235372_dm_profile_time_frame_{mins}min",
                        pbase
                        + [f"test-duration time-frame minutes {mins} probe-interval 1 repeat-interval {mins * 120}", "commit check"]
                        + teardown_dm_profile_commands(pname),
                    )
                )
            for comp_int in (5, 60):
                pname = f"{dm_prof_372}_ns_ci{comp_int}"
                pbase = [
                    "configure",
                    f"services performance-monitoring profiles cfm two-way-delay-measurement {pname}",
                    "inform-test-results enabled",
                    "thresholds delay-rtt-min 100",
                    "thresholds delay-rtt-avg 1000",
                    "thresholds delay-rtt-max 2000",
                    "thresholds jitter-rtt-avg 500",
                    "thresholds jitter-rtt-max 1000",
                    "thresholds success-rate 90",
                ]
                results.append(
                    _run_commit_check_sequence(
                        client,
                        f"sw235372_dm_profile_non_stop_ci_{comp_int}",
                        pbase
                        + [f"test-duration non-stop probe-interval 1 computation-interval {comp_int}", "commit check"]
                        + teardown_dm_profile_commands(pname),
                    )
                )
            for thresh_label, thresh_lines in [
                ("delay_min_only", ["thresholds delay-rtt-min 100"]),
                ("success_rate_only", ["thresholds success-rate 90"]),
                ("delay_min_and_success", ["thresholds delay-rtt-min 100", "thresholds success-rate 90"]),
                ("delay_avg_only", ["thresholds delay-rtt-avg 1000"]),
                ("jitter_avg_only", ["thresholds jitter-rtt-avg 500"]),
                ("all_six", [
                    "thresholds delay-rtt-min 100",
                    "thresholds delay-rtt-avg 1000",
                    "thresholds delay-rtt-max 2000",
                    "thresholds jitter-rtt-avg 500",
                    "thresholds jitter-rtt-max 1000",
                    "thresholds success-rate 90",
                ]),
            ]:
                pname = f"{dm_prof_372}_th_{thresh_label}"
                results.append(
                    _run_commit_check_sequence(
                        client,
                        f"sw235372_dm_profile_threshold_{thresh_label}",
                        ["configure", f"services performance-monitoring profiles cfm two-way-delay-measurement {pname}", "inform-test-results enabled", "test-duration probes probe-count 5 probe-interval 1 repeat-interval 10"]
                        + thresh_lines
                        + ["commit check"]
                        + teardown_dm_profile_commands(pname),
                    )
                )
            for val_label, thresh_cmd in [
                ("delay_min_50", "thresholds delay-rtt-min 50"),
                ("delay_min_200", "thresholds delay-rtt-min 200"),
                ("success_rate_50", "thresholds success-rate 50"),
                ("success_rate_99", "thresholds success-rate 99"),
            ]:
                pname = f"{dm_prof_372}_tv_{val_label}"
                results.append(
                    _run_commit_check_sequence(
                        client,
                        f"sw235372_dm_profile_threshold_value_{val_label}",
                        ["configure", f"services performance-monitoring profiles cfm two-way-delay-measurement {pname}", "inform-test-results enabled", "test-duration probes probe-count 5 probe-interval 1 repeat-interval 10", thresh_cmd, "commit check"]
                        + teardown_dm_profile_commands(pname),
                    )
                )
            pname_inform = f"{dm_prof_372}_inform_disabled"
            results.append(
                _run_commit_check_sequence(
                    client,
                    "sw235372_dm_profile_inform_disabled",
                    ["configure", f"services performance-monitoring profiles cfm two-way-delay-measurement {pname_inform}", "inform-test-results disabled", "test-duration probes probe-count 5 probe-interval 1 repeat-interval 10", "thresholds delay-rtt-min 100", "commit check"]
                    + teardown_dm_profile_commands(pname_inform),
                )
            )

            # SLM profile duration variants + thresholds + PCP
            _progress("sw235372_slm_profile_variants")
            slm_prof_372 = f"{args.slm_profile}_SW235372"
            slm_prof_base = [
                "configure",
                f"services performance-monitoring profiles cfm two-way-synthetic-loss-measurement {slm_prof_372}",
                f"pcp {args.slm_pcp}",
                "inform-test-results enabled",
                "thresholds near-end-loss 1",
                "thresholds far-end-loss 1",
            ]
            results.append(
                _run_commit_check_sequence(
                    client,
                    "sw235372_slm_profile_probes",
                    slm_prof_base
                    + ["test-duration probes probe-count 5 probe-interval 1 repeat-interval 10", "commit check"]
                    + teardown_slm_profile_commands(slm_prof_372),
                )
            )
            results.append(
                _run_commit_check_sequence(
                    client,
                    "sw235372_slm_profile_time_frame",
                    slm_prof_base
                    + ["test-duration time-frame minutes 1 probe-interval 1 repeat-interval 120", "commit check"]
                    + teardown_slm_profile_commands(slm_prof_372),
                )
            )
            results.append(
                _run_commit_check_sequence(
                    client,
                    "sw235372_slm_profile_non_stop",
                    slm_prof_base
                    + ["test-duration non-stop probe-interval 1 computation-interval 10", "commit check"]
                    + teardown_slm_profile_commands(slm_prof_372),
                )
            )
            for probe_count in (1, 10):
                sname = f"{slm_prof_372}_probes_{probe_count}"
                sbase = [
                    "configure",
                    f"services performance-monitoring profiles cfm two-way-synthetic-loss-measurement {sname}",
                    f"pcp {args.slm_pcp}",
                    "inform-test-results enabled",
                    "thresholds near-end-loss 1",
                    "thresholds far-end-loss 1",
                ]
                results.append(
                    _run_commit_check_sequence(
                        client,
                        f"sw235372_slm_profile_probes_count_{probe_count}",
                        sbase
                        + [f"test-duration probes probe-count {probe_count} probe-interval 1 repeat-interval 10", "commit check"]
                        + teardown_slm_profile_commands(sname),
                    )
                )
            for mins in (5,):
                sname = f"{slm_prof_372}_tf_{mins}m"
                sbase = [
                    "configure",
                    f"services performance-monitoring profiles cfm two-way-synthetic-loss-measurement {sname}",
                    f"pcp {args.slm_pcp}",
                    "inform-test-results enabled",
                    "thresholds near-end-loss 1",
                    "thresholds far-end-loss 1",
                ]
                results.append(
                    _run_commit_check_sequence(
                        client,
                        f"sw235372_slm_profile_time_frame_{mins}min",
                        sbase
                        + [f"test-duration time-frame minutes {mins} probe-interval 1 repeat-interval {mins * 120}", "commit check"]
                        + teardown_slm_profile_commands(sname),
                    )
                )
            for comp_int in (5, 60):
                sname = f"{slm_prof_372}_ns_ci{comp_int}"
                sbase = [
                    "configure",
                    f"services performance-monitoring profiles cfm two-way-synthetic-loss-measurement {sname}",
                    f"pcp {args.slm_pcp}",
                    "inform-test-results enabled",
                    "thresholds near-end-loss 1",
                    "thresholds far-end-loss 1",
                ]
                results.append(
                    _run_commit_check_sequence(
                        client,
                        f"sw235372_slm_profile_non_stop_ci_{comp_int}",
                        sbase
                        + [f"test-duration non-stop probe-interval 1 computation-interval {comp_int}", "commit check"]
                        + teardown_slm_profile_commands(sname),
                    )
                )
            for thresh_label, thresh_lines in [
                ("near_only", ["thresholds near-end-loss 1"]),
                ("far_only", ["thresholds far-end-loss 1"]),
                ("both_1", ["thresholds near-end-loss 1", "thresholds far-end-loss 1"]),
                ("near_0_far_0", ["thresholds near-end-loss 0", "thresholds far-end-loss 0"]),
                ("near_5_far_5", ["thresholds near-end-loss 5", "thresholds far-end-loss 5"]),
            ]:
                sname = f"{slm_prof_372}_th_{thresh_label}"
                results.append(
                    _run_commit_check_sequence(
                        client,
                        f"sw235372_slm_profile_threshold_{thresh_label}",
                        ["configure", f"services performance-monitoring profiles cfm two-way-synthetic-loss-measurement {sname}", f"pcp {args.slm_pcp}", "inform-test-results enabled", "test-duration probes probe-count 5 probe-interval 1 repeat-interval 10"]
                        + thresh_lines
                        + ["commit check"]
                        + teardown_slm_profile_commands(sname),
                    )
                )
            for pcp_val in (0, 7):
                sname = f"{slm_prof_372}_pcp{pcp_val}"
                results.append(
                    _run_commit_check_sequence(
                        client,
                        f"sw235372_slm_profile_pcp_{pcp_val}",
                        ["configure", f"services performance-monitoring profiles cfm two-way-synthetic-loss-measurement {sname}", f"pcp {pcp_val}", "inform-test-results enabled", "test-duration probes probe-count 5 probe-interval 1 repeat-interval 10", "thresholds near-end-loss 1", "thresholds far-end-loss 1", "commit check"]
                        + teardown_slm_profile_commands(sname),
                    )
                )
            sname_inform = f"{slm_prof_372}_inform_disabled"
            results.append(
                _run_commit_check_sequence(
                    client,
                    "sw235372_slm_profile_inform_disabled",
                    ["configure", f"services performance-monitoring profiles cfm two-way-synthetic-loss-measurement {sname_inform}", f"pcp {args.slm_pcp}", "inform-test-results disabled", "test-duration probes probe-count 5 probe-interval 1 repeat-interval 10", "thresholds near-end-loss 1", "thresholds far-end-loss 1", "commit check"]
                    + teardown_slm_profile_commands(sname_inform),
                )
            )

        test_iterations = mep_list if mep_list else [None]
        for iteration in test_iterations:
            result_count_before = len(results)
            if iteration is not None:
                (md, ma, mep_id, direction, target_str) = iteration
                args.md, args.ma, args.mep_id = md, ma, mep_id
                args.target = target_str or "mep-id 2"
                args.slm_target = target_str or "mep-id 2"
                args.mep_direction = direction
                args.session = f"DM_CLI_TAB_mep{mep_id}"
                args.slm_session = f"SLM_CLI_TAB_mep{mep_id}"
                abort = False
                cleanup_done = False
                # Validate source mep-id for this MEP (some devices reject "direction" in PM source).
                mep_dir = getattr(args, "mep_direction", None)
                ok_src, why_src = validate_dm_source_mep_id(
                    client, args.md, args.ma, args.mep_id, timeout=20, direction=mep_dir
                )
                if not ok_src and mep_dir:
                    ok_src_no_dir, why_no_dir = validate_dm_source_mep_id(
                        client, args.md, args.ma, args.mep_id, timeout=20, direction=None
                    )
                    if ok_src_no_dir:
                        args.mep_direction = None
                        ok_src, why_src = True, "Accepted without direction (device may not support direction in PM source)."
                if ok_src:
                    results.append(
                        StepResult(
                            name="validate_source_mep_id",
                            ok=True,
                            details=why_src,
                        )
                    )
                else:
                    results.append(
                        StepResult(
                            name="validate_source_mep_id",
                            ok=False,
                            details=why_src,
                        )
                    )

            if not abort:
                # TAB completion checks
                tab_prefixes = [
                "services performance-monitoring cfm two-way-delay-measurement ",
                f"services performance-monitoring cfm two-way-delay-measurement {args.session} ",
                f"services performance-monitoring cfm two-way-delay-measurement {args.session} description ",
                "services performance-monitoring profiles cfm two-way-delay-measurement ",
                "services performance-monitoring profiles cfm two-way-synthetic-loss-measurement ",
                "services performance-monitoring cfm two-way-synthetic-loss-measurement ",
                f"services performance-monitoring cfm two-way-synthetic-loss-measurement {args.slm_session} ",
                f"services performance-monitoring cfm two-way-synthetic-loss-measurement {args.slm_session} description ",
            ]
            for prefix in tab_prefixes:
                _progress(f"tab_completion: {prefix.strip()}")
                output = run_tab_completion(client, prefix, timeout=15)
                err, errs = has_cli_error(output)
                results.append(
                    StepResult(
                        name=f"tab_completion: {prefix.strip()}",
                        ok=not err,
                        details="\n".join(errs) if err else "TAB completion returned output.",
                        raw_output=output,
                    )
                )
                raw_outputs.append(f"## TAB: {prefix.strip()}\n{output}")

            if not abort:
                # Base config commands: create profile, create session, commit
                _progress("configure_dm_session")
                base_commands = (
                    ["configure"]
                    + build_dm_profile_commands(args.profile)
                    + exit_profiles_to_cfg_root()
                    + build_commands(
                        args.session, args.profile, args.md, args.ma, args.mep_id, args.target, args.description,
                        getattr(args, "mep_direction", None),
                    )
                    # Commit from inside services hierarchy is allowed; then exit back to operational.
                    + ["commit", "exit", "exit", "exit", "exit"]
                )
                cmd_outputs = run_shell_sequence_detailed(client, base_commands, timeout=60)
                config_failed = False
                failed_cmd = None
                failed_errs: List[str] = []
                commit_output = ""
                for cmd, output in cmd_outputs:
                    raw_outputs.append(f"## CMD: {cmd}\n{output}")
                    err, errs = has_cli_error(output)
                    if err and not config_failed:
                        config_failed = True
                        failed_cmd = cmd
                        failed_errs = errs
                    if cmd == "commit":
                        commit_output = output
                
                # Auto-retry: If MEP conflict detected, delete existing session and retry
                retry_attempted = False
                if config_failed and any("in use with session" in e for e in failed_errs):
                    conflicting_session = extract_conflicting_session_name(failed_errs)
                    if conflicting_session:
                        _progress(f"auto_delete_conflicting_session: {conflicting_session}")
                        delete_ok, delete_msg = delete_existing_pm_session(client, conflicting_session, timeout=60)
                        results.append(
                            StepResult(
                                name=f"auto_delete_conflicting_session",
                                ok=delete_ok,
                                details=f"Session '{conflicting_session}' was blocking MEP {args.mep_id}. "
                                        f"Deletion: {delete_msg}",
                            )
                        )
                        if delete_ok:
                            retry_attempted = True
                            _progress("retry_configure_dm_session")
                            # Retry the same configuration
                            cmd_outputs = run_shell_sequence_detailed(client, base_commands, timeout=60)
                            config_failed = False
                            failed_cmd = None
                            failed_errs = []
                            commit_output = ""
                            for cmd, output in cmd_outputs:
                                raw_outputs.append(f"## RETRY CMD: {cmd}\n{output}")
                                err, errs = has_cli_error(output)
                                if err and not config_failed:
                                    config_failed = True
                                    failed_cmd = cmd
                                    failed_errs = errs
                                if cmd == "commit":
                                    commit_output = output
                
                results.append(
                    StepResult(
                        name="configure_dm_session" if not retry_attempted else "retry_configure_dm_session",
                        ok=not config_failed,
                        details=(
                            f"Failed command: {failed_cmd}\n" + "\n".join(failed_errs)
                            + (
                                "\n\nNOTE: If error is 'Source ... in use with session', "
                                "this MEP already has an active PM session. Device allows "
                                "max 1 DM/SLM session per MEP."
                                if config_failed and any("in use with session" in e for e in failed_errs)
                                else ""
                            )
                            if config_failed
                            else ("DM session configured after deleting conflicting session." if retry_attempted 
                                  else "DM session configured.")
                        ),
                    )
                )
    
                if config_failed:
                    if args.cleanup:
                        ok, detail = cleanup_config(
                            args.host,
                            args.user,
                            args.password,
                            args.session,
                            args.profile,
                            slm_session=args.slm_session,
                            slm_profile=args.slm_profile,
                        )
                        results.append(
                            StepResult(
                                name="cleanup",
                                ok=ok,
                                details=detail,
                            )
                        )
                        cleanup_done = True
                    results.append(
                        StepResult(
                            name="abort",
                            ok=False,
                            details="Base configuration failed; skipped commit/negative steps.",
                        )
                    )
                    abort = True
    
            if not abort:
                # SW-235927: Base SLM config commands: create profile, create session, commit
                _progress("configure_slm_session")
                slm_base_commands = (
                    ["configure"]
                    + build_slm_profile_commands(args.slm_profile, pcp=args.slm_pcp)
                    + exit_profiles_to_cfg_root()
                    + build_slm_session_commands(
                        args.slm_session,
                        args.slm_profile,
                        args.md,
                        args.ma,
                        args.mep_id,
                        args.slm_target,
                        args.slm_description,
                        getattr(args, "mep_direction", None),
                    )
                    + ["commit", "exit", "exit", "exit", "exit"]
                )
                slm_cmd_outputs = run_shell_sequence_detailed(client, slm_base_commands, timeout=60)
                slm_failed = False
                slm_failed_cmd = None
                slm_failed_errs: List[str] = []
                slm_commit_output = ""
                for cmd, output in slm_cmd_outputs:
                    raw_outputs.append(f"## CMD: {cmd}\n{output}")
                    err, errs = has_cli_error(output)
                    if err and not slm_failed:
                        slm_failed = True
                        slm_failed_cmd = cmd
                        slm_failed_errs = errs
                    if cmd == "commit":
                        slm_commit_output = output
                
                # Auto-retry: If MEP conflict detected, delete existing session and retry
                slm_retry_attempted = False
                if slm_failed and any("in use with session" in e for e in slm_failed_errs):
                    conflicting_slm_session = extract_conflicting_session_name(slm_failed_errs)
                    if conflicting_slm_session:
                        _progress(f"auto_delete_conflicting_slm_session: {conflicting_slm_session}")
                        delete_ok, delete_msg = delete_existing_pm_session(client, conflicting_slm_session, timeout=60)
                        results.append(
                            StepResult(
                                name=f"auto_delete_conflicting_slm_session",
                                ok=delete_ok,
                                details=f"Session '{conflicting_slm_session}' was blocking MEP {args.mep_id}. "
                                        f"Deletion: {delete_msg}",
                            )
                        )
                        if delete_ok:
                            slm_retry_attempted = True
                            _progress("retry_configure_slm_session")
                            # Retry the same configuration
                            slm_cmd_outputs = run_shell_sequence_detailed(client, slm_base_commands, timeout=60)
                            slm_failed = False
                            slm_failed_cmd = None
                            slm_failed_errs = []
                            slm_commit_output = ""
                            for cmd, output in slm_cmd_outputs:
                                raw_outputs.append(f"## RETRY CMD: {cmd}\n{output}")
                                err, errs = has_cli_error(output)
                                if err and not slm_failed:
                                    slm_failed = True
                                    slm_failed_cmd = cmd
                                    slm_failed_errs = errs
                                if cmd == "commit":
                                    slm_commit_output = output
                
                results.append(
                    StepResult(
                        name="configure_slm_session" if not slm_retry_attempted else "retry_configure_slm_session",
                        ok=not slm_failed,
                        details=(
                            f"Failed command: {slm_failed_cmd}\n" + "\n".join(slm_failed_errs)
                            + (
                                "\n\nNOTE: If error is 'Source ... in use with session', "
                                "this MEP already has an active PM session. Device allows "
                                "max 1 DM/SLM session per MEP."
                                if slm_failed and any("in use with session" in e for e in slm_failed_errs)
                                else ""
                            )
                            if slm_failed
                            else ("SLM session configured after deleting conflicting session." if slm_retry_attempted 
                                  else "SLM session configured.")
                        ),
                    )
                )
                if slm_commit_output:
                    slm_commit_err, slm_commit_errs = has_cli_error(slm_commit_output)
                    results.append(
                        StepResult(
                            name="commit_slm",
                            ok=not slm_commit_err,
                            details="\n".join(slm_commit_errs) if slm_commit_err else "Commit OK.",
                            raw_output=slm_commit_output,
                        )
                    )
                    ok_cfg, detail_cfg = show_config_contains(client, args.slm_session, timeout=30)
                    results.append(
                        StepResult(
                            name="verify_slm_config_present",
                            ok=ok_cfg,
                            details=detail_cfg,
                        )
                    )
                # SW-238031: verify SLM profile appears in show config
                ok_prof, detail_prof = show_config_contains(client, args.slm_profile, timeout=30)
                results.append(
                    StepResult(
                        name="verify_slm_profile_in_config",
                        ok=ok_prof,
                        details=detail_prof,
                    )
                )
                # SW-235376: show proactive SLM (pass unless command is unknown/invalid or output empty)
                if getattr(args, "skip_show_proactive", False):
                    results.append(StepResult(name="show_slm_proactive", ok=True, details="Skipped (--skip-show-proactive)."))
                else:
                    try:
                        # Use fallback helper for better reliability
                        slm_show_result = _run_show_command_test_fallback(
                            client, "show_slm_proactive",
                            [f"show services performance-monitoring cfm tests proactive two-way-synthetic-loss session-name {args.slm_session} detail",
                             "show services performance-monitoring cfm tests proactive detail",
                             "show services performance-monitoring cfm tests proactive",
                             "show services performance-monitoring cfm tests"],
                            expected_strings=[args.slm_session], timeout=40,
                        )
                        results.append(slm_show_result)
                    except Exception as e:
                        results.append(StepResult(name="show_slm_proactive", ok=False, details=f"Exception: {str(e)}"))
    
            if not abort:
                # DM session knobs: admin-state enabled/disabled, description, profile, source, target variants
                _progress("sw235372_dm_session_variants")
                # One DM/SLM session per source MEP: reconfigure the existing session (no second session).
                # Use committed base profile (args.profile) so session references an existing profile.
                results.append(
                    _run_commit_check_sequence(
                        client,
                        "sw235372_dm_session_target_mep",
                        [
                            "configure",
                            f"services performance-monitoring cfm two-way-delay-measurement {args.session}",
                            "admin-state enabled",
                            "admin-state disabled",
                            f"description {args.description}",
                            f"profile {args.profile}",
                            _source_line(args.md, args.ma, args.mep_id, getattr(args, "mep_direction", None)),
                            f"target {args.target}",
                            "commit check",
                            "exit", "exit", "exit", "exit",
                        ]
                    )
                )
                results.append(
                    _run_commit_check_sequence(
                        client,
                        "sw235372_dm_session_target_mac",
                        [
                            "configure",
                            f"services performance-monitoring cfm two-way-delay-measurement {args.session}",
                            "admin-state enabled",
                            f"description {args.description}",
                            f"profile {args.profile}",
                            _source_line(args.md, args.ma, args.mep_id, getattr(args, "mep_direction", None)),
                            "target mac-address 00:11:22:33:44:55",
                            "commit check",
                            f"target {args.target}",  # restore mep-id target for later steps
                            "exit", "exit", "exit", "exit",
                        ]
                    )
                )
    
                # SLM session knobs: one SLM per MEP – reconfigure existing session (no second session).
                _progress("sw235372_slm_session_variants")
                results.append(
                    _run_commit_check_sequence(
                        client,
                        "sw235372_slm_session_target_mep",
                        [
                            "configure",
                            f"services performance-monitoring cfm two-way-synthetic-loss-measurement {args.slm_session}",
                            "admin-state enabled",
                            "admin-state disabled",
                            f"description {args.slm_description}",
                            f"profile {args.slm_profile}",
                            _source_line(args.md, args.ma, args.mep_id, getattr(args, "mep_direction", None)),
                            f"target {args.slm_target}",
                            "commit check",
                            "exit", "exit", "exit", "exit",
                        ]
                    )
                )
                results.append(
                    _run_commit_check_sequence(
                        client,
                        "sw235372_slm_session_target_mac",
                        [
                            "configure",
                            f"services performance-monitoring cfm two-way-synthetic-loss-measurement {args.slm_session}",
                            "admin-state enabled",
                            f"description {args.slm_description}",
                            f"profile {args.slm_profile}",
                            _source_line(args.md, args.ma, args.mep_id, getattr(args, "mep_direction", None)),
                            "target mac-address 00:11:22:33:44:55",
                            "commit check",
                            f"target {args.slm_target}",  # restore mep-id target for later steps
                            "exit", "exit", "exit", "exit",
                        ]
                    )
                )
    
                commit_err, commit_errs = has_cli_error(commit_output)
                commit_no_changes = "no configuration changes were made" in commit_output.lower()
                results.append(
                    StepResult(
                        name="commit",
                        ok=not commit_err,
                        details=(
                            "\n".join(commit_errs)
                            if commit_err
                            else (
                                "Commit OK."
                                if not commit_no_changes
                                else "Commit OK (no configuration changes)."
                            )
                        ),
                        raw_output=commit_output,
                    )
                )
                ok_cfg, detail_cfg = show_config_contains(client, args.session, timeout=30)
                results.append(
                    StepResult(
                        name="verify_dm_config_present",
                        ok=ok_cfg,
                        details=detail_cfg,
                    )
                )
                # SW-238005: verify DM profile appears in show config
                ok_prof_dm, detail_prof_dm = show_config_contains(client, args.profile, timeout=30)
                results.append(
                    StepResult(
                        name="verify_dm_profile_in_config",
                        ok=ok_prof_dm,
                        details=detail_prof_dm,
                    )
                )
                # SW-235376: show proactive DM (pass unless command is unknown/invalid or output empty)
                if getattr(args, "skip_show_proactive", False):
                    results.append(StepResult(name="show_dm_proactive", ok=True, details="Skipped (--skip-show-proactive)."))
                else:
                    try:
                        # Use fallback helper for better reliability
                        dm_show_result = _run_show_command_test_fallback(
                            client, "show_dm_proactive",
                            [f"show services performance-monitoring cfm tests proactive two-way-delay session-name {args.session} detail",
                             "show services performance-monitoring cfm tests proactive detail",
                             "show services performance-monitoring cfm tests proactive",
                             "show services performance-monitoring cfm tests"],
                            expected_strings=[args.session], timeout=40,
                        )
                        results.append(dm_show_result)
                    except Exception as e:
                        results.append(StepResult(name="show_dm_proactive", ok=False, details=f"Exception: {str(e)}"))
                # SW-237984: on-demand start + stop tests (DM, SLM, loopback, linktrace)
                if getattr(args, "skip_on_demand_stop", False):
                    results.append(StepResult(name="on_demand_tests", ok=True, details="Skipped (--skip-on-demand-stop)."))
                else:
                    run_on_demand_stop_tests(client, args, results, raw_outputs, _progress)

            # -----------------------------------------------------------
            # Gap 2: TAB completion for profile-level subcommands
            # (profiles are committed at this point)
            # -----------------------------------------------------------
            if not abort:
                profile_tab_prefixes = [
                    f"services performance-monitoring profiles cfm two-way-delay-measurement {args.profile} ",
                    f"services performance-monitoring profiles cfm two-way-delay-measurement {args.profile} thresholds ",
                    f"services performance-monitoring profiles cfm two-way-delay-measurement {args.profile} test-duration ",
                    f"services performance-monitoring profiles cfm two-way-synthetic-loss-measurement {args.slm_profile} ",
                    f"services performance-monitoring profiles cfm two-way-synthetic-loss-measurement {args.slm_profile} thresholds ",
                    f"services performance-monitoring profiles cfm two-way-synthetic-loss-measurement {args.slm_profile} test-duration ",
                ]
                for prefix in profile_tab_prefixes:
                    _progress(f"tab_completion_profile: {prefix.strip()}")
                    output = run_tab_completion(client, prefix, timeout=15)
                    err, errs = has_cli_error(output)
                    results.append(
                        StepResult(
                            name=f"tab_completion_profile: {prefix.strip()}",
                            ok=not err,
                            details="\n".join(errs) if err else "TAB completion returned output.",
                            raw_output=output,
                        )
                    )
                    raw_outputs.append(f"## TAB: {prefix.strip()}\n{output}")

            # -----------------------------------------------------------
            # Gap 3: PCP boundary/range testing for SLM
            # -----------------------------------------------------------
            if not abort:
                _progress("sw235372_slm_profile_pcp_boundary")
                slm_prof_pcp = f"{args.slm_profile}_PCP"
                slm_pcp_base_cmds = [
                    f"services performance-monitoring profiles cfm two-way-synthetic-loss-measurement {slm_prof_pcp}",
                    "inform-test-results enabled",
                    "thresholds near-end-loss 1",
                    "thresholds far-end-loss 1",
                    "test-duration probes probe-count 5 probe-interval 1 repeat-interval 10",
                ]
                results.append(
                    _run_commit_check_sequence(
                        client,
                        "sw235372_slm_profile_pcp_0",
                        ["configure"] + [slm_pcp_base_cmds[0], "pcp 0"] + slm_pcp_base_cmds[1:]
                        + ["commit check"]
                        + teardown_slm_profile_commands(slm_prof_pcp),
                    )
                )
                results.append(
                    _run_commit_check_sequence(
                        client,
                        "sw235372_slm_profile_pcp_7",
                        ["configure"] + [slm_pcp_base_cmds[0], "pcp 7"] + slm_pcp_base_cmds[1:]
                        + ["commit check"]
                        + teardown_slm_profile_commands(slm_prof_pcp),
                    )
                )
                # Negative: invalid PCP value (expect error)
                neg_pcp_cmds = (
                    ["configure"]
                    + [slm_pcp_base_cmds[0], "pcp 8"]
                    + slm_pcp_base_cmds[1:]
                    + ["commit check"]
                    + teardown_slm_profile_commands(slm_prof_pcp)
                )
                neg_pcp_outputs = run_shell_sequence_detailed(client, neg_pcp_cmds, timeout=60)
                neg_pcp_err = False
                neg_pcp_failed_cmd = None
                neg_pcp_failed_errs: List[str] = []
                for cmd, output in neg_pcp_outputs:
                    raw_outputs.append(f"## CMD: {cmd}\n{output}")
                    err, errs = has_cli_error(output)
                    if err and not neg_pcp_err:
                        neg_pcp_err = True
                        neg_pcp_failed_cmd = cmd
                        neg_pcp_failed_errs = errs
                results.append(
                    StepResult(
                        name="negative_slm_pcp_invalid",
                        ok=neg_pcp_err,
                        details=(
                            f"Expected error; failed on: {neg_pcp_failed_cmd}\n" + "\n".join(neg_pcp_failed_errs)
                            if neg_pcp_err
                            else "No error for invalid PCP value 8."
                        ),
                    )
                )

            # -----------------------------------------------------------
            # Gap 5: Show commands testing (SW-206837)
            # -----------------------------------------------------------
            if not abort:
                _progress("show_commands")
                results.append(
                    _run_show_command_test_fallback(
                        client, "show_cfm_tests_summary",
                        ["show services performance-monitoring cfm tests",
                         "show services performance-monitoring connectivity-fault-management sessions"],
                        expected_strings=[args.session], timeout=30,
                    )
                )
                results.append(
                    _run_show_command_test_fallback(
                        client, "show_cfm_tests_proactive",
                        ["show services performance-monitoring cfm tests proactive"],
                        expected_strings=[args.session], timeout=30,
                    )
                )
                results.append(
                    _run_show_command_test_fallback(
                        client, "show_cfm_tests_proactive_dm",
                        ["show services performance-monitoring cfm tests proactive two-way-delay",
                         "show services performance-monitoring cfm tests proactive detail",
                         "show services performance-monitoring cfm tests proactive",
                         "show services performance-monitoring cfm tests"],
                        expected_strings=[args.session], timeout=30,
                    )
                )
                results.append(
                    _run_show_command_test_fallback(
                        client, "show_cfm_tests_proactive_slm",
                        ["show services performance-monitoring cfm tests proactive two-way-synthetic-loss",
                         "show services performance-monitoring cfm tests proactive detail",
                         "show services performance-monitoring cfm tests proactive",
                         "show services performance-monitoring cfm tests"],
                        expected_strings=[args.slm_session], timeout=30,
                    )
                )
                results.append(
                    _run_show_command_test_fallback(
                        client, "show_cfm_tests_dm_detail",
                        ["show services performance-monitoring cfm tests proactive two-way-delay detail",
                         f"show services performance-monitoring cfm tests proactive two-way-delay session-name {args.session} detail",
                         "show services performance-monitoring cfm tests proactive detail",
                         "show services performance-monitoring cfm tests proactive",
                         "show services performance-monitoring cfm tests"],
                        expected_strings=[args.md, args.ma, args.mep_id], timeout=30,
                    )
                )
                results.append(
                    _run_show_command_test_fallback(
                        client, "show_cfm_tests_slm_detail",
                        ["show services performance-monitoring cfm tests proactive two-way-synthetic-loss detail",
                         f"show services performance-monitoring cfm tests proactive two-way-synthetic-loss session-name {args.slm_session} detail",
                         "show services performance-monitoring cfm tests proactive detail",
                         "show services performance-monitoring cfm tests proactive",
                         "show services performance-monitoring cfm tests"],
                        expected_strings=[args.md, args.ma, args.mep_id], timeout=30,
                    )
                )
                results.append(
                    _run_show_command_test_fallback(
                        client, "show_cfm_tests_filter_session",
                        [f"show services performance-monitoring cfm tests session-name {args.session}",
                         "show services performance-monitoring cfm tests proactive",
                         "show services performance-monitoring cfm tests"],
                        expected_strings=[args.session], timeout=30,
                    )
                )
                results.append(
                    _run_show_command_test_fallback(
                        client, "show_cfm_tests_filter_md",
                        [f"show services performance-monitoring cfm tests md-name {args.md}"],
                        expected_strings=[args.md], timeout=30,
                    )
                )
                results.append(
                    _run_show_command_test_fallback(
                        client, "show_cfm_tests_filter_ma",
                        [f"show services performance-monitoring cfm tests ma-name {args.ma}",
                         "show services performance-monitoring cfm tests proactive",
                         "show services performance-monitoring cfm tests"],
                        expected_strings=[args.ma], timeout=30,
                    )
                )
                results.append(
                    _run_show_command_test_fallback(
                        client, "show_cfm_tests_filter_mep",
                        [f"show services performance-monitoring cfm tests mep-id {args.mep_id}",
                         "show services performance-monitoring cfm tests",
                         "show services performance-monitoring cfm tests proactive"],
                        expected_strings=[args.mep_id], timeout=30,
                    )
                )

            # -----------------------------------------------------------
            # Gap 6: Operational state / session lifecycle verification
            # -----------------------------------------------------------
            if not abort:
                _progress("verify_dm_operational_state")
                # Try multiple show command variants - device may not support all syntax
                dm_show_commands = [
                    f"show services performance-monitoring cfm tests proactive two-way-delay session-name {args.session} detail",
                    f"show services performance-monitoring cfm tests proactive two-way-delay detail",
                    "show services performance-monitoring cfm tests proactive detail",
                    "show services performance-monitoring cfm tests proactive",
                    "show services performance-monitoring cfm tests",
                ]
                dm_state_output = ""
                err_dm_state = True
                errs_dm_state = []
                for dm_cmd in dm_show_commands:
                    dm_state_output = run_shell_with_prompt(client, dm_cmd, timeout=30)
                    err_dm_state, errs_dm_state = has_cli_error(dm_state_output)
                    if not err_dm_state:
                        break  # Found a working command
                
                dm_state_ok = False
                dm_state_detail = ""
                state_indicators = ["enabled", "Ongoing", "active", "running", "Valid", args.session]
                if err_dm_state:
                    dm_state_detail = f"CLI error on all show variants: {'; '.join(errs_dm_state)}"
                else:
                    found = [s for s in state_indicators if s.lower() in dm_state_output.lower()]
                    if found:
                        dm_state_ok = True
                        dm_state_detail = f"Operational indicators found: {found}"
                    else:
                        dm_state_detail = "No operational state indicators found in show output."
                results.append(
                    StepResult(
                        name="verify_dm_operational_state",
                        ok=dm_state_ok,
                        details=dm_state_detail,
                        raw_output=dm_state_output,
                    )
                )

                _progress("verify_slm_operational_state")
                # Try multiple show command variants - device may not support all syntax
                slm_show_commands = [
                    f"show services performance-monitoring cfm tests proactive two-way-synthetic-loss session-name {args.slm_session} detail",
                    f"show services performance-monitoring cfm tests proactive two-way-synthetic-loss detail",
                    "show services performance-monitoring cfm tests proactive detail",
                    "show services performance-monitoring cfm tests proactive",
                    "show services performance-monitoring cfm tests",
                ]
                slm_state_output = ""
                err_slm_state = True
                errs_slm_state = []
                for slm_cmd in slm_show_commands:
                    slm_state_output = run_shell_with_prompt(client, slm_cmd, timeout=30)
                    err_slm_state, errs_slm_state = has_cli_error(slm_state_output)
                    if not err_slm_state:
                        break  # Found a working command
                
                slm_state_ok = False
                slm_state_detail = ""
                if err_slm_state:
                    slm_state_detail = f"CLI error on all show variants: {'; '.join(errs_slm_state)}"
                else:
                    found = [s for s in state_indicators if s.lower() in slm_state_output.lower()]
                    if found:
                        slm_state_ok = True
                        slm_state_detail = f"Operational indicators found: {found}"
                    else:
                        slm_state_detail = "No operational state indicators found in show output."
                results.append(
                    StepResult(
                        name="verify_slm_operational_state",
                        ok=slm_state_ok,
                        details=slm_state_detail,
                        raw_output=slm_state_output,
                    )
                )

                # 6c: Verify session parameter change
                _progress("verify_session_param_change")
                changed_desc = "changed_desc_test"
                param_change_cmds = [
                    "configure",
                    f"services performance-monitoring cfm two-way-delay-measurement {args.session}",
                    f"description {changed_desc}",
                    "commit",
                    "exit", "exit", "exit", "exit",
                ]
                param_change_outputs = run_shell_sequence_detailed(client, param_change_cmds, timeout=60)
                param_change_err = False
                for cmd, output in param_change_outputs:
                    raw_outputs.append(f"## CMD: {cmd}\n{output}")
                    err, errs = has_cli_error(output)
                    if err:
                        param_change_err = True
                if not param_change_err:
                    # Try multiple show commands to verify description change
                    verify_show_cmds = [
                        f"show services performance-monitoring cfm tests proactive two-way-delay session-name {args.session} detail",
                        f"show services performance-monitoring cfm tests proactive two-way-delay detail",
                        "show services performance-monitoring cfm tests proactive detail",
                        "show services performance-monitoring cfm tests proactive",
                        "show services performance-monitoring cfm tests",
                        f"show config services performance-monitoring cfm two-way-delay-measurement {args.session}",
                    ]
                    verify_output = ""
                    for vcmd in verify_show_cmds:
                        verify_output = run_shell_with_prompt(client, vcmd, timeout=30)
                        err, _ = has_cli_error(verify_output)
                        if not err and changed_desc in verify_output:
                            break  # Found command that shows the description
                    
                    desc_found = changed_desc in verify_output
                    restore_cmds = [
                        "configure",
                        f"services performance-monitoring cfm two-way-delay-measurement {args.session}",
                        f"description {args.description}",
                        "commit",
                        "exit", "exit", "exit", "exit",
                    ]
                    run_shell_sequence_detailed(client, restore_cmds, timeout=60)
                    results.append(
                        StepResult(
                            name="verify_session_param_change",
                            ok=desc_found,
                            details=(
                                f"Changed description '{changed_desc}' found in show output."
                                if desc_found
                                else f"Changed description '{changed_desc}' NOT found in show output."
                            ),
                            raw_output=verify_output,
                        )
                    )
                else:
                    results.append(
                        StepResult(
                            name="verify_session_param_change",
                            ok=False,
                            details="Failed to change session description (CLI error during commit).",
                        )
                    )

            # -----------------------------------------------------------
            # Gap 7: Historic test results verification (SW-206804)
            # -----------------------------------------------------------
            if not abort:
                _progress("verify_historic_results")
                if args.wait_for_results > 0:
                    if args.show_progress:
                        print(f"  Waiting {args.wait_for_results}s for proactive probes to generate results...")
                    time.sleep(args.wait_for_results)

                # Try multiple show command variants for DM
                hist_show_cmds = [
                    f"show services performance-monitoring cfm tests proactive two-way-delay session-name {args.session} detail",
                    f"show services performance-monitoring cfm tests proactive two-way-delay detail",
                    "show services performance-monitoring cfm tests proactive detail",
                    "show services performance-monitoring cfm tests proactive",
                ]
                hist_output = ""
                err_hist = True
                errs_hist = []
                for hist_cmd in hist_show_cmds:
                    hist_output = run_shell_with_prompt(client, hist_cmd, timeout=30)
                    err_hist, errs_hist = has_cli_error(hist_output)
                    if not err_hist:
                        break  # Found working command
                
                hist_ok = False
                hist_detail = ""
                hist_indicators = ["Index", "Historical", "valid", "PDU", "transmitted", "received", "Start time",
                                   "delay", "loss", "success"]
                if err_hist:
                    hist_detail = f"CLI error on all show variants: {'; '.join(errs_hist)}"
                else:
                    found_hist = [s for s in hist_indicators if s.lower() in hist_output.lower()]
                    if found_hist:
                        hist_ok = True
                        hist_detail = f"Historic result indicators found: {found_hist}"
                    else:
                        hist_detail = (
                            f"No historic result indicators found after {args.wait_for_results}s wait. "
                            "Probes may not have completed yet."
                        )
                results.append(
                    StepResult(
                        name="verify_historic_results",
                        ok=hist_ok,
                        details=hist_detail,
                        raw_output=hist_output,
                    )
                )

                # Try multiple show command variants for SLM
                slm_hist_show_cmds = [
                    f"show services performance-monitoring cfm tests proactive two-way-synthetic-loss session-name {args.slm_session} detail",
                    f"show services performance-monitoring cfm tests proactive two-way-synthetic-loss detail",
                    "show services performance-monitoring cfm tests proactive detail",
                    "show services performance-monitoring cfm tests proactive",
                ]
                slm_hist_output = ""
                err_slm_hist = True
                errs_slm_hist = []
                for slm_hist_cmd in slm_hist_show_cmds:
                    slm_hist_output = run_shell_with_prompt(client, slm_hist_cmd, timeout=30)
                    err_slm_hist, errs_slm_hist = has_cli_error(slm_hist_output)
                    if not err_slm_hist:
                        break  # Found working command
                
                slm_hist_ok = False
                slm_hist_detail = ""
                if err_slm_hist:
                    slm_hist_detail = f"CLI error on all show variants: {'; '.join(errs_slm_hist)}"
                else:
                    found_slm_hist = [s for s in hist_indicators if s.lower() in slm_hist_output.lower()]
                    if found_slm_hist:
                        slm_hist_ok = True
                        slm_hist_detail = f"Historic result indicators found: {found_slm_hist}"
                    else:
                        slm_hist_detail = (
                            f"No historic result indicators found after {args.wait_for_results}s wait. "
                            "Probes may not have completed yet."
                        )
                results.append(
                    StepResult(
                        name="verify_slm_historic_results",
                        ok=slm_hist_ok,
                        details=slm_hist_detail,
                        raw_output=slm_hist_output,
                    )
                )

            # -----------------------------------------------------------
            # Gap 8: System event CFM_PROACTIVE_TEST_FAILURE (SW-207209)
            # -----------------------------------------------------------
            if not abort and not args.skip_event_test:
                _progress("system_event_cfm_proactive_test_failure")
                low_thresh_profile = "DM_LOW_THRESH"
                low_thresh_session = "DM_LOW_THRESH_SESS"

                # Step 1: Open a dedicated logging channel BEFORE configuring
                # the low-threshold session, so we capture events in real-time.
                logging_channel: Optional[paramiko.Channel] = None
                try:
                    logging_channel = _open_logging_channel(client, timeout=30)
                    if args.show_progress:
                        print("  Opened 'set logging terminal' channel for event capture.")
                except Exception as log_exc:
                    if args.show_progress:
                        print(f"  WARNING: Could not open logging channel: {log_exc}")

                # Step 2: Configure and commit the low-threshold session
                # All commands in a single sequence (same shell session)
                event_setup_and_commit_cmds = (
                    ["configure"]
                    + [
                        f"services performance-monitoring profiles cfm two-way-delay-measurement {low_thresh_profile}",
                        "inform-test-results enabled",
                        "test-duration probes probe-count 3 probe-interval 1 repeat-interval 5",
                        "thresholds delay-rtt-max 1",
                        "thresholds success-rate 10000",
                        "exit",
                    ]
                    + exit_profiles_to_cfg_root()
                    + [
                        f"services performance-monitoring cfm two-way-delay-measurement {low_thresh_session}",
                        "admin-state enabled",
                        f"profile {low_thresh_profile}",
                        f"source maintenance-domain {args.md} maintenance-association {args.ma} mep-id {args.mep_id}",
                        f"target {args.target}",
                        "exit",
                        "exit",  # Back to cfg-srv-pm
                        "exit",  # Back to cfg-srv
                        "exit",  # Back to cfg
                        "commit",  # Commit while in config mode
                        "exit",  # Exit config mode
                    ]
                )

                # Try setup + commit (max 2 attempts with auto-conflict resolution)
                event_setup_err = True
                event_setup_err_msgs: List[str] = []
                disabled_session_for_event: Optional[str] = None
                max_event_retries = 2
                retry_log: List[str] = []  # Track retry attempts for error reporting
                for event_attempt in range(max_event_retries):
                    if args.show_progress and event_attempt > 0:
                        print(f"  Event setup retry attempt {event_attempt + 1}/{max_event_retries}...")
                    # Send all commands in ONE shell session
                    all_outputs = run_shell_sequence_detailed(client, event_setup_and_commit_cmds, timeout=60)
                    commit_output = ""
                    for cmd, output in all_outputs:
                        raw_outputs.append(f"## CMD: {cmd}\n{output}")
                        if cmd == "commit":
                            commit_output = output

                    commit_err, commit_errs = has_cli_error(commit_output)
                    if not commit_err:
                        # Success!
                        event_setup_err = False
                        event_setup_err_msgs = []
                        retry_log.append(f"Attempt {event_attempt+1}: Success")
                        if args.show_progress:
                            print(f"  Event test low-threshold session configured successfully.")
                        break
                    else:
                        # Commit failed (but we already exited config mode, so changes discarded)
                        event_setup_err_msgs = commit_errs
                        retry_log.append(f"Attempt {event_attempt+1}: Commit failed - {commit_errs[0] if commit_errs else 'unknown error'}")
                        if args.show_progress:
                            print(f"  Commit failed: {'; '.join(commit_errs[:2])}")
                        if any("in use with session" in e for e in commit_errs):
                            conflicting_evt_session = extract_conflicting_session_name(commit_errs)
                            retry_log.append(f"  Detected conflict with session: {conflicting_evt_session}")
                            if args.show_progress:
                                print(f"  Extracted conflicting session: {conflicting_evt_session}")
                            if conflicting_evt_session and event_attempt < max_event_retries - 1:
                                _progress(f"auto_delete_conflicting_session: {conflicting_evt_session}")
                                if args.show_progress:
                                    print(f"  Attempting to delete: {conflicting_evt_session}")
                                try:
                                    retry_log.append(f"  Attempting to delete {conflicting_evt_session}...")
                                    delete_ok, delete_msg = delete_existing_pm_session(client, conflicting_evt_session, timeout=60)
                                    raw_outputs.append(f"## DELETE CONFLICTING: {conflicting_evt_session}\n{delete_msg}")
                                    if delete_ok:
                                        disabled_session_for_event = conflicting_evt_session
                                        retry_log.append(f"  Successfully deleted {conflicting_evt_session}, will retry")
                                        _progress(f"auto_delete successful, retrying event setup...")
                                        if args.show_progress:
                                            print(f"  Successfully deleted {conflicting_evt_session}, retrying...")
                                        continue
                                    else:
                                        retry_log.append(f"  Failed to delete: {delete_msg}")
                                        if args.show_progress:
                                            print(f"  Failed to delete conflicting session: {delete_msg}")
                                        break
                                except Exception as dis_exc:
                                    retry_log.append(f"  Exception during delete: {dis_exc}")
                                    _progress(f"auto_delete exception: {dis_exc}")
                                    if args.show_progress:
                                        print(f"  Exception during delete: {dis_exc}")
                                    break
                            else:
                                # No conflict or last attempt - give up
                                if not conflicting_evt_session:
                                    retry_log.append(f"  Could not extract session name from error, giving up")
                                else:
                                    retry_log.append(f"  Last retry attempt, giving up")
                                if args.show_progress:
                                    if not conflicting_evt_session:
                                        print(f"  Could not extract conflicting session name, giving up.")
                                    else:
                                        print(f"  Last retry attempt, giving up.")
                                break
                        else:
                            # Different error (not MEP conflict) - give up
                            retry_log.append(f"  Not a MEP conflict error, giving up")
                            if args.show_progress:
                                print(f"  Error is not MEP conflict, giving up.")
                            break

                # Step 3: Wait for probes to run and check for event
                if not event_setup_err:
                    if args.show_progress:
                        print(f"  Waiting {args.low_threshold_wait}s for low-threshold violation event...")
                    time.sleep(args.low_threshold_wait)

                    # Try the logging channel first (preferred method)
                    evt_found = False
                    evt_detail = ""
                    evt_raw = ""
                    if logging_channel is not None:
                        try:
                            evt_found, evt_detail, evt_raw = _read_logging_channel(
                                logging_channel, "CFM_PROACTIVE_TEST_FAILURE", timeout=10
                            )
                        except Exception as read_exc:
                            evt_detail = f"Error reading logging channel: {read_exc}"

                    # Fallback: try legacy show commands if logging channel failed
                    if not evt_found:
                        legacy_found, legacy_detail, legacy_raw = _check_system_event(
                            client, "CFM_PROACTIVE_TEST_FAILURE", timeout=30
                        )
                        if legacy_found:
                            evt_found, evt_detail, evt_raw = legacy_found, legacy_detail, legacy_raw
                        else:
                            # Combine details from both attempts
                            if evt_detail:
                                evt_detail = f"Logging channel: {evt_detail}; Legacy: {legacy_detail}"
                            else:
                                evt_detail = legacy_detail
                            evt_raw = legacy_raw

                    results.append(
                        StepResult(
                            name="system_event_cfm_proactive_test_failure",
                            ok=evt_found,
                            details=evt_detail,
                            raw_output=evt_raw,
                        )
                    )

                    if evt_found:
                        expected_event_fields = [low_thresh_session, "delay"]
                        missing_fields = [f for f in expected_event_fields if f.lower() not in evt_raw.lower()]
                        results.append(
                            StepResult(
                                name="system_event_content_check",
                                ok=len(missing_fields) == 0,
                                details=(
                                    "Event contains expected fields."
                                    if not missing_fields
                                    else f"Missing fields in event: {missing_fields}"
                                ),
                                raw_output=evt_raw,
                            )
                        )
                else:
                    # Include retry log in error details
                    error_detail = f"Failed to set up low-threshold session: {'; '.join(event_setup_err_msgs)}"
                    if retry_log:
                        error_detail += f" | Retry history: {' → '.join(retry_log)}"
                    results.append(
                        StepResult(
                            name="system_event_cfm_proactive_test_failure",
                            ok=False,
                            details=error_detail,
                        )
                    )

                # Close logging channel
                if logging_channel is not None:
                    try:
                        logging_channel.close()
                    except Exception:
                        pass

                # Teardown low-threshold session and profile
                _progress("cleanup_low_threshold")
                event_teardown_cmds = [
                    "configure",
                    f"no services performance-monitoring cfm two-way-delay-measurement {low_thresh_session}",
                    f"no services performance-monitoring profiles cfm two-way-delay-measurement {low_thresh_profile}",
                    "commit",
                    "exit",
                ]
                run_shell_sequence_detailed(client, event_teardown_cmds, timeout=60)

                # Re-create the main test session if we deleted it for the event test
                if disabled_session_for_event:
                    _progress(f"recreate_main_session: {disabled_session_for_event}")
                    recreate_cmds = (
                        ["configure"]
                        + build_commands(
                            args.session, args.profile, args.md, args.ma,
                            args.mep_id, args.target, args.description,
                            getattr(args, "mep_direction", None),
                        )
                        + ["commit", "exit", "exit", "exit", "exit"]
                    )
                    try:
                        recreate_outputs = run_shell_sequence_detailed(client, recreate_cmds, timeout=60)
                        recreate_ok = True
                        for cmd, output in recreate_outputs:
                            raw_outputs.append(f"## RECREATE CMD: {cmd}\n{output}")
                            if cmd == "commit":
                                err, errs = has_cli_error(output)
                                if err:
                                    recreate_ok = False
                                    _progress(f"recreate commit failed: {'; '.join(errs)}")
                        if recreate_ok and args.show_progress:
                            print(f"  Re-created main session {disabled_session_for_event}.")
                    except Exception as reen_exc:
                        _progress(f"recreate failed: {reen_exc}")

            # -----------------------------------------------------------
            # Gap 4: CLI validation -- reject commit on dependency deletion (SW-198127)
            # -----------------------------------------------------------
            if not abort:
                _progress("negative_delete_cfm_dependency")
                dep_delete_cmds = [
                    "configure",
                    f"no services ethernet-oam connectivity-fault-management maintenance-domain {args.md} maintenance-association {args.ma} local-mep {args.mep_id}",
                    "commit check",
                    "rollback 0",
                    "exit",
                ]
                dep_outputs = run_shell_sequence_detailed(client, dep_delete_cmds, timeout=60)
                dep_err = False
                dep_failed_cmd = None
                dep_failed_errs: List[str] = []
                dep_commit_check = ""
                for cmd, output in dep_outputs:
                    raw_outputs.append(f"## CMD: {cmd}\n{output}")
                    if cmd == "commit check":
                        dep_commit_check = output
                        err, errs = has_cli_error(output)
                        if err:
                            dep_err = True
                            dep_failed_cmd = cmd
                            dep_failed_errs = errs
                results.append(
                    StepResult(
                        name="negative_delete_cfm_dependency",
                        ok=dep_err,
                        details=(
                            f"Expected error; commit check rejected: {dep_failed_cmd}\n" + "\n".join(dep_failed_errs)
                            if dep_err
                            else "Commit check did NOT reject deletion of referenced CFM dependency."
                        ),
                        raw_output=dep_commit_check,
                    )
                )

            if not abort:
                # Negative (SLM): long name/description (expect command error or commit check failure)
                _progress("negative_slm_long_name_desc")
                slm_long_profile = "SLM_PROF_" + ("X" * 220)
                slm_long_name = "SLM_" + ("X" * 220)
                neg_slm_long_cmds = (
                    ["configure"]
                    + build_slm_profile_commands(slm_long_profile, pcp=args.slm_pcp)
                    + exit_profiles_to_cfg_root()
                    + build_slm_session_commands(
                        slm_long_name,
                        slm_long_profile,
                        args.md,
                        args.ma,
                        args.mep_id,
                        args.slm_target,
                        args.long_desc,
                        getattr(args, "mep_direction", None),
                    )
                    + ["commit check"]
                    + teardown_slm_session_and_profile(slm_long_name, slm_long_profile)
                )
                cmd_outputs = run_shell_sequence_detailed(client, neg_slm_long_cmds, timeout=60)
                neg_err = False
                neg_failed_cmd = None
                neg_failed_errs = []
                neg_commit_check = ""
                for cmd, output in cmd_outputs:
                    raw_outputs.append(f"## CMD: {cmd}\n{output}")
                    err, errs = has_cli_error(output)
                    if err and not neg_err:
                        neg_err = True
                        neg_failed_cmd = cmd
                        neg_failed_errs = errs
                    if cmd == "commit check":
                        neg_commit_check = output
                if not neg_err and neg_commit_check:
                    err, errs = has_cli_error(neg_commit_check)
                    neg_err = err
                    if err:
                        neg_failed_cmd = "commit check"
                        neg_failed_errs = errs
                results.append(
                    StepResult(
                        name="negative_slm_long_name_desc",
                        ok=neg_err,
                        details=(
                            f"Expected error; failed on: {neg_failed_cmd}\n" + "\n".join(neg_failed_errs)
                            if neg_err
                            else "No error for long name/desc."
                        ),
                        raw_output=neg_commit_check,
                    )
                )
    
            if not abort:
                # Negative (SLM): bad MD/MA / non-existent CFM (expect command error or commit check failure)
                _progress("negative_slm_bad_md_ma")
                neg_slm_profile_bad = f"{args.slm_profile}_BAD"
                neg_slm_bad_cmds = (
                    ["configure"]
                    + build_slm_profile_commands(neg_slm_profile_bad, pcp=args.slm_pcp)
                    + exit_profiles_to_cfg_root()
                    + build_slm_session_commands(
                        f"{args.slm_session}_BAD",
                        neg_slm_profile_bad,
                        args.bad_md,
                        args.bad_ma,
                        args.mep_id,
                        args.slm_target,
                        args.slm_description,
                        getattr(args, "mep_direction", None),
                    )
                    + ["commit check"]
                    + teardown_slm_session_and_profile(f"{args.slm_session}_BAD", neg_slm_profile_bad)
                )
                cmd_outputs = run_shell_sequence_detailed(client, neg_slm_bad_cmds, timeout=60)
                neg_err = False
                neg_failed_cmd = None
                neg_failed_errs = []
                neg_commit_check = ""
                for cmd, output in cmd_outputs:
                    raw_outputs.append(f"## CMD: {cmd}\n{output}")
                    err, errs = has_cli_error(output)
                    if err and not neg_err:
                        neg_err = True
                        neg_failed_cmd = cmd
                        neg_failed_errs = errs
                    if cmd == "commit check":
                        neg_commit_check = output
                if not neg_err and neg_commit_check:
                    err, errs = has_cli_error(neg_commit_check)
                    neg_err = err
                    if err:
                        neg_failed_cmd = "commit check"
                        neg_failed_errs = errs
                results.append(
                    StepResult(
                        name="negative_slm_bad_md_ma",
                        ok=neg_err,
                        details=(
                            f"Expected error; failed on: {neg_failed_cmd}\n" + "\n".join(neg_failed_errs)
                            if neg_err
                            else "No error for bad MD/MA."
                        ),
                        raw_output=neg_commit_check,
                    )
                )
    
            if not abort:
                # Negative: long name/description (expect command error or commit check failure)
                _progress("negative_long_name_desc")
                dm_long_profile = "DM_PROF_" + ("X" * 220)
                neg_long_cmds = (
                    ["configure"]
                    + build_dm_profile_commands(dm_long_profile)
                    + exit_profiles_to_cfg_root()
                    + build_commands(
                        args.long_name,
                        dm_long_profile,
                        args.md,
                        args.ma,
                        args.mep_id,
                        args.target,
                        args.long_desc,
                        getattr(args, "mep_direction", None),
                    )
                    + ["commit check"]
                    + teardown_dm_session_and_profile(args.long_name, dm_long_profile)
                )
                cmd_outputs = run_shell_sequence_detailed(client, neg_long_cmds, timeout=60)
                neg_err = False
                neg_failed_cmd = None
                neg_failed_errs = []
                neg_commit_check = ""
                for cmd, output in cmd_outputs:
                    raw_outputs.append(f"## CMD: {cmd}\n{output}")
                    err, errs = has_cli_error(output)
                    if err and not neg_err:
                        neg_err = True
                        neg_failed_cmd = cmd
                        neg_failed_errs = errs
                    if cmd == "commit check":
                        neg_commit_check = output
                if not neg_err and neg_commit_check:
                    err, errs = has_cli_error(neg_commit_check)
                    neg_err = err
                    if err:
                        neg_failed_cmd = "commit check"
                        neg_failed_errs = errs
                results.append(
                    StepResult(
                        name="negative_long_name_desc",
                        ok=neg_err,
                        details=(
                            f"Expected error; failed on: {neg_failed_cmd}\n"
                            + "\n".join(neg_failed_errs)
                            if neg_err
                            else "No error for long name/desc."
                        ),
                        raw_output=neg_commit_check,
                    )
                )
    
            if not abort:
                # Negative: bad MD/MA (expect command error or commit check failure)
                _progress("negative_bad_md_ma")
                neg_profile_bad = f"{args.profile}_BAD"
                neg_bad_cmds = (
                    ["configure"]
                    + build_dm_profile_commands(neg_profile_bad)
                    + exit_profiles_to_cfg_root()
                    + build_commands(
                        f"{args.session}_BAD",
                        neg_profile_bad,
                        args.bad_md,
                        args.bad_ma,
                        args.mep_id,
                        args.target,
                        args.description,
                        getattr(args, "mep_direction", None),
                    )
                    + ["commit check"]
                    + teardown_dm_session_and_profile(f"{args.session}_BAD", neg_profile_bad)
                )
                cmd_outputs = run_shell_sequence_detailed(client, neg_bad_cmds, timeout=60)
                neg_err = False
                neg_failed_cmd = None
                neg_failed_errs = []
                neg_commit_check = ""
                for cmd, output in cmd_outputs:
                    raw_outputs.append(f"## CMD: {cmd}\n{output}")
                    err, errs = has_cli_error(output)
                    if err and not neg_err:
                        neg_err = True
                        neg_failed_cmd = cmd
                        neg_failed_errs = errs
                    if cmd == "commit check":
                        neg_commit_check = output
                if not neg_err and neg_commit_check:
                    err, errs = has_cli_error(neg_commit_check)
                    neg_err = err
                    if err:
                        neg_failed_cmd = "commit check"
                        neg_failed_errs = errs
                results.append(
                    StepResult(
                        name="negative_bad_md_ma",
                        ok=neg_err,
                        details=(
                            f"Expected error; failed on: {neg_failed_cmd}\n"
                            + "\n".join(neg_failed_errs)
                            if neg_err
                            else "No error for bad MD/MA."
                        ),
                        raw_output=neg_commit_check,
                    )
                )
    
            if not abort:
                # Negative: non-numeric fields (expect command error or commit check failure)
                # Covers "unnumerical" (e.g., mep-id must be numeric).
                _progress("negative_non_numeric")
                neg_profile_nonnum = f"{args.profile}_NONNUM"
                neg_sess_nonnum = f"{args.session}_NONNUM"
                neg_nonnum_cmds = (
                    ["configure"]
                    + build_dm_profile_commands(neg_profile_nonnum)
                    + exit_profiles_to_cfg_root()
                    + [
                        f"services performance-monitoring cfm two-way-delay-measurement {neg_sess_nonnum}",
                        f"profile {neg_profile_nonnum}",
                        "admin-state enabled",
                        f"description {args.description}",
                        # Non-numeric mep-id (direction still applied when set for consistency)
                        _source_line(args.md, args.ma, "abc", getattr(args, "mep_direction", None)),
                        # Non-numeric target mep-id
                        "target mep-id abc",
                        "exit",
                    ]
                    + ["commit check"]
                    + teardown_dm_session_and_profile(neg_sess_nonnum, neg_profile_nonnum, from_session_context=False)
                )
                cmd_outputs = run_shell_sequence_detailed(client, neg_nonnum_cmds, timeout=60)
                neg_err = False
                neg_failed_cmd = None
                neg_failed_errs = []
                neg_commit_check = ""
                for cmd, output in cmd_outputs:
                    raw_outputs.append(f"## CMD: {cmd}\n{output}")
                    err, errs = has_cli_error(output)
                    if err and not neg_err:
                        neg_err = True
                        neg_failed_cmd = cmd
                        neg_failed_errs = errs
                    if cmd == "commit check":
                        neg_commit_check = output
                if not neg_err and neg_commit_check:
                    err, errs = has_cli_error(neg_commit_check)
                    neg_err = err
                    if err:
                        neg_failed_cmd = "commit check"
                        neg_failed_errs = errs
                results.append(
                    StepResult(
                        name="negative_non_numeric",
                        ok=neg_err,
                        details=(
                            f"Expected error; failed on: {neg_failed_cmd}\n"
                            + "\n".join(neg_failed_errs)
                            if neg_err
                            else "No error for non-numeric inputs."
                        ),
                        raw_output=neg_commit_check,
                    )
                )
    
            if not abort:
                # Negative: invalid PCP (e.g. PCP 8 out of range 0-7; expect error)
                _progress("negative_slm_invalid_pcp")
                neg_pcp_profile = f"{args.slm_profile}_BADPCP"
                neg_pcp_cmds = (
                    ["configure"]
                    + ["services performance-monitoring profiles cfm two-way-synthetic-loss-measurement " + neg_pcp_profile, "pcp 8", "inform-test-results enabled", "test-duration probes probe-count 5 probe-interval 1 repeat-interval 10", "thresholds near-end-loss 1", "thresholds far-end-loss 1", "exit"]
                    + exit_profiles_to_cfg_root()
                    + ["commit check"]
                    + [f"no services performance-monitoring profiles cfm two-way-synthetic-loss-measurement {neg_pcp_profile}", "exit"]
                )
                cmd_outputs = run_shell_sequence_detailed(client, neg_pcp_cmds, timeout=60)
                neg_err = False
                neg_failed_cmd = None
                neg_failed_errs = []
                for cmd, output in cmd_outputs:
                    raw_outputs.append(f"## CMD: {cmd}\n{output}")
                    err, errs = has_cli_error(output)
                    if err and not neg_err:
                        neg_err = True
                        neg_failed_cmd = cmd
                        neg_failed_errs = errs
                results.append(
                    StepResult(
                        name="negative_slm_invalid_pcp",
                        ok=neg_err,
                        details=(
                            f"Expected error; failed on: {neg_failed_cmd}\n" + "\n".join(neg_failed_errs)
                            if neg_err
                            else "No error for invalid PCP 8."
                        ),
                    )
                )
    
            if not abort:
                # Negative: invalid timer (probe-count 0; expect error)
                _progress("negative_dm_invalid_timer")
                neg_timer_profile = f"{args.profile}_BADTIMER"
                neg_timer_cmds = (
                    ["configure"]
                    + ["services performance-monitoring profiles cfm two-way-delay-measurement " + neg_timer_profile, "inform-test-results enabled", "test-duration probes probe-count 0 probe-interval 1 repeat-interval 10", "thresholds delay-rtt-min 100", "exit"]
                    + exit_profiles_to_cfg_root()
                    + ["commit check"]
                    + [f"no services performance-monitoring profiles cfm two-way-delay-measurement {neg_timer_profile}", "exit"]
                )
                cmd_outputs = run_shell_sequence_detailed(client, neg_timer_cmds, timeout=60)
                neg_err = False
                neg_failed_cmd = None
                neg_failed_errs = []
                for cmd, output in cmd_outputs:
                    raw_outputs.append(f"## CMD: {cmd}\n{output}")
                    err, errs = has_cli_error(output)
                    if err and not neg_err:
                        neg_err = True
                        neg_failed_cmd = cmd
                        neg_failed_errs = errs
                results.append(
                    StepResult(
                        name="negative_dm_invalid_timer",
                        ok=neg_err,
                        details=(
                            f"Expected error; failed on: {neg_failed_cmd}\n" + "\n".join(neg_failed_errs)
                            if neg_err
                            else "No error for probe-count 0."
                        ),
                    )
                )
    
            if args.cleanup and not cleanup_done:
                _progress("cleanup")
                ok, detail = cleanup_config(
                    args.host,
                    args.user,
                    args.password,
                    args.session,
                    args.profile,
                    slm_session=args.slm_session,
                    slm_profile=args.slm_profile,
                )
                results.append(
                    StepResult(
                        name="cleanup",
                        ok=ok,
                        details=detail,
                    )
                )
            if iteration is not None and mep_list:
                for r in results[result_count_before:]:
                    r.name = f"[MEP {args.mep_id}] " + r.name
    finally:
        client.close()

    if args.output_file:
        with open(args.output_file, "w", encoding="utf-8") as handle:
            handle.write("\n\n".join(raw_outputs))

    if args.show_cli_output:
        print("\n=== RAW DEVICE OUTPUT ===")
        print("\n\n".join(raw_outputs))

    def _print_table(rows: List[StepResult], show_details: bool) -> None:
        name_w = max([len("Test"), *[len(r.name) for r in rows]]) if rows else len("Test")
        status_w = len("Status")
        # Always show details column if there are any failures
        has_failures = any(not r.ok for r in rows)
        show_details_col = show_details or has_failures
        details_w = (
            max([len("Details"), *[len(r.details) for r in rows]]) if rows else len("Details")
        )

        def sep() -> str:
            if show_details_col:
                return f"+-{'-' * name_w}-+-{'-' * status_w}-+-{'-' * details_w}-+"
            return f"+-{'-' * name_w}-+-{'-' * status_w}-+"

        def header() -> str:
            if show_details_col:
                return f"| {'Test'.ljust(name_w)} | {'Status'.ljust(status_w)} | {'Details'.ljust(details_w)} |"
            return f"| {'Test'.ljust(name_w)} | {'Status'.ljust(status_w)} |"

        print(sep())
        print(header())
        print(sep())
        for r in rows:
            status = "PASS" if r.ok else "FAIL"
            if show_details_col:
                # Show details for all tests if --show-details, or only for failures
                details_to_show = r.details if (show_details or not r.ok) else ""
                print(
                    f"| {r.name.ljust(name_w)} | {status.ljust(status_w)} | {details_to_show.ljust(details_w)} |"
                )
            else:
                print(f"| {r.name.ljust(name_w)} | {status.ljust(status_w)} |")
        print(sep())

    def _bucket_result(r: StepResult) -> str:
        # Split results into DM vs SLM vs on-demand vs other.
        n = r.name.lower()
        if n.startswith("on_demand_"):
            return "on_demand"
        if (
            "two-way-synthetic-loss-measurement" in n
            or n.startswith("configure_slm")
            or n.startswith("commit_slm")
            or n.startswith("negative_slm_")
            or n.startswith("sw235372_slm_")
            or n.startswith("verify_slm")
            or n.startswith("show_slm")
        ):
            return "slm"
        if (
            "two-way-delay-measurement" in n
            or n.startswith("configure_dm")
            or n == "commit"
            or n.startswith("negative_")
            or n.startswith("sw235372_dm_")
            or n.startswith("verify_dm")
            or n.startswith("show_dm")
        ):
            return "dm"
        return "other"

    failed = any(not r.ok for r in results)
    if args.output_format == "table":
        dm_rows = [r for r in results if _bucket_result(r) == "dm"]
        slm_rows = [r for r in results if _bucket_result(r) == "slm"]
        on_demand_rows = [r for r in results if _bucket_result(r) == "on_demand"]
        other_rows = [r for r in results if _bucket_result(r) == "other"]

        if dm_rows:
            print("DM results")
            _print_table(dm_rows, show_details=args.show_details)
        if slm_rows:
            print("SLM results")
            _print_table(slm_rows, show_details=args.show_details)
        if on_demand_rows:
            print("On-Demand results")
            _print_table(on_demand_rows, show_details=args.show_details)
        if other_rows:
            print("Other results")
            _print_table(other_rows, show_details=args.show_details)
    else:
        for result in results:
            status = "PASS" if result.ok else "FAIL"
            print(f"{status}: {result.name}")
            if args.show_details and result.details:
                print(f"  {result.details}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
