#!/usr/bin/env python3
import argparse
import paramiko
import random
import re
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional


COMMANDS = [
    ("lldp_neighbors", "show lldp neighbors"),
    ("lacp_interfaces", "show lacp interfaces"),
    ("lacp_counters", "show lacp counters"),
]


@dataclass
class SectionResult:
    name: str
    ok: bool
    warnings: List[str]
    errors: List[str]


def create_ssh_client(host: str, user: str, password: str, timeout: int):
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


ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _read_for_quiet(
    channel,
    quiet=1.5,
    max_duration=10,
    auto_confirm=False,
    verbose=False,
):
    output = ""
    start = time.time()
    last_data = time.time()
    while True:
        if time.time() - start > max_duration:
            break
        try:
            if channel.recv_ready():
                chunk = channel.recv(4096).decode(errors="ignore")
                output += chunk
                last_data = time.time()
                clean = ANSI_ESCAPE.sub("", output)
                lower = clean.lower()
                if auto_confirm and "yes/no" in lower:
                    if verbose:
                        print("Auto-confirm: yes")
                    channel.send("yes\n")
                if "what would you like to do (check-and-merge, merge-only, abort)" in lower:
                    if verbose:
                        print("Auto-confirm: merge-only")
                    channel.send("merge-only\n")
                if "enter yes to continue" in lower:
                    if verbose:
                        print("Auto-confirm: yes")
                    channel.send("yes\n")
                if "--more--" in lower:
                    channel.send(" ")
            else:
                if time.time() - last_data > quiet:
                    break
                time.sleep(0.2)
        except Exception:
            break
    return output


def run_shell_commands(
    host: str,
    user: str,
    password: str,
    timeout: int,
    commands: List[str],
    verbose: bool = False,
    inter_command_delay: float = 0.0,
) -> str:
    client = create_ssh_client(host, user, password, timeout)
    channel = client.invoke_shell()
    channel.settimeout(timeout)
    try:
        banner = _read_for_quiet(channel, quiet=1, max_duration=timeout)
        output = banner
        for cmd in commands:
            if verbose:
                print(f"Running: {cmd}")
            try:
                channel.send(cmd + "\n")
            except Exception as exc:
                raise RuntimeError(str(exc)) from exc
            if inter_command_delay:
                time.sleep(inter_command_delay)
            if cmd in ("commit", "commit and-exit"):
                max_duration = 120
            elif cmd in ("configure", "interfaces", "interface", "protocols", "lacp"):
                max_duration = 30
            else:
                max_duration = 20
            output += _read_for_quiet(
                channel,
                quiet=1.5,
                max_duration=max_duration,
                auto_confirm=(cmd in ("commit", "commit and-exit")),
                verbose=verbose,
            )
        return output
    finally:
        try:
            channel.close()
        finally:
            client.close()


def run_exec_commands(
    host: str,
    user: str,
    password: str,
    timeout: int,
    commands: List[Tuple[str, str]],
) -> Dict[str, str]:
    client = create_ssh_client(host, user, password, timeout)
    results: Dict[str, str] = {}
    try:
        for name, cmd in commands:
            stdin, stdout, stderr = client.exec_command(
                cmd, timeout=timeout, get_pty=True
            )
            output = (stdout.read().decode() or "") + (stderr.read().decode() or "")
            results[name] = output.strip()
    finally:
        client.close()
    return results


def run_shell_sequence(
    host: str,
    user: str,
    password: str,
    timeout: int,
    commands: List[Tuple[str, str]],
    verbose: bool = False,
) -> Dict[str, str]:
    client = create_ssh_client(host, user, password, timeout)
    channel = client.invoke_shell()
    channel.settimeout(timeout)
    try:
        _read_for_quiet(channel, quiet=1, max_duration=timeout)
        results: Dict[str, str] = {}
        for name, cmd in commands:
            if verbose:
                print(f"Running show: {cmd}")
            channel.send(cmd + "\n")
            output = _read_for_quiet(
                channel, quiet=1.5, max_duration=20, auto_confirm=True, verbose=verbose
            )
            results[name] = output.strip()
        return results
    finally:
        try:
            channel.close()
        finally:
            client.close()


def run_shell_block(
    host: str,
    user: str,
    password: str,
    timeout: int,
    block: str,
    verbose: bool = False,
) -> str:
    client = create_ssh_client(host, user, password, timeout)
    channel = client.invoke_shell()
    channel.settimeout(timeout)
    try:
        banner = _read_for_quiet(channel, quiet=1, max_duration=timeout)
        if verbose:
            print("Sending config block...")
        channel.send(block + "\n")
        output = banner + _read_for_quiet(
            channel,
            quiet=2,
            max_duration=max(180, timeout),
            auto_confirm=True,
            verbose=verbose,
        )
        return output
    finally:
        try:
            channel.close()
        finally:
            client.close()


def has_cli_error(text: str) -> Tuple[bool, List[str]]:
    errors = []
    for line in text.splitlines():
        if re.search(
            r"\b(Error:|Unknown command|Invalid command|ERROR:|Commit check failed|Commit failed)\b",
            line,
        ):
            errors.append(line.strip())
    return (len(errors) > 0, errors)




def check_lacp_interfaces(text: str) -> SectionResult:
    warnings = []
    errors = []
    has_error, err_lines = has_cli_error(text)
    if has_error:
        errors += err_lines

    if "Aggregate Interface:" not in text:
        errors.append("Missing 'Aggregate Interface' section.")
    if "Port State" not in text or "Protocol State" not in text:
        warnings.append("Missing expected LACP table headers.")

    ok = len(errors) == 0
    return SectionResult("lacp_interfaces", ok, warnings, errors)


def check_lacp_counters(text: str) -> SectionResult:
    warnings = []
    errors = []
    has_error, err_lines = has_cli_error(text)
    if has_error:
        errors += err_lines

    if "Aggregate Interface:" not in text:
        errors.append("Missing 'Aggregate Interface' section.")
    if "LACPDU rx" not in text or "Errors" not in text:
        warnings.append("Missing expected counters headers.")

    # Try to parse the Errors column.
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|") or line.startswith("|-"):
            continue
        parts = [p.strip() for p in line.strip("|").split("|")]
        if len(parts) < 7:
            continue
        interface = parts[0]
        if interface.lower() in ("interface", ""):
            continue
        errors_col = parts[6]
        if errors_col.isdigit() and int(errors_col) > 0:
            errors.append(f"LACP errors on {interface}: {errors_col}")
    ok = len(errors) == 0
    return SectionResult("lacp_counters", ok, warnings, errors)


def check_intf_drops(text: str) -> SectionResult:
    warnings = []
    errors = []
    has_error, err_lines = has_cli_error(text)
    if has_error:
        errors += err_lines

    if "| Interface" not in text and "Interface" not in text:
        warnings.append("Missing interface drops table.")

    # Try to parse RX/TX drops columns; non-zero drops fail.
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|") or line.startswith("|-"):
            continue
        parts = [p.strip() for p in line.strip("|").split("|")]
        if len(parts) < 8:
            continue
        interface = parts[0]
        if interface.lower() in ("interface", ""):
            continue
        rx_drops = parts[6]
        tx_drops = parts[7]
        if rx_drops.isdigit() and int(rx_drops) > 0:
            errors.append(f"RX drops on {interface}: {rx_drops}")
        if tx_drops.isdigit() and int(tx_drops) > 0:
            errors.append(f"TX drops on {interface}: {tx_drops}")
    ok = len(errors) == 0
    return SectionResult("intf_drops", ok, warnings, errors)


def prompt_if_missing(value: str, prompt: str) -> str:
    if value:
        return value
    return input(prompt).strip()


def prompt_int(label: str, min_value: int, max_value: int) -> int:
    while True:
        raw = input(label).strip()
        try:
            value = int(raw)
        except ValueError:
            print(f"Invalid number: {raw}")
            continue
        if min_value <= value <= max_value:
            return value
        print(f"Value must be between {min_value} and {max_value}")


def _validate_members(members: List[str]) -> List[str]:
    pattern = re.compile(r"^ge\d+-\d+/\d+/\d+$")
    return [m for m in members if not pattern.match(m)]


def prompt_interface(prompt: str) -> str:
    while True:
        raw = input(prompt).strip()
        invalid = _validate_members([raw])
        if not invalid:
            return raw
        print("Invalid interface.")
        print("Expected format: ge<speed>-<ncp>/<slot>/<port> (e.g. ge100-1/1/1)")


def generate_lacp_system_id() -> str:
    # Use vendor prefix 84:40:76 as documented in DNOS LACP system-id overview.
    suffix = [random.randint(0, 255) for _ in range(3)]
    return "84:40:76:%02x:%02x:%02x" % tuple(suffix)


def parse_system_id(value: str) -> str:
    mac = value.strip().lower()
    if not re.match(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$", mac):
        raise ValueError("Invalid system-id format (expected aa:bb:cc:dd:ee:ff).")
    return mac


def parse_lldp_pairs(output: str) -> List[Tuple[str, str]]:
    """
    Return reciprocal LLDP interface pairs found in show lldp neighbors output.
    """
    rows = []
    for line in output.splitlines():
        if not line.strip().startswith("|"):
            continue
        if "Interface" in line and "Neighbor" in line:
            continue
        if set(line.strip()) <= {"+", "-", "|", " "}:
            continue
        parts = [p.strip() for p in line.strip("|").split("|")]
        if len(parts) < 3:
            continue
        local_if = parts[0]
        neighbor_if = parts[2]
        if local_if and neighbor_if:
            rows.append((local_if, neighbor_if))

    neighbors = {local: neighbor for local, neighbor in rows}
    pairs = []
    for local, neighbor in rows:
        if neighbor in neighbors and neighbors[neighbor] == local:
            pair = tuple(sorted((local, neighbor)))
            if pair not in pairs:
                pairs.append(pair)
    return pairs


def main() -> int:
    parser = argparse.ArgumentParser(description="LACP sanity test")
    parser.add_argument("--serial", help="Device serial number (SN)")
    parser.add_argument("--bundle-a", type=int, help="Bundle ID for port A")
    parser.add_argument("--bundle-b", type=int, help="Bundle ID for port B")
    parser.add_argument(
        "--port-a",
        help="Optional override for port A (e.g. ge100-1/1/1)",
    )
    parser.add_argument(
        "--port-b",
        help="Optional override for port B (physically connected)",
    )
    parser.add_argument(
        "--system-id-a",
        help="LACP system-id for bundle A (aa:bb:cc:dd:ee:ff)",
    )
    parser.add_argument(
        "--system-id-b",
        help="LACP system-id for bundle B (aa:bb:cc:dd:ee:ff)",
    )
    parser.add_argument("--timeout", type=int, default=10, help="SSH timeout seconds")
    parser.add_argument("--verbose", action="store_true", help="Print command progress")
    parser.add_argument(
        "--dump-config-output",
        action="store_true",
        help="Print config output if config fails",
    )
    parser.add_argument(
        "--dump-show-output",
        action="store_true",
        help="Print show output if show checks fail",
    )
    args = parser.parse_args()

    args.serial = prompt_if_missing(args.serial, "Device serial number (SN): ")
    bundle_a = args.bundle_a or prompt_int("Bundle ID for port A (1-65535): ", 1, 65535)
    bundle_b = args.bundle_b or prompt_int("Bundle ID for port B (1-65535): ", 1, 65535)
    if bundle_a == bundle_b:
        print("FAIL: Bundle A and Bundle B must be different.")
        return 2
    port_a = args.port_a
    port_b = args.port_b
    if port_a and port_b and port_a == port_b:
        print("FAIL: Port A and Port B must be different.")
        return 2
    members = [port_a, port_b] if port_a and port_b else []
    host = args.serial
    user = "dnroot"
    password = "dnroot"
    try:
        system_id_a = parse_system_id(args.system_id_a) if args.system_id_a else None
        system_id_b = parse_system_id(args.system_id_b) if args.system_id_b else None
    except ValueError as exc:
        print(f"FAIL: {exc}")
        return 2
    if not system_id_a:
        system_id_a = generate_lacp_system_id()
    if not system_id_b:
        system_id_b = generate_lacp_system_id()
    if system_id_a == system_id_b:
        system_id_b = generate_lacp_system_id()

    print(f"Target device (SN): {args.serial}")
    if members:
        print(f"Bundle A (bundle-{bundle_a}) member: {members[0]}")
        print(f"Bundle B (bundle-{bundle_b}) member: {members[1]}")
    print(f"Bundle A LACP system-id: {system_id_a}")
    print(f"Bundle B LACP system-id: {system_id_b}")
    try:
        def build_config_cmds(interfaces_kw: str, cleanup: bool = True) -> List[str]:
            cmds = [
                interfaces_kw,
            ]
            if cleanup and members:
                for member in members:
                    cmds += [
                        f"{member}",
                        "no bundle-id",
                        "exit",
                    ]
            if cleanup:
                cmds += [
                    f"no bundle-{bundle_a}",
                    f"no bundle-{bundle_b}",
                ]
            cmds += [
                f"bundle-{bundle_a}",
                "exit",
                f"bundle-{bundle_b}",
                "exit",
            ]
            if members:
                cmds += [
                    f"{members[0]}",
                    f"bundle-id {bundle_a}",
                    "exit",
                    f"{members[1]}",
                    f"bundle-id {bundle_b}",
                    "exit",
                ]
            cmds += [
                "exit",
                "protocols",
                "lldp",
                "admin-state enabled",
                "exit",
                "protocols",
                "lacp",
                f"interface bundle-{bundle_a}",
                f"system-id {system_id_a}",
                "mode active",
                "period short",
                "exit",
                f"interface bundle-{bundle_b}",
                f"system-id {system_id_b}",
                "mode active",
                "period short",
                "exit",
            ]
            return cmds

        config_cmds = build_config_cmds("interfaces")
        config_block = "configure\n" + "\n".join(config_cmds) + "\ncommit and-exit"
        try:
            config_output = run_shell_block(
                host, user, password, args.timeout, config_block, verbose=args.verbose
            )
        except Exception as exc:
            if "Socket is closed" in str(exc):
                time.sleep(1)
                config_output = run_shell_block(
                    host, user, password, args.timeout, config_block, verbose=True
                )
            else:
                raise
        err, _ = has_cli_error(config_output)
        if err:
            # Retry with older 'interface' hierarchy (pre-6.0 syntax)
            if args.verbose:
                print("Retrying config with 'interface' hierarchy...")
            config_cmds = build_config_cmds("interface")
            config_block = "configure\n" + "\n".join(config_cmds) + "\ncommit and-exit"
            try:
                config_output = run_shell_block(
                    host, user, password, args.timeout, config_block, verbose=args.verbose
                )
            except Exception as exc:
                if "Socket is closed" in str(exc):
                    time.sleep(1)
                    config_output = run_shell_block(
                        host, user, password, args.timeout, config_block, verbose=True
                    )
                else:
                    raise
        show_cmds: List[Tuple[str, str]] = []
        for name, cmd in COMMANDS:
            if "lacp interfaces" in cmd or "lacp counters" in cmd:
                show_cmds.append((name, f"{cmd} bundle-{bundle_a}"))
                show_cmds.append((f"{name}_b", f"{cmd} bundle-{bundle_b}"))
            else:
                show_cmds.append((name, cmd))
        sections = run_shell_sequence(
            host, user, password, args.timeout, show_cmds, verbose=args.verbose
        )
        output = config_output + "\n" + "\n".join(sections.values())
    except Exception as exc:
        print(f"FAIL: {exc}")
        return 2

    if not members:
        lldp_output = sections.get("lldp_neighbors", "")
        pairs = parse_lldp_pairs(lldp_output)
        if not pairs:
            print("FAIL: No reciprocal LLDP-connected port pairs found.")
            return 2
        selected = pairs[0]
        members = [selected[0], selected[1]]
        print(f"Detected LLDP-connected ports: {members[0]} <-> {members[1]}")
        # Re-run config with detected ports bound to bundles
        config_cmds = build_config_cmds("interfaces")
        config_block = "configure\n" + "\n".join(config_cmds) + "\ncommit and-exit"
        config_output = run_shell_block(
            host, user, password, args.timeout, config_block, verbose=args.verbose
        )
        config_error, config_err_lines = has_cli_error(config_output)
        if config_error:
            print("FAIL: Configuration errors detected:")
            for line in config_err_lines:
                print(f"  - {line}")
            if args.dump_config_output:
                print("\n--- Config Output ---")
                print(config_output)
            return 1

        show_cmds = []
        for name, cmd in COMMANDS:
            if "lacp interfaces" in cmd or "lacp counters" in cmd:
                show_cmds.append((name, f"{cmd} bundle-{bundle_a}"))
                show_cmds.append((f"{name}_b", f"{cmd} bundle-{bundle_b}"))
            else:
                show_cmds.append((name, cmd))
        sections = run_shell_sequence(
            host, user, password, args.timeout, show_cmds, verbose=args.verbose
        )

    config_error, config_err_lines = has_cli_error(config_output)
    if config_error:
        print("FAIL: Configuration errors detected:")
        for line in config_err_lines:
            print(f"  - {line}")
        if args.dump_config_output:
            print("\n--- Config Output ---")
            print(config_output)
        return 1

    show_errors = []
    for name, text in sections.items():
        err, err_lines = has_cli_error(text)
        if err:
            show_errors.append((name, err_lines))
    if show_errors:
        print("FAIL: Show command errors detected:")
        for name, err_lines in show_errors:
            print(f"  - {name}:")
            for line in err_lines:
                print(f"    {line}")
        return 1

    results = [
        check_lacp_interfaces(sections.get("lacp_interfaces", "")),
        check_lacp_counters(sections.get("lacp_counters", "")),
    ]

    failed = False
    for result in results:
        status = "PASS" if result.ok else "FAIL"
        print(f"{status}: {result.name}")
        for msg in result.errors:
            print(f"  - {msg}")
        for msg in result.warnings:
            print(f"  - WARN: {msg}")
        if not result.ok and args.dump_show_output:
            print(f"\n--- Show Output ({result.name}) ---")
            print(sections.get(result.name, ""))
        if not result.ok:
            failed = True

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
