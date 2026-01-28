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
        if re.search(
            r"\b(Error:|Unknown command|Invalid command|ERROR:|Commit check failed|Commit failed)\b",
            line,
        ):
            errors.append(line.strip())
    return (len(errors) > 0, errors)


def run_shell_with_prompt(client: paramiko.SSHClient, command: str, timeout: int = 30) -> str:
    channel = client.invoke_shell()
    channel.settimeout(timeout)
    banner = _read_until_prompt(channel, prompt=None, timeout=timeout, quiet=1)
    prompt = _extract_prompt(banner)
    channel.send(command + "\n")
    output = _read_until_prompt(channel, prompt=prompt, timeout=timeout, quiet=2)
    channel.close()
    return banner + output


def run_shell_sequence(client: paramiko.SSHClient, commands: List[str], timeout: int = 30) -> str:
    channel = client.invoke_shell()
    channel.settimeout(timeout)
    banner = _read_until_prompt(channel, prompt=None, timeout=timeout, quiet=1)
    prompt = _extract_prompt(banner)
    output = banner
    for cmd in commands:
        channel.send(cmd + "\n")
        output += _read_until_prompt(channel, prompt=prompt, timeout=timeout, quiet=2)
    channel.close()
    return output


def run_shell_sequence_detailed(
    client: paramiko.SSHClient, commands: List[str], timeout: int = 30
) -> List[Tuple[str, str]]:
    channel = client.invoke_shell()
    channel.settimeout(timeout)
    banner = _read_until_prompt(channel, prompt=None, timeout=timeout, quiet=1)
    prompt = _extract_prompt(banner)
    results: List[Tuple[str, str]] = []
    for cmd in commands:
        channel.send(cmd + "\n")
        output = _read_until_prompt(channel, prompt=prompt, timeout=timeout, quiet=2)
        results.append((cmd, banner + output))
    channel.close()
    return results


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
    return banner + output


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
        "configure terminal",
        f"services performance-monitoring cfm two-way-delay-measurement {session}",
        f"profile {profile}",
        "admin-state enabled",
        f"description {description}",
        f"source maintenance-domain {md} maintenance-association {ma} mep-id {mep_id}",
        f"target {target}",
        "exit",
    ]


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


def cleanup_config(client: paramiko.SSHClient, session: str, profile: str) -> Tuple[bool, str]:
    commands = [
        "configure terminal",
        f"no services performance-monitoring cfm two-way-delay-measurement {session}",
        f"no services performance-monitoring profiles cfm two-way-delay-measurement {profile}",
        "exit",
    ]
    cmd_outputs = run_shell_sequence_detailed(client, commands, timeout=60)
    for cmd, output in cmd_outputs:
        err, errs = has_cli_error(output)
        if err:
            return False, f"Failed cleanup command: {cmd}\n" + "\n".join(errs)
    commit_output = run_commit(client)
    err, errs = has_cli_error(commit_output)
    if err:
        return False, "\n".join(errs)
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
    parser.add_argument("--md", default="MD1")
    parser.add_argument("--ma", default="MA1")
    parser.add_argument("--mep-id", default="1")
    parser.add_argument("--target", default="mep-id 2")
    parser.add_argument("--description", default="cli_tab_test")
    parser.add_argument("--long-name", default="DM_CLI_TAB_LONG_NAME_1234567890")
    parser.add_argument("--long-desc", default="desc_" + "x" * 64)
    parser.add_argument("--bad-md", default="MD_BAD")
    parser.add_argument("--bad-ma", default="MA_BAD")
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
    client = create_ssh_client(args.host, args.user, args.password, args.timeout)
    try:
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
                )
            )

        # Base config commands (capture failing command)
        cmd_outputs = run_shell_sequence_detailed(
            client,
            build_commands(
                args.session, args.profile, args.md, args.ma, args.mep_id, args.target, args.description
            ),
            timeout=60,
        )
        config_failed = False
        failed_cmd = None
        failed_errs: List[str] = []
        for cmd, output in cmd_outputs:
            err, errs = has_cli_error(output)
            if err:
                config_failed = True
                failed_cmd = cmd
                failed_errs = errs
                break
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

        # Commit (ensure commit works)
        commit_output = run_commit(client)
        err, errs = has_cli_error(commit_output)
        results.append(
            StepResult(
                name="commit",
                ok=not err,
                details="\n".join(errs) if err else "Commit OK.",
            )
        )

        # Negative: long name/description (expect commit check failure)
        cmd_outputs = run_shell_sequence_detailed(
            client,
            build_commands(
                args.long_name,
                args.profile,
                args.md,
                args.ma,
                args.mep_id,
                args.target,
                args.long_desc,
            ),
            timeout=60,
        )
        commit_output = run_commit_check(client)
        err, errs = has_cli_error(commit_output)
        results.append(
            StepResult(
                name="negative_long_name_desc",
                ok=err,
                details=(
                    "Expected commit check error on long name/desc."
                    if err
                    else "Commit check did not fail for long name/desc."
                ),
            )
        )
        run_rollback(client)

        # Negative: bad MD/MA (expect commit check failure)
        cmd_outputs = run_shell_sequence_detailed(
            client,
            build_commands(
                f"{args.session}_BAD",
                args.profile,
                args.bad_md,
                args.bad_ma,
                args.mep_id,
                args.target,
                args.description,
            ),
            timeout=60,
        )
        commit_output = run_commit_check(client)
        err, errs = has_cli_error(commit_output)
        results.append(
            StepResult(
                name="negative_bad_md_ma",
                ok=err,
                details=(
                    "Expected commit check error on bad MD/MA."
                    if err
                    else "Commit check did not fail for bad MD/MA."
                ),
            )
        )
        run_rollback(client)

        if args.cleanup:
            ok, detail = cleanup_config(client, args.session, args.profile)
            results.append(
                StepResult(
                    name="cleanup",
                    ok=ok,
                    details=detail,
                )
            )
    finally:
        client.close()

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
