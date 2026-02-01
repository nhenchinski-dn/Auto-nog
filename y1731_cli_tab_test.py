#!/usr/bin/env python3
"""
Y.1731 DM/SLM CLI and TAB completion test (SW-235373, SW-235927, SW-235372).
This script does not use 'rollback 0': discovery, validation, cleanup, and
commit-check sequences tear down only the PM sessions/profiles they create,
so your candidate config (e.g. services ethernet-oam connectivity-fault-management)
is preserved.
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


def _read_until_prompt(channel, prompt: Optional[str], timeout: int, quiet: float = 2.0) -> str:
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
    """
    output = _read_until_prompt(channel, prompt=prompt, timeout=timeout, quiet=2)
    output += _read_until_quiet(channel, timeout=timeout, quiet=0.8)
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
    """
    show_cmds = [
        "show config services performance-monitoring | display-set",
        "show configuration services performance-monitoring | display-set",
        "show config services performance-monitoring",
        "show configuration services performance-monitoring",
    ]
    used, out = _first_successful_show(client, show_cmds, timeout=timeout)
    if not used:
        return False, "Unable to run show config for services performance-monitoring."
    if match_text in out:
        return True, f"Found '{match_text}' in '{used}'."
    # Include a tiny snippet for debugging
    sample = "\n".join(out.splitlines()[:40]).strip()
    return False, f"Did not find '{match_text}' in '{used}'.\n--- Output sample ---\n{sample}"


def discover_cfm_context(
    client: paramiko.SSHClient, timeout: int = 30
) -> Tuple[bool, str, Optional[str], Optional[str], Optional[str], Optional[str]]:
    """
    Discover (md, ma, local_mep_id, target_mep_id) from existing
    'services ethernet-oam connectivity-fault-management' config on the device.

    Returns: (ok, details, md, ma, mep_id, target_str)
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
    for cmd in show_cmds:
        out = run_shell_with_prompt(client, cmd, timeout=timeout)
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
        )

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
            candidates[key] = {"meps": set(), "remote_meps": set()}

        # Remote MEP IDs frequently appear under the same md/ma context; don't treat them
        # as local MEP IDs (otherwise we might pick a remote MEP as the "source" MEP).
        is_remote_line = bool(remote_mep_re.search(line)) or ("remote-mep" in line.lower()) or ("remote_mep" in line.lower())

        for m in remote_mep_re.finditer(line):
            candidates[key]["remote_meps"].add(int(m.group(1)))
        if is_remote_line:
            continue

        for m in mep_id_re.finditer(line):
            candidates[key]["meps"].add(int(m.group(1)))
        # Some configs may use "mep <ID>".
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
    details = f"Discovered CFM context from '{used}': md={md} ma={ma}" + (
        f" mep-id={local_mep}" if local_mep else " mep-id=<not-found>"
    ) + (f" target={target_str}" if target_str else " target=<not-found>")
    return True, details, md, ma, local_mep, target_str


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
    client: paramiko.SSHClient, md: str, ma: str, timeout: int = 20
) -> List[int]:
    """
    Ask the device (via TAB completion) what source MEP IDs are valid for the given md/ma.
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
    client: paramiko.SSHClient, md: str, ma: str, mep_id: str, timeout: int = 20
) -> Tuple[bool, str]:
    """
    Best-effort validation that the PM CLI accepts the given source mep-id
    (helps avoid using remote-mep IDs as local source MEP).
    """
    tmp_session = "__DISC_DM_SRC_VALIDATE__"
    cmd = f"source maintenance-domain {md} maintenance-association {ma} mep-id {mep_id}"
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


def build_commands(
    session: str,
    profile: str,
    md: str,
    ma: str,
    mep_id: str,
    target: str,
    description: str,
) -> List[str]:
    return [
        f"services performance-monitoring cfm two-way-delay-measurement {session}",
        f"profile {profile}",
        "admin-state enabled",
        f"description {description}",
        f"source maintenance-domain {md} maintenance-association {ma} mep-id {mep_id}",
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
) -> List[str]:
    return [
        f"services performance-monitoring cfm two-way-synthetic-loss-measurement {session}",
        f"profile {profile}",
        "admin-state enabled",
        f"description {description}",
        f"source maintenance-domain {md} maintenance-association {ma} mep-id {mep_id}",
        f"target {target}",
        "exit",
    ]


def _run_commit_check_sequence(
    client: paramiko.SSHClient, name: str, commands: List[str], timeout: int = 60
) -> StepResult:
    """
    Run commands in one shell session and evaluate PASS/FAIL based on CLI error detection.
    Intended for SW-235372 CLI coverage checks.
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
    # Only remove the PM sessions/profiles created by this script, then commit.
    commands = [
        "configure",
        f"no services performance-monitoring cfm two-way-delay-measurement {session}",
        f"no services performance-monitoring profiles cfm two-way-delay-measurement {profile}",
        *(  # noqa: C400
            [
                f"no services performance-monitoring cfm two-way-synthetic-loss-measurement {slm_session}",
                f"no services performance-monitoring profiles cfm two-way-synthetic-loss-measurement {slm_profile}",
            ]
            if slm_session and slm_profile
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
            # Include the full device output for troubleshooting.
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
    parser.add_argument("--md", default=None, help="Override maintenance-domain name (otherwise auto-discovered)")
    parser.add_argument("--ma", default=None, help="Override maintenance-association name (otherwise auto-discovered)")
    parser.add_argument("--mep-id", default=None, help="Override local MEP ID (otherwise auto-discovered)")
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

        # Auto-discover MD/MA/MEP/target from existing ethernet-oam CFM config.
        if args.auto_from_cfm:
            _progress("discover_cfm_context")
            ok, detail, md, ma, mep_id, target = discover_cfm_context(client, timeout=30)
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

                # If discovery found MD/MA but couldn't confidently determine MEP/targets,
                # prompt for the missing pieces instead of silently falling back.
                #
                # Also validate that a discovered mep-id is actually accepted by the PM "source ... mep-id <X>"
                # completion list; some devices show remote-meps in the CFM tree and the naive parser can pick those.
                if args.md and args.ma and args.mep_id:
                    candidates = discover_valid_source_mep_ids(client, args.md, args.ma, timeout=20)
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
                        ok_src, why = validate_dm_source_mep_id(client, args.md, args.ma, args.mep_id, timeout=20)
                        if not ok_src:
                            # Try to select a local MEP ID from ethernet-oam CFM config.
                            cfg_meps = discover_local_mep_ids_from_ethernet_oam(client, args.md, args.ma, timeout=30)
                            picked: Optional[int] = None
                            for candidate in cfg_meps:
                                ok_cand, _ = validate_dm_source_mep_id(
                                    client, args.md, args.ma, str(candidate), timeout=20
                                )
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
                    candidates = discover_valid_source_mep_ids(client, args.md, args.ma, timeout=20)
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
                                client, args.md, args.ma, str(candidate), timeout=20
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

        if not abort:
            # TAB completion checks
            tab_prefixes = [
                "services performance-monitoring cfm two-way-delay-measurement ",
                f"services performance-monitoring cfm two-way-delay-measurement {args.session} ",
                f"services performance-monitoring cfm two-way-delay-measurement {args.session} description ",
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
                    args.session, args.profile, args.md, args.ma, args.mep_id, args.target, args.description
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
            results.append(
                StepResult(
                    name="configure_dm_session",
                    ok=not config_failed,
                    details=(
                        f"Failed command: {failed_cmd}\n" + "\n".join(failed_errs)
                        if config_failed
                        else "DM session configured."
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
            results.append(
                StepResult(
                    name="configure_slm_session",
                    ok=not slm_failed,
                    details=(
                        f"Failed command: {slm_failed_cmd}\n" + "\n".join(slm_failed_errs)
                        if slm_failed
                        else "SLM session configured."
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

        if not abort:
            # SW-235372: CLI coverage for DM/SLM profiles + sessions (knobs from issue tree)
            # DM profile duration variants + thresholds
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
                    + ["test-duration time-frame 2 probe-interval 1 repeat-interval 10", "commit check"]
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
                    + ["test-duration time-frame 2 probe-interval 1 repeat-interval 10", "commit check"]
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

            # DM session knobs: admin-state enabled/disabled, description, profile, source, target variants
            _progress("sw235372_dm_session_variants")
            dm_sess_372 = f"{args.session}_SW235372"
            # Use committed base profile (args.profile) so session references an existing profile.
            results.append(
                _run_commit_check_sequence(
                    client,
                    "sw235372_dm_session_target_mep",
                    [
                        "configure",
                        f"services performance-monitoring cfm two-way-delay-measurement {dm_sess_372}",
                        "admin-state enabled",
                        "admin-state disabled",
                        f"description {args.description}",
                        f"profile {args.profile}",
                        f"source maintenance-domain {args.md} maintenance-association {args.ma} mep-id {args.mep_id}",
                        f"target {args.target}",
                        "commit check",
                    ]
                    + teardown_dm_session_commands(dm_sess_372),
                )
            )
            results.append(
                _run_commit_check_sequence(
                    client,
                    "sw235372_dm_session_target_mac",
                    [
                        "configure",
                        f"services performance-monitoring cfm two-way-delay-measurement {dm_sess_372}_MAC",
                        "admin-state enabled",
                        f"description {args.description}",
                        f"profile {args.profile}",
                        f"source maintenance-domain {args.md} maintenance-association {args.ma} mep-id {args.mep_id}",
                        "target mac-address 00:11:22:33:44:55",
                        "commit check",
                    ]
                    + teardown_dm_session_commands(f"{dm_sess_372}_MAC"),
                )
            )

            # SLM session knobs: admin-state enabled/disabled, description, profile, source, target variants
            _progress("sw235372_slm_session_variants")
            slm_sess_372 = f"{args.slm_session}_SW235372"
            # Use committed base profile (args.slm_profile) so session references an existing profile.
            results.append(
                _run_commit_check_sequence(
                    client,
                    "sw235372_slm_session_target_mep",
                    [
                        "configure",
                        f"services performance-monitoring cfm two-way-synthetic-loss-measurement {slm_sess_372}",
                        "admin-state enabled",
                        "admin-state disabled",
                        f"description {args.slm_description}",
                        f"profile {args.slm_profile}",
                        f"source maintenance-domain {args.md} maintenance-association {args.ma} mep-id {args.mep_id}",
                        f"target {args.slm_target}",
                        "commit check",
                    ]
                    + teardown_slm_session_commands(slm_sess_372),
                )
            )
            results.append(
                _run_commit_check_sequence(
                    client,
                    "sw235372_slm_session_target_mac",
                    [
                        "configure",
                        f"services performance-monitoring cfm two-way-synthetic-loss-measurement {slm_sess_372}_MAC",
                        "admin-state enabled",
                        f"description {args.slm_description}",
                        f"profile {args.slm_profile}",
                        f"source maintenance-domain {args.md} maintenance-association {args.ma} mep-id {args.mep_id}",
                        "target mac-address 00:11:22:33:44:55",
                        "commit check",
                    ]
                    + teardown_slm_session_commands(f"{slm_sess_372}_MAC"),
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
                    # Non-numeric mep-id
                    f"source maintenance-domain {args.md} maintenance-association {args.ma} mep-id abc",
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
        details_w = (
            max([len("Details"), *[len(r.details) for r in rows]]) if rows else len("Details")
        )

        def sep() -> str:
            if show_details:
                return f"+-{'-' * name_w}-+-{'-' * status_w}-+-{'-' * details_w}-+"
            return f"+-{'-' * name_w}-+-{'-' * status_w}-+"

        def header() -> str:
            if show_details:
                return f"| {'Test'.ljust(name_w)} | {'Status'.ljust(status_w)} | {'Details'.ljust(details_w)} |"
            return f"| {'Test'.ljust(name_w)} | {'Status'.ljust(status_w)} |"

        print(sep())
        print(header())
        print(sep())
        for r in rows:
            status = "PASS" if r.ok else "FAIL"
            if show_details:
                print(
                    f"| {r.name.ljust(name_w)} | {status.ljust(status_w)} | {r.details.ljust(details_w)} |"
                )
            else:
                print(f"| {r.name.ljust(name_w)} | {status.ljust(status_w)} |")
        print(sep())

    def _bucket_result(r: StepResult) -> str:
        # Split results into DM vs SLM vs other.
        n = r.name.lower()
        if (
            "two-way-synthetic-loss-measurement" in n
            or n.startswith("configure_slm")
            or n.startswith("commit_slm")
            or n.startswith("negative_slm_")
            or n.startswith("sw235372_slm_")
        ):
            return "slm"
        if "two-way-delay-measurement" in n or n.startswith("configure_dm") or n == "commit" or n.startswith(
            "negative_"
        ) or n.startswith("sw235372_dm_"):
            return "dm"
        return "other"

    failed = any(not r.ok for r in results)
    if args.output_format == "table":
        dm_rows = [r for r in results if _bucket_result(r) == "dm"]
        slm_rows = [r for r in results if _bucket_result(r) == "slm"]
        other_rows = [r for r in results if _bucket_result(r) == "other"]

        if dm_rows:
            print("DM results")
            _print_table(dm_rows, show_details=args.show_details)
        if slm_rows:
            print("SLM results")
            _print_table(slm_rows, show_details=args.show_details)
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
