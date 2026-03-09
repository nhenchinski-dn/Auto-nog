#!/usr/bin/env python3
import paramiko
import re
import time

DEVICE_HOST = "YE41F7VK00003B1"
USERNAME = "dnroot"
PASSWORD = "dnroot"

TARGET_SERIAL = "YE41F7VK00003B1"
POLL_INTERVAL = 10  # seconds
CHECK_COMMAND = "show sys | no-more"
PROMPT_MARKERS = ("#", ">")
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
DEFAULT_ITERATIONS = 10


def create_ssh_client(timeout=30):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        DEVICE_HOST,
        username=USERNAME,
        password=PASSWORD,
        timeout=timeout,
        banner_timeout=timeout,
        auth_timeout=timeout,
    )
    transport = client.get_transport()
    if transport is not None:
        transport.set_keepalive(30)
    return client


def ssh_run_command(client, command, timeout=30, get_pty=False):
    stdin, stdout, stderr = client.exec_command(
        command, timeout=timeout, get_pty=get_pty
    )
    output = (stdout.read().decode() or "") + (stderr.read().decode() or "")
    return output


def _read_until_prompt(channel, prompt=None, timeout=30, quiet=2):
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


def _extract_prompt(output):
    clean = ANSI_ESCAPE.sub("", output)
    lines = [line for line in clean.splitlines() if line.strip()]
    if not lines:
        return None
    last = lines[-1].rstrip()
    if last.endswith(PROMPT_MARKERS):
        return last
    return None


def ssh_run_shell_command(client, command, timeout=30):
    channel = client.invoke_shell()
    channel.settimeout(timeout)
    banner = _read_until_prompt(channel, prompt=None, timeout=timeout, quiet=1)
    prompt = _extract_prompt(banner)
    channel.send(command + "\n")
    output = _read_until_prompt(channel, prompt=prompt, timeout=timeout, quiet=2)
    channel.close()
    return banner + output


def ssh_run_shell_with_confirm(client, command, confirm="yes", timeout=30):
    channel = client.invoke_shell()
    channel.settimeout(timeout)
    banner = _read_until_prompt(channel, prompt=None, timeout=timeout, quiet=1)
    prompt = _extract_prompt(banner)
    channel.send(command + "\n")
    output = _read_until_prompt(channel, prompt=prompt, timeout=timeout, quiet=2)
    if "yes/no" in output.lower():
        channel.send(confirm + "\n")
        output += _read_until_prompt(channel, prompt=prompt, timeout=timeout, quiet=2)
    channel.close()
    return banner + output


def _find_ncp_operational(sh_sys_output, serial):
    """
    Return the operational field for the matching serial, or None.
    """
    serial_norm = serial.strip().lower()
    header = None
    for line in sh_sys_output.splitlines():
        if line.strip().startswith("|") and "Serial Number" in line:
            header = [c.strip() for c in line.split("|") if c.strip()]
            continue
        if not line.strip().startswith("|"):
            continue
        if set(line.strip()) <= {"+", "-"}:
            continue

        columns = [c.strip() for c in line.split("|") if c.strip()]
        if header and len(columns) == len(header):
            row = dict(zip(header, columns))
            row_serial = row.get("Serial Number", "").strip().lower()
            if row_serial == serial_norm:
                return row.get("Operational")
        elif serial_norm in line.lower():
            # Fallback for older layouts without a header parse.
            raw_columns = [c.strip() for c in line.split("|")]
            if len(raw_columns) > 4:
                return raw_columns[4]
            return None
    return None


def is_target_ncp_up(sh_sys_output, serial):
    """
    Match a specific NCP by serial number and check if it is UP.
    """
    operational = _find_ncp_operational(sh_sys_output, serial)
    if not operational:
        return False
    return "up" in operational.lower()


def _prompt_iterations(default):
    try:
        raw = input(f"How many restarts? [{default}] ").strip()
    except EOFError:
        return default
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def main():
    iterations = _prompt_iterations(DEFAULT_ITERATIONS)
    times = []
    for iteration in range(1, iterations + 1):
        print(f"\n--- Iteration {iteration}/{iterations} ---")
        elapsed = run_single_restart()
        if elapsed is not None:
            times.append(elapsed)
        time.sleep(2)

    if times:
        avg = sum(times) / len(times)
        print("\nRestart time summary:")
        print(f"  Runs: {len(times)}/{iterations}")
        print(f"  Average: {avg:.1f} seconds")
        print(f"  Min: {min(times):.1f} seconds")
        print(f"  Max: {max(times):.1f} seconds")
    else:
        print("\nNo successful restart times recorded.")


def run_single_restart():
    client = None
    serial_missing_logged = False
    backoff = POLL_INTERVAL

    print(f"Checking NCP serial {TARGET_SERIAL} is UP before restart...")
    while True:
        time.sleep(backoff)
        try:
            if client is None:
                client = create_ssh_client(timeout=10)
            output = ssh_run_shell_command(client, CHECK_COMMAND, timeout=30)
            if is_target_ncp_up(output, TARGET_SERIAL):
                print("Target NCP is UP. Sending cold restart command...")
                break
            if not serial_missing_logged and TARGET_SERIAL not in output:
                print("Serial not found in show system output; check command output format.")
                preview = "\n".join(output.splitlines()[:60])
                if preview:
                    print("show system output preview:")
                    print(preview)
                serial_missing_logged = True
            print("Device reachable, NCP not up yet")
            backoff = POLL_INTERVAL
        except Exception:
            if client:
                client.close()
            client = None
            print("Device not reachable yet")
            backoff = min(60, max(POLL_INTERVAL, int(backoff * 1.5)))

    start_time = time.time()
    try:
        if client is None:
            client = create_ssh_client(timeout=10)
        ssh_run_shell_with_confirm(
            client, "request system restart", confirm="yes", timeout=30
        )
    except Exception:
        print("Connection dropped (expected during restart)")
    finally:
        if client:
            client.close()
        client = None

    print(f"Waiting for NCP serial {TARGET_SERIAL} to come UP again...")
    seen_down = False
    backoff = POLL_INTERVAL
    while True:
        time.sleep(backoff)
        try:
            if client is None:
                client = create_ssh_client(timeout=10)
            output = ssh_run_shell_command(client, CHECK_COMMAND, timeout=30)
            is_up = is_target_ncp_up(output, TARGET_SERIAL)
            if not is_up:
                seen_down = True
            if is_up and (seen_down or time.time() - start_time >= POLL_INTERVAL):
                elapsed = time.time() - start_time
                print("Target NCP is UP")
                print(f"Cold restart time: {elapsed:.1f} seconds")
                return elapsed
            print("Device reachable, NCP not up yet")
            backoff = POLL_INTERVAL
        except Exception:
            if client:
                client.close()
            client = None
            print("Device not reachable yet")
            backoff = min(60, max(POLL_INTERVAL, int(backoff * 1.5)))
    return None


if __name__ == "__main__":
    main()
