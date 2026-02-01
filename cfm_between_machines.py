#!/usr/bin/env python3
"""
Discover the link between two devices (via LLDP) and bring up CFM
(Connectivity Fault Management) between them.

Usage:
  python3 cfm_between_machines.py --host-a <IP> --host-b <IP>
  python3 cfm_between_machines.py --host-a <IP> --host-b <IP> --iface-a eth0 --iface-b eth0  # skip discovery
"""
import argparse
import os
import re
import time
from dataclasses import dataclass


from typing import List, Optional, Set, Tuple

import paramiko


PROMPT_MARKERS = ("#", ">")
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


@dataclass
class LldpNeighbor:
    local_interface: str
    remote_chassis_id: str
    remote_system_name: str
    remote_port: str


def create_ssh_client(host: str, user: str, password: str, timeout: int = 30) -> paramiko.SSHClient:
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


def _strip_ansi(text: str) -> str:
    return ANSI_ESCAPE.sub("", text)


def _print_device_output(raw: str, host: str, max_lines: int = 50) -> None:
    """Print a snippet of device output for debugging commit/CFM failures."""
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    if not lines:
        return
    snippet = lines[-max_lines:] if len(lines) > max_lines else lines
    print(f"  --- device output ({host}) ---")
    for ln in snippet:
        print(f"  {ln}")
    print("  ---")


def _read_until_prompt(channel, timeout: int = 30, quiet: float = 2.0) -> str:
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
                clean = _strip_ansi(output)
                tail = clean.strip()
                if tail.endswith(PROMPT_MARKERS):
                    break
            else:
                if time.time() - last_data > quiet:
                    break
                time.sleep(0.2)
        except Exception:
            break
    return output


def _read_until_prompt_with_paging(channel, timeout: int = 60, quiet: float = 2.0) -> str:
    """
    Read until no new data for `quiet` seconds. On --more--/Press send space.
    When we see a prompt in the output, send space a few times to request next page (device may be paged),
    then keep reading so we get full output including any second table.
    """
    output = ""
    start = time.time()
    last_data = time.time()
    space_count = 0
    max_spaces_after_prompt = 5
    while True:
        if time.time() - start > timeout:
            break
        try:
            if channel.recv_ready():
                chunk = channel.recv(8192).decode(errors="ignore")
                output += chunk
                last_data = time.time()
                space_count = 0
                clean = _strip_ansi(output)
                tail = clean[-1000:] if len(clean) > 1000 else clean
                tail_lower = tail.lower()
                if (
                    "--more--" in tail_lower
                    or "press space" in tail_lower
                    or "press enter" in tail_lower
                    or ("more" in tail_lower and "press" in tail_lower)  # e.g. "-- More -- (Press q to quit)"
                ):
                    channel.send(" ")
                    time.sleep(0.4)
                    continue
                if tail.rstrip().endswith(PROMPT_MARKERS) and space_count < max_spaces_after_prompt:
                    for _ in range(3):
                        channel.send(" ")
                        time.sleep(0.4)
                    space_count += 1
                    continue
            else:
                if time.time() - last_data > quiet:
                    break
                time.sleep(0.2)
        except Exception:
            break
    return output


def run_shell(client: paramiko.SSHClient, command: str, timeout: int = 30, use_paging: bool = False) -> str:
    channel = client.invoke_shell()
    channel.settimeout(timeout)
    _read_until_prompt(channel, timeout=timeout, quiet=1.0)
    channel.send(command + "\n")
    if use_paging:
        out = _read_until_prompt_with_paging(channel, timeout=min(timeout + 15, 60), quiet=1.2)
    else:
        out = _read_until_prompt(channel, timeout=timeout, quiet=1.5)
    channel.close()
    return _strip_ansi(out)


def run_shell_with_no_paging(client: paramiko.SSHClient, show_cmd: str, timeout: int = 60) -> str:
    """
    Run a show command in a single shell after disabling paging, so full output is returned.
    Sends all common no-paging commands in sequence so whichever the device supports takes effect.
    """
    channel = client.invoke_shell()
    channel.settimeout(timeout)
    _read_until_prompt(channel, timeout=timeout, quiet=1.0)
    for no_page in ("set cli screen-length 0", "terminal length 0", "set pagination off", "terminal length 0"):
        channel.send(no_page + "\n")
        _read_until_prompt(channel, timeout=timeout, quiet=0.8)
    channel.send(show_cmd + "\n")
    out = _read_until_prompt_with_paging(channel, timeout=timeout, quiet=2.0)
    channel.close()
    return _strip_ansi(out)


def get_device_hostname(client: paramiko.SSHClient, timeout: int = 15) -> Optional[str]:
    """Get this device's hostname (for matching in LLDP neighbor tables)."""
    for cmd in ("hostname", "show hostname", "show system information"):
        out = run_shell(client, cmd, timeout=timeout)
        for line in out.splitlines():
            line = line.strip()
            if not line or line == cmd or line.startswith("show "):
                continue
            if "hostname" in line.lower() and ":" in line:
                m = re.search(r"hostname\s*[:\s]+\s*(\S+)", line, re.IGNORECASE)
                if m:
                    return m.group(1).strip()
            if line.endswith(PROMPT_MARKERS):
                line = line.rstrip("#>").strip()
            if line and len(line) < 80 and re.match(r"^[\w.-]+$", line):
                return line
        m = re.search(r"hostname\s*[:\s]+\s*(\S+)", out, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


def run_config_sequence(
    client: paramiko.SSHClient, commands: List[str], timeout: int = 60
) -> Tuple[bool, str]:
    """Run a sequence of config-mode commands (e.g. configure, ..., commit, exit). Returns (success, last_output)."""
    channel = client.invoke_shell()
    channel.settimeout(timeout)
    _read_until_prompt(channel, timeout=timeout, quiet=1.0)
    failed_cmd = None
    last_out = ""
    for cmd in commands:
        channel.send(cmd + "\n")
        last_out = _read_until_prompt(channel, timeout=timeout, quiet=1.2)
        if re.search(r"error|unknown|invalid|failed", last_out, re.IGNORECASE):
            failed_cmd = cmd
            break
    channel.close()
    if failed_cmd:
        return False, last_out
    return True, last_out


def check_lldp_enabled(client: paramiko.SSHClient, timeout: int = 25) -> bool:
    """Return True if LLDP appears enabled (show lldp neighbors returns a table or status shows enabled)."""
    out = run_shell(client, "show lldp neighbors", timeout=timeout)
    if re.search(r"disabled|not configured|not enabled|unknown command", out, re.IGNORECASE):
        return False
    if "interface" in out.lower() and "neighbor" in out.lower():
        return True
    out2 = run_shell(client, "show lldp status", timeout=timeout)
    if re.search(r"enabled|admin-status.*enabled", out2, re.IGNORECASE):
        return True
    return "interface" in out2.lower() and "neighbor" not in out2.lower()


def get_interface_list(
    client: paramiko.SSHClient,
    exclude_patterns: Tuple[str, ...] = ("sub", "bundle", "lo", "mgmt", "management"),
    timeout: int = 30,
) -> List[str]:
    """Get list of interface names from show interfaces (brief), excluding those matching exclude_patterns."""
    seen: Set[str] = set()
    for cmd in ("show interfaces brief", "show interfaces", "show interface brief"):
        out = run_shell(client, cmd, timeout=timeout)
        if "error" in out.lower()[:300] and "interface" not in out.lower():
            continue
        for line in out.splitlines():
            line = line.strip()
            if not line or line.lower().startswith("interface") or set(line) <= {"-", "+", "|", " "}:
                continue
            parts = line.split()
            if not parts:
                continue
            iface = parts[0].rstrip(":")
            if not iface or len(iface) < 3:
                continue
            if any(p in iface.lower() for p in exclude_patterns):
                continue
            if re.match(r"^[a-zA-Z]+\d+[-/]\d+[-/]\d+", iface):
                seen.add(iface)
        if seen:
            break
    return sorted(seen)


def enable_lldp(
    client: paramiko.SSHClient,
    exclude_patterns: Tuple[str, ...] = ("sub", "bundle", "lo", "mgmt", "management"),
    timeout: int = 60,
) -> Tuple[bool, str]:
    """
    Enable LLDP globally and on all interfaces except those matching exclude_patterns.
    Tries: configure -> protocols -> lldp -> admin-state enabled; then interface all or per-interface.
    """
    commands_global = [
        "configure",
        "protocols",
        "lldp",
        "admin-state enabled",
        "exit",
        "exit",
        "exit",
        "commit",
        "exit",
    ]
    ok, out = run_config_sequence(client, commands_global, timeout=timeout)
    if not ok:
        return False, out
    interfaces = get_interface_list(client, exclude_patterns=exclude_patterns, timeout=timeout)
    if not interfaces:
        return True, "LLDP enabled globally."
    commands_ifaces = ["configure", "protocols", "lldp"]
    for iface in interfaces:
        commands_ifaces.append(f"interface {iface}")
        commands_ifaces.append("exit")
    commands_ifaces += ["exit", "exit", "commit", "exit"]
    ok2, _ = run_config_sequence(client, commands_ifaces, timeout=timeout)
    if not ok2:
        return True, "LLDP enabled globally; per-interface enable failed (may not be required)."
    return True, "LLDP enabled globally and on interfaces."


def enable_l2_on_interface(
    client: paramiko.SSHClient,
    interface: str,
    unit: int = 1,
    vlan_id: int = 1,
    timeout: int = 60,
) -> Tuple[bool, str]:
    """
    Create L2 child interface so a CFM MEP can attach.
    Configures: int <interface>.<unit> admin-state enabled vlan-id <vlan_id> l2-service enabled
    """
    child_iface = f"{interface}.{unit}"
    cmd_create = f"int {child_iface} admin-state enabled vlan-id {vlan_id} l2-service enabled"
    commands = [
        "configure",
        cmd_create,
        "exit",
        "commit and-exit",
    ]
    error_pat = re.compile(
        r"unknown\s+command|invalid\s+value|invalid\s+command|error:|syntax\s+error|"
        r"commit\s+failed|validation\s+failed|command\s+failed|not\s+found",
        re.IGNORECASE,
    )
    channel = client.invoke_shell()
    channel.settimeout(timeout)
    _read_until_prompt(channel, timeout=timeout, quiet=1.0)
    failed_cmd = None
    for cmd in commands:
        channel.send(cmd + "\n")
        out = _read_until_prompt(channel, timeout=timeout, quiet=2.0 if cmd == "commit and-exit" else 1.2)
        if cmd == "commit and-exit":
            if error_pat.search(out):
                failed_cmd = cmd
                break
        elif error_pat.search(out):
            failed_cmd = cmd
            break
    channel.close()
    if failed_cmd:
        return False, f"L2 enable failed at: {failed_cmd}"
    return True, f"L2 child interface {child_iface} created."


def get_lldp_system_name(client: paramiko.SSHClient, timeout: int = 15) -> Optional[str]:
    """
    Get this device's LLDP system name (what it advertises to neighbors).
    Used to match the correct link: on the other device's LLDP table we look for this name.
    """
    for cmd in ("show lldp local", "show lldp", "show lldp status"):
        out = run_shell(client, cmd, timeout=timeout)
        m = re.search(r"system\s*name\s*[:\s]+\s*(\S+)", out, re.IGNORECASE)
        if m:
            return m.group(1).strip()
        m = re.search(r"system\s*name\s+(\S+)", out, re.IGNORECASE)
        if m:
            return m.group(1).strip()
        m = re.search(r"chassis\s*id\s*[:\s]+\s*(\S+)", out, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


def _normalize(s: str) -> str:
    """Normalize for matching: strip, collapse spaces, ASCII hyphen."""
    s = (s or "").strip().replace("\u2013", "-").replace("\u2014", "-")
    return " ".join(s.split())


def parse_lldp_neighbors(output: str) -> List[LldpNeighbor]:
    """
    Parse 'show lldp neighbors' output into a list of LldpNeighbor.
    Supports table format:
      | Interface | Neighbor System Name | Neighbor interface | Neighbor TTL |
      | ge400-0/0/4 | NCP3-nog-cfm | ge400-0/0/18 | 120 |
    Uses header to detect column order. Only includes rows where Neighbor System Name is non-empty.
    """
    neighbors: List[LldpNeighbor] = []
    lines = output.splitlines()

    header_idx = None
    col_interface = 0
    col_neighbor_name = 1
    col_neighbor_port = 2
    for i, line in enumerate(lines):
        lower = line.lower()
        if "interface" in lower and "neighbor" in lower:
            header_idx = i
            parts = [p.strip().lower() for p in line.split("|")]
            parts = [p for p in parts if p]
            for j, p in enumerate(parts):
                if "interface" in p and "neighbor" not in p:
                    col_interface = j
                elif "neighbor system name" in p or ("neighbor" in p and "name" in p):
                    col_neighbor_name = j
                elif "neighbor interface" in p or ("neighbor" in p and "interface" in p and "name" not in p):
                    col_neighbor_port = j
            break
    if header_idx is None:
        header_idx = -1

    for line in lines[header_idx + 1 :]:
        line = line.strip()
        if not line or set(line) <= {"+", "-", "|", " "}:
            continue
        if "interface" in line.lower() and "neighbor" in line.lower():
            continue

        parts = [p.strip() for p in line.split("|")]
        parts = [p for p in parts if p]
        if len(parts) > max(col_interface, col_neighbor_name):
            local_if = parts[col_interface]
            remote_name = parts[col_neighbor_name]
            remote_port = parts[col_neighbor_port] if col_neighbor_port < len(parts) else ""
            if not _normalize(remote_name):
                continue
            neighbors.append(
                LldpNeighbor(
                    local_interface=local_if,
                    remote_chassis_id=remote_name,
                    remote_system_name=remote_name,
                    remote_port=remote_port,
                )
            )
        elif len(parts) == 2:
            local_if, remote_name = parts[0], parts[1]
            if _normalize(remote_name):
                neighbors.append(
                    LldpNeighbor(
                        local_interface=local_if,
                        remote_chassis_id=remote_name,
                        remote_system_name=remote_name,
                        remote_port="",
                    )
                )
    return neighbors


def find_link_between(
    host_a: str,
    host_b: str,
    user: str,
    password: str,
    timeout: int = 30,
    save_lldp_path: Optional[str] = None,
    enable_lldp_if_needed: bool = True,
) -> Tuple[Optional[str], Optional[str], str]:
    """
    SSH to both devices, collect LLDP and hostnames, and determine which interface
    on A connects to B and which on B connects to A.
    Returns (iface_a, iface_b, message).
    """
    try:
        client_a = create_ssh_client(host_a, user, password, timeout)
    except Exception as e:
        err = str(e).lower()
        if "auth" in err or "password" in err or "permission" in err:
            return None, None, (
                f"Cannot SSH to {host_a}: {e}. "
                f"Check username (--user, default: dnroot) and password (--password or set CFM_SSH_PASSWORD)."
            )
        return None, None, f"Cannot SSH to {host_a}: {e}"
    try:
        client_b = create_ssh_client(host_b, user, password, timeout)
    except Exception as e:
        client_a.close()
        err = str(e).lower()
        if "auth" in err or "password" in err or "permission" in err:
            return None, None, (
                f"Cannot SSH to {host_b}: {e}. "
                f"Check username (--user, default: dnroot) and password (--password or set CFM_SSH_PASSWORD)."
            )
        return None, None, f"Cannot SSH to {host_b}: {e}"

    try:
        name_a = get_device_hostname(client_a, timeout)
        name_b = get_device_hostname(client_b, timeout)
        if not name_a:
            name_a = host_a
        if not name_b:
            name_b = host_b

        # Ensure LLDP is enabled on both devices (skip if --no-enable-lldp)
        if enable_lldp_if_needed:
            lldp_ok_a = check_lldp_enabled(client_a, timeout)
            lldp_ok_b = check_lldp_enabled(client_b, timeout)
            if not lldp_ok_a or not lldp_ok_b:
                exclude = ("sub", "bundle", "lo", "mgmt", "management")
                if not lldp_ok_a:
                    ok_a, msg_a = enable_lldp(client_a, exclude_patterns=exclude, timeout=timeout + 15)
                    if not ok_a:
                        return None, None, f"Could not enable LLDP on {host_a}: {msg_a}"
                if not lldp_ok_b:
                    ok_b, msg_b = enable_lldp(client_b, exclude_patterns=exclude, timeout=timeout + 15)
                    if not ok_b:
                        return None, None, f"Could not enable LLDP on {host_b}: {msg_b}"
                time.sleep(2)

        # What each device advertises in LLDP (often different from SSH hostname)
        lldp_name_a = get_lldp_system_name(client_a, timeout) or name_a
        lldp_name_b = get_lldp_system_name(client_b, timeout) or name_b

        lldp_cmds = ["show lldp neighbors", "sh lldp neighbors", "show lldp neighbor", "show lldp neighbors detail"]
        out_a = ""
        for cmd in lldp_cmds:
            out_a = run_shell_with_no_paging(client_a, cmd, timeout=timeout + 15)
            if "error" not in out_a.lower()[:200] and ("lldp" in out_a.lower() or "interface" in out_a.lower() or "neighbor" in out_a.lower()):
                break
        out_b = ""
        for cmd in lldp_cmds:
            out_b = run_shell_with_no_paging(client_b, cmd, timeout=timeout + 15)
            if "error" not in out_b.lower()[:200] and ("lldp" in out_b.lower() or "interface" in out_b.lower() or "neighbor" in out_b.lower()):
                break

        if save_lldp_path:
            try:
                with open(save_lldp_path + "_host_a.txt", "w") as f:
                    f.write(out_a)
                with open(save_lldp_path + "_host_b.txt", "w") as f:
                    f.write(out_b)
            except OSError:
                pass
        neighbors_a = parse_lldp_neighbors(out_a)
        neighbors_b = parse_lldp_neighbors(out_b)

        # On A: find the row where Neighbor System Name matches B's LLDP name (e.g. "ncpl-nog")
        def _matches(remote: str, target: str, host: str) -> bool:
            r = _normalize(remote).lower()
            t = _normalize(target).lower()
            h = (host or "").lower()
            if not r:
                return False
            return r == t or r == h or t in r or r in t or h in r

        iface_a = None
        for n in neighbors_a:
            remote = n.remote_system_name or n.remote_chassis_id or ""
            if _matches(remote, lldp_name_b, host_b) or _matches(remote, name_b, host_b):
                iface_a = n.local_interface
                break

        iface_b = None
        for n in neighbors_b:
            remote = n.remote_system_name or n.remote_chassis_id or ""
            if _matches(remote, lldp_name_a, host_a) or _matches(remote, name_a, host_a):
                iface_b = n.local_interface
                break

        if not iface_a:
            seen = [f"'{_normalize(n.remote_system_name or n.remote_chassis_id or '')}'" for n in neighbors_a]
            hint = (
                f" Use --iface-a ge400-0/0/33 --iface-b ge100-0/0/70 to specify the link manually"
                f" (adjust to your interfaces). Use --save-lldp FILE to save raw LLDP output for debugging."
            )
            return None, None, (
                f"Could not find interface on {host_a} that connects to {host_b}. "
                f"Looking for neighbor name '{lldp_name_b}' (or '{name_b}'). "
                f"Parsed neighbor names: {seen[:20]}{'...' if len(seen) > 20 else ''}.{hint}"
            )
        if not iface_b:
            seen = [f"'{_normalize(n.remote_system_name or n.remote_chassis_id or '')}'" for n in neighbors_b]
            hint = (
                f" Use --iface-a <iface_on_A> --iface-b <iface_on_B> to specify the link manually."
                f" Use --save-lldp FILE to save raw LLDP output for debugging."
            )
            return None, None, (
                f"Could not find interface on {host_b} that connects to {host_a}. "
                f"Looking for neighbor name '{lldp_name_a}' (or '{name_a}'). "
                f"Parsed neighbor names: {seen[:20]}{'...' if len(seen) > 20 else ''}.{hint}"
            )
        return iface_a, iface_b, f"Link: {host_a}:{iface_a} <-> {host_b}:{iface_b}"
    finally:
        client_a.close()
        client_b.close()


def build_cfm_commands(
    md_name: str,
    ma_name: str,
    mep_id: int,
    remote_mep_id: int,
    interface: str,
    direction: str = "down",
    level: int = 7,
) -> List[str]:
    """
    Build CLI commands to create CFM per device hierarchy:
    maintenance-domains, maintenance-associations, local-mep (direction, interface),
    remote-meps (auto-discovery disabled, crosscheck mep-id).
    """
    return [
        "configure",
        "services ethernet-oam connectivity-fault-management",
        f"maintenance-domains {md_name}",
        f"level {level}",
        f"md-name string {md_name}",
        f"maintenance-associations {ma_name}",
        f"short-ma-name string {ma_name}",
        f"local-mep {mep_id}",
        f"direction {direction}",
        f"interface {interface}",
        "exit",
        "remote-meps",
        "auto-discovery disabled",
        f"crosscheck mep-id {remote_mep_id}",
        "exit",
        "exit",
        "exit",
        "exit",
        "exit",
        "exit",
        "commit and-exit",
    ]


def apply_cfm(
    client: paramiko.SSHClient,
    md_name: str,
    ma_name: str,
    mep_id: int,
    remote_mep_id: int,
    interface: str,
    timeout: int = 60,
    level: int = 7,
    create_interface: Optional[str] = None,
    vlan_id: int = 1,
) -> Tuple[bool, str, Optional[str]]:
    """Apply CFM config on one device. Returns (success, message, raw_output_if_failed).
    When create_interface is set (e.g. ge400-0/0/33.1), creates that child interface in the same
    configure block as CFM so the interface exists at commit time.
    """
    commands = build_cfm_commands(
        md_name, ma_name, mep_id, remote_mep_id, interface, level=level
    )
    if create_interface:
        # Create child interface in same configure block (interfaces -> name -> attributes -> exit exit)
        interface_block = [
            "interfaces",
            create_interface,
            "admin-state enabled",
            f"vlan-id {vlan_id}",
            "l2-service enabled",
            "exit",
            "exit",
        ]
        commands = [commands[0]] + interface_block + commands[1:]
    channel = client.invoke_shell()
    channel.settimeout(timeout)
    _read_until_prompt(channel, timeout=timeout, quiet=1.0)
    failed_cmd = None
    last_out = ""
    for cmd in commands:
        channel.send(cmd + "\n")
        out = _read_until_prompt(channel, timeout=timeout, quiet=2.0 if cmd == "commit and-exit" else 1.5)
        last_out = out
        if cmd == "commit and-exit":
            if re.search(r"commit\s+failed|error|invalid|unknown\s+command|validation\s+failed|TRANSACTION_COMMIT|commit\s+check\s+failed", out, re.IGNORECASE):
                failed_cmd = cmd
                break
        elif re.search(r"error|unknown|invalid|failed", out, re.IGNORECASE):
            failed_cmd = cmd
            break
    channel.close()
    if failed_cmd:
        return False, f"Failed at: {failed_cmd}", last_out
    return True, "CFM configured and committed.", None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Find link between two devices (LLDP) and bring up CFM between them."
    )
    parser.add_argument("--host-a", help="First device hostname or IP")
    parser.add_argument("--host-b", help="Second device hostname or IP")
    parser.add_argument("--user", default="dnroot", help="SSH user")
    parser.add_argument(
        "--password",
        default="dnroot",
        help="SSH password (default: dnroot)",
    )
    parser.add_argument("--iface-a", help="Override: interface on host-a (skip LLDP discovery)")
    parser.add_argument("--iface-b", help="Override: interface on host-b (skip LLDP discovery)")
    parser.add_argument("--md-name", default="CFM-MD", help="Maintenance domain name")
    parser.add_argument("--ma-name", default="CFM-MA", help="Maintenance association name")
    parser.add_argument("--level", type=int, default=7, help="MD level 0-7 (default 7)")
    parser.add_argument("--mep-a", type=int, default=1, help="MEP ID on host-a")
    parser.add_argument("--mep-b", type=int, default=2, help="MEP ID on host-b")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--dry-run", action="store_true", help="Only discover link, do not configure CFM")
    parser.add_argument(
        "--save-lldp",
        metavar="FILE",
        help="Save raw LLDP output to FILE_host_a.txt and FILE_host_b.txt for debugging",
    )
    parser.add_argument(
        "--no-enable-lldp",
        action="store_true",
        help="Do not auto-enable LLDP if disabled; assume LLDP is already configured",
    )
    parser.add_argument(
        "--l2-unit",
        type=int,
        default=1,
        help="Logical unit for L2 child interface (default 1); creates interface.unit e.g. ge400-0/0/33.1",
    )
    parser.add_argument(
        "--vlan-id",
        type=int,
        default=1,
        help="VLAN ID for L2 child interface (default 1)",
    )
    parser.add_argument(
        "--no-enable-l2",
        action="store_true",
        help="Do not create L2 child interface; assume L2/unit is already configured",
    )
    args = parser.parse_args()

    host_a = args.host_a or input("Host A (IP or hostname): ").strip()
    host_b = args.host_b or input("Host B (IP or hostname): ").strip()
    if not host_a or not host_b:
        print("Need both host-a and host-b.")
        return 2
    password = args.password or os.environ.get("CFM_SSH_PASSWORD") or os.environ.get("SSH_PASSWORD") or "dnroot"

    if args.iface_a and args.iface_b:
        iface_a, iface_b = args.iface_a, args.iface_b
        print(f"Using interfaces: {host_a}:{iface_a} <-> {host_b}:{iface_b}")
    else:
        print("Discovering link via LLDP...")
        iface_a, iface_b, msg = find_link_between(
            host_a,
            host_b,
            args.user,
            password,
            args.timeout,
            save_lldp_path=args.save_lldp,
            enable_lldp_if_needed=not args.no_enable_lldp,
        )
        if iface_a is None:
            print(msg)
            return 1
        print(msg)

    if args.dry_run:
        print("Dry-run: not configuring CFM.")
        return 0

    cfm_iface_a = f"{iface_a}.{args.l2_unit}"
    cfm_iface_b = f"{iface_b}.{args.l2_unit}"
    if not args.no_enable_l2:
        print("Enabling L2 on interfaces...")
        try:
            client_a = create_ssh_client(host_a, args.user, password, args.timeout)
            client_b = create_ssh_client(host_b, args.user, password, args.timeout)
        except Exception as e:
            print(f"SSH failed: {e}")
            return 1
        try:
            ok_la, msg_la = enable_l2_on_interface(
                client_a, iface_a, unit=args.l2_unit, vlan_id=args.vlan_id, timeout=args.timeout + 20
            )
            ok_lb, msg_lb = enable_l2_on_interface(
                client_b, iface_b, unit=args.l2_unit, vlan_id=args.vlan_id, timeout=args.timeout + 20
            )
            if not ok_la:
                print(f"  {host_a}: {msg_la}")
            if not ok_lb:
                print(f"  {host_b}: {msg_lb}")
            if not ok_la or not ok_lb:
                client_a.close()
                client_b.close()
                return 1
        finally:
            client_a.close()
            client_b.close()

    print(f"Configuring CFM: MD={args.md_name} MA={args.ma_name} (MEP {args.mep_a} on A, MEP {args.mep_b} on B)...")
    try:
        client_a = create_ssh_client(host_a, args.user, password, args.timeout)
        client_b = create_ssh_client(host_b, args.user, password, args.timeout)
    except Exception as e:
        print(f"SSH failed: {e}")
        return 1

    try:
        ok_a, msg_a, raw_a = apply_cfm(
            client_a, args.md_name, args.ma_name, args.mep_a, args.mep_b, cfm_iface_a,
            timeout=args.timeout + 30, level=args.level,
            create_interface=cfm_iface_a, vlan_id=args.vlan_id,
        )
        ok_b, msg_b, raw_b = apply_cfm(
            client_b, args.md_name, args.ma_name, args.mep_b, args.mep_a, cfm_iface_b,
            timeout=args.timeout + 30, level=args.level,
            create_interface=cfm_iface_b, vlan_id=args.vlan_id,
        )
        if ok_a:
            print(f"  {host_a}: {msg_a}")
        else:
            print(f"  {host_a}: {msg_a}")
            if raw_a:
                _print_device_output(raw_a, host_a)
        if ok_b:
            print(f"  {host_b}: {msg_b}")
        else:
            print(f"  {host_b}: {msg_b}")
            if raw_b:
                _print_device_output(raw_b, host_b)
        if not ok_a or not ok_b:
            return 1
        print("CFM is up between the two machines.")
        return 0
    finally:
        client_a.close()
        client_b.close()


if __name__ == "__main__":
    raise SystemExit(main())
