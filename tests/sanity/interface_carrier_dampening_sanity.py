#!/usr/bin/env python3
import argparse
import random
import re
import sys
import time
from typing import List, Optional, Tuple

import paramiko


PROMPT_MARKERS = ("#", ">")


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


def _read_for_quiet(channel, quiet=1.5, max_duration=10):
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
) -> str:
    client = create_ssh_client(host, user, password, timeout)
    channel = client.invoke_shell()
    channel.settimeout(timeout)
    try:
        output = _read_for_quiet(channel, quiet=1, max_duration=timeout)
        for cmd in commands:
            channel.send(cmd + "\n")
            max_duration = 30 if cmd == "commit" else 8
            output += _read_for_quiet(channel, quiet=1.5, max_duration=max_duration)
        return output
    finally:
        try:
            channel.close()
        finally:
            client.close()


def run_exec_command(
    host: str,
    user: str,
    password: str,
    timeout: int,
    command: str,
) -> str:
    client = create_ssh_client(host, user, password, timeout)
    try:
        stdin, stdout, stderr = client.exec_command(
            command, timeout=timeout, get_pty=True
        )
        return (stdout.read().decode() or "") + (stderr.read().decode() or "")
    finally:
        client.close()


def has_cli_error(text: str) -> Tuple[bool, List[str]]:
    errors = []
    for line in text.splitlines():
        if re.search(
            r"\b(Error:|Unknown command|Invalid|ERROR:|Commit check failed|Commit failed)\b",
            line,
        ):
            errors.append(line.strip())
    return (len(errors) > 0, errors)


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


def prompt_interface(prompt: str) -> str:
    pattern = re.compile(r"^ge\d+-\d+/\d+/\d+$")
    while True:
        raw = input(prompt).strip()
        if pattern.match(raw):
            return raw
        print("Invalid interface. Expected format: ge<speed>-<ncp>/<slot>/<port>")


def sleep_before_expire(delay_ms: int):
    if delay_ms <= 0:
        time.sleep(0.1)
        return
    seconds = max(0.1, (delay_ms / 1000.0) * 0.5)
    time.sleep(seconds)


def get_interface_section(output: str, interface: str) -> str:
    lines = output.splitlines()
    section = []
    in_section = False
    header = f"Interface {interface}"
    for line in lines:
        if line.strip().startswith("Interface "):
            if in_section and not line.strip().startswith(header):
                break
            in_section = line.strip().startswith(header)
        if in_section:
            section.append(line)
    return "\n".join(section)


def get_dampening_penalty(output: str, interface: str) -> Optional[int]:
    section = get_interface_section(output, interface)
    match = re.search(
        r"Interface-Dampening:\s*enabled,\s*current penalty counter:\s*([^\s,]+)",
        section,
    )
    if not match:
        return None
    value = match.group(1).strip()
    if value.lower() in ("none", "n/a"):
        return None
    if value.isdigit():
        return int(value)
    return None


def apply_interface_config(
    host: str, user: str, password: str, timeout: int, interface: str, commands: List[str]
) -> str:
    return run_shell_commands(
        host,
        user,
        password,
        timeout,
        ["configure", "interfaces", interface] + commands + ["exit", "commit", "exit"],
    )


def apply_dampening_config(
    host: str, user: str, password: str, timeout: int, interface: str, commands: List[str]
) -> str:
    return run_shell_commands(
        host,
        user,
        password,
        timeout,
        [
            "configure",
            "interfaces",
            interface,
            "dampening",
        ]
        + commands
        + ["exit", "exit", "commit", "exit"],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Carrier-delay and dampening sanity test")
    parser.add_argument("--serial", help="Device serial number (SN)")
    parser.add_argument("--interface", help="Interface to test (e.g. ge100-1/1/1)")
    parser.add_argument("--timeout", type=int, default=10, help="SSH timeout seconds")
    parser.add_argument("--flap-count", type=int, default=20, help="Flaps for dampening")
    args = parser.parse_args()

    serial = prompt_if_missing(args.serial, "Device serial number (SN): ")
    interface = args.interface or prompt_interface("Interface to test: ")
    host = serial
    user = "dnroot"
    password = "dnroot"

    print(f"Target device (SN): {serial}")
    print(f"Target interface: {interface}")

    # Ensure admin-state enabled
    output = apply_interface_config(
        host, user, password, args.timeout, interface, ["admin-state enabled"]
    )
    err, err_lines = has_cli_error(output)
    if err:
        print("FAIL: Error during admin-state enable:")
        for line in err_lines:
            print(f"  - {line}")
        return 1

    # Carrier-delay down tests
    down_high = 60000
    down_low = 0
    down_rand = random.randint(1, 59999)
    for label, down_ms in [
        ("highest", down_high),
        ("lowest", down_low),
        ("random", down_rand),
    ]:
        print(f"Carrier-delay down {label}: {down_ms} ms")
        output = apply_interface_config(
            host, user, password, args.timeout, interface, [f"carrier-delay down {down_ms}"]
        )
        err, err_lines = has_cli_error(output)
        if err:
            print("FAIL: Error during carrier-delay down config:")
            for line in err_lines:
                print(f"  - {line}")
            return 1
        output = apply_interface_config(
            host, user, password, args.timeout, interface, ["admin-state disabled"]
        )
        err, err_lines = has_cli_error(output)
        if err:
            print("FAIL: Error during admin-state disable:")
            for line in err_lines:
                print(f"  - {line}")
            return 1
        sleep_before_expire(down_ms)
        output = apply_interface_config(
            host, user, password, args.timeout, interface, ["admin-state enabled"]
        )
        err, err_lines = has_cli_error(output)
        if err:
            print("FAIL: Error during admin-state enable:")
            for line in err_lines:
                print(f"  - {line}")
            return 1

    # Turn interface down
    output = apply_interface_config(
        host, user, password, args.timeout, interface, ["admin-state disabled"]
    )
    err, err_lines = has_cli_error(output)
    if err:
        print("FAIL: Error during admin-state disable:")
        for line in err_lines:
            print(f"  - {line}")
        return 1

    # Carrier-delay up tests
    up_high = 120000
    up_low = 0
    up_rand = random.randint(1, 119999)
    for label, up_ms in [
        ("highest", up_high),
        ("lowest", up_low),
        ("random", up_rand),
    ]:
        print(f"Carrier-delay up {label}: {up_ms} ms")
        output = apply_interface_config(
            host, user, password, args.timeout, interface, [f"carrier-delay up {up_ms}"]
        )
        err, err_lines = has_cli_error(output)
        if err:
            print("FAIL: Error during carrier-delay up config:")
            for line in err_lines:
                print(f"  - {line}")
            return 1
        output = apply_interface_config(
            host, user, password, args.timeout, interface, ["admin-state enabled"]
        )
        err, err_lines = has_cli_error(output)
        if err:
            print("FAIL: Error during admin-state enable:")
            for line in err_lines:
                print(f"  - {line}")
            return 1
        sleep_before_expire(up_ms)
        output = apply_interface_config(
            host, user, password, args.timeout, interface, ["admin-state disabled"]
        )
        err, err_lines = has_cli_error(output)
        if err:
            print("FAIL: Error during admin-state disable:")
            for line in err_lines:
                print(f"  - {line}")
            return 1

    # Set carrier-delay up and down to lowest value
    output = apply_interface_config(
        host,
        user,
        password,
        args.timeout,
        interface,
        ["carrier-delay down 0 up 0"],
    )
    err, err_lines = has_cli_error(output)
    if err:
        print("FAIL: Error during carrier-delay down/up config:")
        for line in err_lines:
            print(f"  - {line}")
        return 1

    # Dampening default values
    output = apply_dampening_config(
        host, user, password, args.timeout, interface, ["admin-state enabled"]
    )
    err, err_lines = has_cli_error(output)
    if err:
        print("FAIL: Error during dampening default config:")
        for line in err_lines:
            print(f"  - {line}")
        return 1

    # Dampening random values (small ranges to keep test duration reasonable)
    half_life = random.randint(10, 60)
    reuse = random.randint(500, 1500)
    suppress = random.randint(reuse + 200, reuse + 1500)
    max_suppress = random.randint(120, 600)
    output = apply_dampening_config(
        host,
        user,
        password,
        args.timeout,
        interface,
        [
            f"half-life {half_life}",
            f"reuse-threshold {reuse}",
            f"suppress-threshold {suppress}",
            f"max-suppress {max_suppress}",
            "admin-state enabled",
        ],
    )
    err, err_lines = has_cli_error(output)
    if err:
        print("FAIL: Error during dampening random config:")
        for line in err_lines:
            print(f"  - {line}")
        return 1

    # Flap interface until dampening is triggered
    print("Flapping interface to trigger dampening...")
    dampened = False
    for i in range(args.flap_count):
        apply_interface_config(
            host, user, password, args.timeout, interface, ["admin-state disabled"]
        )
        time.sleep(0.3)
        apply_interface_config(
            host, user, password, args.timeout, interface, ["admin-state enabled"]
        )
        time.sleep(0.3)
        detail = run_exec_command(
            host, user, password, args.timeout, "show interfaces detail | no-more"
        )
        penalty = get_dampening_penalty(detail, interface)
        if penalty is not None and penalty > 0:
            print(f"Dampening triggered (penalty {penalty}).")
            dampened = True
            break
    if not dampened:
        print("WARN: Dampening not triggered within flap limit.")

    # Wait for timers to expire (bounded)
    wait_seconds = min(60, max_suppress)
    print(f"Waiting {wait_seconds} seconds for dampening timers to expire...")
    time.sleep(wait_seconds)

    # Clear dampening penalty and counters
    run_exec_command(
        host,
        user,
        password,
        args.timeout,
        f"clear interfaces dampening penalty {interface}",
    )
    run_exec_command(
        host,
        user,
        password,
        args.timeout,
        f"clear interfaces dampening counters {interface}",
    )

    print("DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
