#!/usr/bin/env python3
import argparse
import getpass
import re
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

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
            r"(Error:|ERROR:|Unknown command|Invalid command|Commit check failed|Commit failed|Command failed)",
            line,
        ):
            stripped = line.strip()
            # Redact very long "Invalid value '<...>'" strings.
            m = re.search(r"(Invalid value ')([^']+)(')", stripped)
            if m and len(m.group(2)) > 64:
                stripped = stripped[: m.start(2)] + "<redacted>" + stripped[m.end(2) :]
            errors.append(stripped)
    return (len(errors) > 0, errors)


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
        "thresholds delay-rtt-avg 1000",
        "thresholds jitter-rtt-avg 500",
        "thresholds success-rate 90",
        "exit",
    ]


def exit_profiles_to_cfg_root() -> List[str]:
    # After creating a profile, we land under cfg-pm-profiles-cfm.
    # Exit back to (cfg)# so session commands are accepted.
    return ["exit", "exit", "exit", "exit"]


def run_shell_with_prompt(client: paramiko.SSHClient, command: str, timeout: int = 30) -> str:
    channel = client.invoke_shell()
    channel.settimeout(timeout)
    banner = _read_until_prompt(channel, prompt=None, timeout=timeout, quiet=1)
    channel.send(command + "\n")
    # Don't lock to a specific prompt string because the prompt changes between
    # operational/config/submodes. Just wait until we see any prompt marker again.
    output = _read_until_prompt(channel, prompt=None, timeout=timeout, quiet=2)
    channel.close()
    return redact_text(banner + output)


def run_shell_sequence(client: paramiko.SSHClient, commands: List[str], timeout: int = 30) -> str:
    channel = client.invoke_shell()
    channel.settimeout(timeout)
    banner = _read_until_prompt(channel, prompt=None, timeout=timeout, quiet=1)
    output = banner
    for cmd in commands:
        channel.send(cmd + "\n")
        output += _read_until_prompt(channel, prompt=None, timeout=timeout, quiet=2)
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
        output = _read_until_prompt(channel, prompt=None, timeout=timeout, quiet=2)
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
) -> Tuple[bool, str]:
    # Use a fresh connection for cleanup (more reliable), but keep one shell
    # so "configure" applies to subsequent commands.
    # Always start from a clean candidate to avoid unrelated leftover errors
    # causing the cleanup commit to fail.
    commands = [
        "configure",
        "rollback 0",
        f"no services performance-monitoring cfm two-way-delay-measurement {session}",
        f"no services performance-monitoring profiles cfm two-way-delay-measurement {profile}",
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Y.1731 DM CLI + TAB validation")
    parser.add_argument("--host", help="Device hostname or IP")
    parser.add_argument("--user", default="dnroot")
    parser.add_argument("--password", default="dnroot")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--session", default="DM_CLI_TAB")
    parser.add_argument("--profile", default="DM_PROF_CLI")
    parser.add_argument("--md", default="MD-CUST")
    parser.add_argument("--ma", default="MA-CUST")
    parser.add_argument("--mep-id", default="1")
    parser.add_argument("--target", default="mep-id 2")
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
        "--cleanup",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Remove created session/profile at end (default: true)",
    )
    args = parser.parse_args()

    args.host = _prompt_if_missing(args.host, "Device hostname or IP: ")
    args.user = _prompt_if_missing(args.user, "Username [dnroot]: ") or "dnroot"
    if args.password == "dnroot":
        args.password = _prompt_if_missing(args.password, "Password [dnroot]: ", secret=True) or "dnroot"
    else:
        args.password = _prompt_if_missing(args.password, "Password: ", secret=True)

    results: List[StepResult] = []
    raw_outputs: List[str] = []
    client = create_ssh_client(args.host, args.user, args.password, args.timeout)
    try:
        abort = False
        cleanup_done = False
        # TAB completion checks
        tab_prefixes = [
            "services performance-monitoring cfm two-way-delay-measurement ",
            f"services performance-monitoring cfm two-way-delay-measurement {args.session} ",
            f"services performance-monitoring cfm two-way-delay-measurement {args.session} description ",
        ]
        for prefix in tab_prefixes:
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

        # Base config commands: create profile, create session, commit
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
                    args.host, args.user, args.password, args.session, args.profile
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

        if not abort:
            # Negative: long name/description (expect command error or commit check failure)
            neg_profile_long = f"{args.profile}_LONG"
            neg_long_cmds = (
                ["configure"]
                + build_dm_profile_commands(neg_profile_long)
                + exit_profiles_to_cfg_root()
                + build_commands(
                    args.long_name,
                    neg_profile_long,
                    args.md,
                    args.ma,
                    args.mep_id,
                    args.target,
                    args.long_desc,
                )
                + ["commit check", "rollback 0", "exit", "exit"]
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
                + ["commit check", "rollback 0", "exit", "exit"]
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
                + ["commit check", "rollback 0", "exit", "exit"]
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
            ok, detail = cleanup_config(
                args.host, args.user, args.password, args.session, args.profile
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

    failed = False
    for result in results:
        status = "PASS" if result.ok else "FAIL"
        print(f"{status}: {result.name}")
        if result.details:
            print(f"  {result.details}")
        if not result.ok:
            failed = True

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
