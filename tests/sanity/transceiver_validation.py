#!/usr/bin/env python3
"""
Transceiver Validation Script for DNOS

Validates N pairs of transceivers on a single DNOS machine:
  1. Transceiver info (Tx/Rx power, temperature, voltage, bias current)
  2. Traffic flow via interface counters (user sets up BD manually)
  3. Admin-state toggling (shut one side, both sides, verify recovery)

Usage:
  python3 transceiver_validation.py --host <SN-or-IP>
  python3 transceiver_validation.py --host <SN-or-IP> \\
      --pairs ge100-0/0/1,ge100-0/0/2 ge100-0/0/3,ge100-0/0/4 ge100-0/0/5,ge100-0/0/6

  Or interactively (prompts for number of pairs and interfaces):
  python3 transceiver_validation.py --host <SN-or-IP>

  Optional flags:
      --num-pairs N        Number of pairs (prompted if omitted)
      --skip-transceiver   Skip transceiver info validation
      --skip-traffic       Skip traffic counter validation
      --skip-admin         Skip admin-state toggle tests
      --counter-wait 10    Seconds between counter snapshots (default 10)
      --settle-time 5      Seconds to wait after admin-state change (default 5)
"""
import argparse
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import paramiko


# ---------------------------------------------------------------------------
# Transceiver thresholds  (adjust per transceiver model / datasheet)
# ---------------------------------------------------------------------------
TEMP_WARN_LOW = -5.0        # Celsius
TEMP_WARN_HIGH = 75.0
VOLTAGE_WARN_LOW = 3.0      # Volts
VOLTAGE_WARN_HIGH = 3.6
TX_POWER_FAIL_LOW = -8.0    # dBm
TX_POWER_FAIL_HIGH = 3.0
RX_POWER_FAIL_LOW = -14.0   # dBm
RX_POWER_FAIL_HIGH = 1.0
BIAS_WARN_LOW = 2.0         # mA
BIAS_WARN_HIGH = 80.0

# Timing defaults
DEFAULT_COUNTER_WAIT = 10   # seconds between counter snapshots
ADMIN_STATE_SETTLE = 5      # seconds after admin-state change


# ---------------------------------------------------------------------------
# SSH / CLI utilities  (same patterns as existing codebase)
# ---------------------------------------------------------------------------
PROMPT_MARKERS = ("#", ">")
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    return ANSI_ESCAPE.sub("", text)


@dataclass
class SectionResult:
    name: str
    ok: bool
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


@dataclass
class TransceiverInfo:
    interface: str
    vendor: str = ""
    part_number: str = ""
    serial_number: str = ""
    temperature: Optional[float] = None
    voltage: Optional[float] = None
    tx_power: Optional[float] = None
    rx_power: Optional[float] = None
    tx_bias: Optional[float] = None
    raw_output: str = ""


@dataclass
class InterfaceCounters:
    interface: str
    rx_packets: int = 0
    tx_packets: int = 0
    rx_bytes: int = 0
    tx_bytes: int = 0
    rx_errors: int = 0
    tx_errors: int = 0
    rx_drops: int = 0
    tx_drops: int = 0


# ---------------------------------------------------------------------------
# SSH connection and command helpers
# ---------------------------------------------------------------------------
def create_ssh_client(
    host: str, user: str, password: str, timeout: int = 30
) -> paramiko.SSHClient:
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


def _read_for_quiet(channel, quiet: float = 1.5, max_duration: float = 10):
    """Read from channel until silence for *quiet* seconds or *max_duration* elapsed."""
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
                clean = _strip_ansi(output)
                lower = clean.lower()
                tail = lower[-500:] if len(lower) > 500 else lower
                # Handle --more-- paging
                if "--more--" in tail:
                    channel.send(" ")
                # Handle DNOS auto-confirm prompts during commit
                if "yes/no" in tail:
                    channel.send("yes\n")
                if "what would you like to do" in tail and "merge" in tail:
                    channel.send("merge-only\n")
                if "enter yes to continue" in tail:
                    channel.send("yes\n")
            else:
                if time.time() - last_data > quiet:
                    break
                time.sleep(0.2)
        except Exception:
            break
    return output


def run_show_command(
    client: paramiko.SSHClient, command: str, timeout: int = 30
) -> str:
    """Run a show command via interactive shell, with paging disabled."""
    channel = client.invoke_shell()
    channel.settimeout(timeout)
    _read_for_quiet(channel, quiet=1, max_duration=timeout)
    channel.send(command + " | no-more\n")
    out = _read_for_quiet(channel, quiet=2.0, max_duration=timeout)
    channel.close()
    return _strip_ansi(out)


def run_config_commands(
    client: paramiko.SSHClient, commands: List[str], timeout: int = 30
) -> str:
    """Run a sequence of configuration commands via interactive shell."""
    channel = client.invoke_shell()
    channel.settimeout(timeout)
    _read_for_quiet(channel, quiet=1, max_duration=timeout)
    output = ""
    for cmd in commands:
        channel.send(cmd + "\n")
        max_dur = 30 if cmd in ("commit", "commit and-exit") else 8
        out = _read_for_quiet(channel, quiet=1.5, max_duration=max_dur)
        output += out
    channel.close()
    return _strip_ansi(output)


def has_cli_error(text: str) -> Tuple[bool, List[str]]:
    errors: List[str] = []
    for line in text.splitlines():
        if re.search(
            r"\b(Error:|Unknown command|Invalid|ERROR:|"
            r"Commit check failed|Commit failed)\b",
            line,
        ):
            errors.append(line.strip())
    return (len(errors) > 0, errors)


# ---------------------------------------------------------------------------
# Interactive prompts
# ---------------------------------------------------------------------------
IFACE_PATTERN = re.compile(r"^[a-zA-Z]+\d+[-]\d+/\d+/\d+$")


def prompt_if_missing(value: Optional[str], prompt_text: str) -> str:
    if value:
        return value
    return input(prompt_text).strip()


def prompt_interface(prompt_text: str) -> str:
    while True:
        raw = input(prompt_text).strip()
        if IFACE_PATTERN.match(raw):
            return raw
        print("  Invalid interface. Expected format: ge<speed>-<ncp>/<slot>/<port>  "
              "(e.g. ge100-0/0/1)")


def prompt_pair(pair_num: int) -> Tuple[str, str]:
    print(f"\n--- Pair {pair_num} ---")
    side_a = prompt_interface(f"  Side-A interface: ")
    side_b = prompt_interface(f"  Side-B interface: ")
    if side_a == side_b:
        print("  Side-A and Side-B cannot be the same interface.")
        return prompt_pair(pair_num)
    return (side_a, side_b)


def parse_pair_arg(value: str) -> Tuple[str, str]:
    parts = [p.strip() for p in value.split(",")]
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise argparse.ArgumentTypeError(
            f"Invalid pair '{value}'. Expected: ifaceA,ifaceB  "
            f"(e.g. ge100-0/0/1,ge100-0/0/2)"
        )
    return (parts[0], parts[1])


# ===================================================================
# TEST 1 :  Transceiver Info Validation
# ===================================================================
def parse_transceiver_output(interface: str, output: str) -> TransceiverInfo:
    """Parse a single-interface section from ``show int transceiver`` (DNOS format).

    DNOS uses key-value lines aligned with ``:`` separator, e.g.::

        Vendor name                                   : HUBER+SUHNER
        Module temperature                            : 24.4 degrees C / 76.0 degrees F
        Module voltage                                : 3.4 V
        Laser tx bias current (Channel 0)             : 29.5 mA
        Transmit avg optical power (Channel 0)        : -3.8 dBm / 0.4 mW
        Rcvr signal avg optical power (Channel 0)     : -3.1 dBm / 0.5 mW
    """
    info = TransceiverInfo(interface=interface, raw_output=output)

    def _first_float(s: str) -> Optional[float]:
        m = re.search(r"([+-]?\d+\.?\d*)", s)
        return float(m.group(1)) if m else None

    for line in output.splitlines():
        # Each DNOS line: <description> : <value>
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip().lower()
        val = val.strip()

        # Skip alarm/warning/threshold lines
        if "threshold" in key or " alarm" in key or " warning" in key:
            continue

        if key.startswith("vendor name"):
            info.vendor = val
        elif key.startswith("vendor pn") or key.startswith("vendor p/n"):
            info.part_number = val
        elif key.startswith("vendor sn") or key.startswith("vendor s/n"):
            info.serial_number = val
        elif key == "module temperature":
            info.temperature = _first_float(val)
        elif key == "module voltage":
            info.voltage = _first_float(val)
        elif "transmit avg optical power" in key:
            info.tx_power = _first_float(val)
        elif "rcvr signal avg optical power" in key:
            info.rx_power = _first_float(val)
        elif "laser tx bias current" in key:
            info.tx_bias = _first_float(val)

    return info


def _extract_interface_section(output: str, interface: str) -> str:
    """Extract the block for *interface* from global ``show int transceiver``."""
    lines = output.splitlines()
    section: List[str] = []
    in_section = False
    for line in lines:
        stripped = line.strip()
        # Section headers look like: "Interface ge10-0/0/26"
        if stripped.startswith("Interface "):
            iface_name = stripped[len("Interface "):].strip()
            if iface_name == interface:
                in_section = True
                section.append(line)
            elif in_section:
                break  # hit next interface, stop
            continue
        if in_section:
            section.append(line)
    return "\n".join(section)


def validate_transceiver(info: TransceiverInfo) -> SectionResult:
    """Validate transceiver readings against thresholds."""
    name = f"transceiver_{info.interface}"
    warnings: List[str] = []
    errors: List[str] = []

    if info.tx_power is None and info.rx_power is None and info.temperature is None:
        errors.append(f"{info.interface}: No transceiver data found "
                      "(absent or unsupported)")
        return SectionResult(name, False, warnings, errors)

    # Temperature
    if info.temperature is not None:
        if not (TEMP_WARN_LOW <= info.temperature <= TEMP_WARN_HIGH):
            warnings.append(
                f"{info.interface}: Temperature {info.temperature} C outside "
                f"[{TEMP_WARN_LOW}, {TEMP_WARN_HIGH}]"
            )

    # Voltage
    if info.voltage is not None:
        if not (VOLTAGE_WARN_LOW <= info.voltage <= VOLTAGE_WARN_HIGH):
            warnings.append(
                f"{info.interface}: Voltage {info.voltage} V outside "
                f"[{VOLTAGE_WARN_LOW}, {VOLTAGE_WARN_HIGH}]"
            )

    # Tx Power
    if info.tx_power is not None:
        if not (TX_POWER_FAIL_LOW <= info.tx_power <= TX_POWER_FAIL_HIGH):
            errors.append(
                f"{info.interface}: Tx Power {info.tx_power} dBm outside "
                f"[{TX_POWER_FAIL_LOW}, {TX_POWER_FAIL_HIGH}]"
            )
    else:
        warnings.append(f"{info.interface}: Tx Power not available")

    # Rx Power
    if info.rx_power is not None:
        if not (RX_POWER_FAIL_LOW <= info.rx_power <= RX_POWER_FAIL_HIGH):
            errors.append(
                f"{info.interface}: Rx Power {info.rx_power} dBm outside "
                f"[{RX_POWER_FAIL_LOW}, {RX_POWER_FAIL_HIGH}]"
            )
    else:
        warnings.append(f"{info.interface}: Rx Power not available")

    # Tx Bias
    if info.tx_bias is not None:
        if not (BIAS_WARN_LOW <= info.tx_bias <= BIAS_WARN_HIGH):
            warnings.append(
                f"{info.interface}: Tx Bias {info.tx_bias} mA outside "
                f"[{BIAS_WARN_LOW}, {BIAS_WARN_HIGH}]"
            )

    return SectionResult(name, len(errors) == 0, warnings, errors)


def run_transceiver_test(
    client: paramiko.SSHClient,
    pairs: List[Tuple[str, str]],
    timeout: int,
    debug: bool = False,
) -> List[SectionResult]:
    """Test 1: Validate transceiver info for every interface in every pair."""
    results: List[SectionResult] = []
    all_ifaces = [iface for pair in pairs for iface in pair]

    # DNOS uses a global command: "show int transceiver"
    print("  Fetching transceiver data (show int transceiver) ...")
    global_output = run_show_command(client, "show int transceiver", timeout)
    if debug:
        print(f"    [DEBUG] show int transceiver (first 80 lines):")
        for ln in global_output.splitlines()[:80]:
            print(f"    [DEBUG]   {ln}")

    for iface in all_ifaces:
        print(f"  Checking transceiver on {iface} ...")
        output = _extract_interface_section(global_output, iface)
        if debug:
            print(f"    [DEBUG] Extracted section for {iface}:")
            for ln in output.splitlines():
                print(f"    [DEBUG]   {ln}")

        info = parse_transceiver_output(iface, output)

        print(f"    Vendor:      {info.vendor or 'N/A'}")
        print(f"    Part Number: {info.part_number or 'N/A'}")
        print(f"    Serial:      {info.serial_number or 'N/A'}")
        _v = lambda v, u: f"{v} {u}" if v is not None else "N/A"  # noqa: E731
        print(f"    Temperature: {_v(info.temperature, 'C')}")
        print(f"    Voltage:     {_v(info.voltage, 'V')}")
        print(f"    Tx Power:    {_v(info.tx_power, 'dBm')}")
        print(f"    Rx Power:    {_v(info.rx_power, 'dBm')}")
        print(f"    Tx Bias:     {_v(info.tx_bias, 'mA')}")

        results.append(validate_transceiver(info))

    return results


# ===================================================================
# TEST 2 :  Traffic / Counter Validation
# ===================================================================
def parse_counters(output: str, interface: str) -> InterfaceCounters:
    """Parse counters from ``show interfaces counters <iface>`` (DNOS).

    DNOS format::

        RX octets:                                        0 (   0 bps / 0 Mbps)
        RX frames:                                        0 (   0 fps / 0 Mfps)
        TX octets:                            9453790460672 (   ... )
        TX frames:                               3365247613 (   ... )
        RX errors:                                        0
        TX errors:                                        0
    """
    c = InterfaceCounters(interface=interface)

    for line in output.splitlines():
        lower = line.lower().strip()
        if ":" not in lower:
            continue

        def _first_int(s: str) -> Optional[int]:
            m = re.search(r":\s*(\d+)", s)
            return int(m.group(1)) if m else None

        # Match DNOS counter names exactly
        if lower.startswith("rx frames"):
            v = _first_int(line)
            if v is not None:
                c.rx_packets = v
        elif lower.startswith("tx frames"):
            v = _first_int(line)
            if v is not None:
                c.tx_packets = v
        elif lower.startswith("rx octets"):
            v = _first_int(line)
            if v is not None:
                c.rx_bytes = v
        elif lower.startswith("tx octets"):
            v = _first_int(line)
            if v is not None:
                c.tx_bytes = v
        elif lower.startswith("rx errors"):
            v = _first_int(line)
            if v is not None:
                c.rx_errors = v
        elif lower.startswith("tx errors"):
            v = _first_int(line)
            if v is not None:
                c.tx_errors = v

    return c


def _snapshot_counters(
    client: paramiko.SSHClient, interfaces: List[str], timeout: int,
    debug: bool = False,
) -> Dict[str, InterfaceCounters]:
    counters: Dict[str, InterfaceCounters] = {}
    for iface in interfaces:
        # DNOS: "show interfaces counters <iface>"
        output = run_show_command(
            client, f"show interfaces counters {iface}", timeout
        )
        if debug:
            print(f"    [DEBUG] show interfaces counters {iface}:")
            for ln in output.splitlines():
                print(f"    [DEBUG]   {ln}")
        counters[iface] = parse_counters(output, iface)
    return counters


def run_traffic_test(
    client: paramiko.SSHClient,
    pairs: List[Tuple[str, str]],
    timeout: int,
    counter_wait: int,
    debug: bool = False,
) -> List[SectionResult]:
    """Test 2: Verify traffic is flowing through each pair (BD configured manually)."""
    results: List[SectionResult] = []
    all_ifaces = [iface for pair in pairs for iface in pair]

    print("  Taking counter snapshot (before) ...")
    before = _snapshot_counters(client, all_ifaces, timeout, debug)

    print(f"  Waiting {counter_wait}s for traffic to flow ...")
    time.sleep(counter_wait)

    print("  Taking counter snapshot (after) ...")
    after = _snapshot_counters(client, all_ifaces, timeout, debug)

    for idx, (a, b) in enumerate(pairs):
        label = f"traffic_pair{idx + 1}_{a}_{b}"
        warnings: List[str] = []
        errors: List[str] = []

        for iface in (a, b):
            bef = before[iface]
            aft = after[iface]
            drx  = aft.rx_packets - bef.rx_packets
            dtx  = aft.tx_packets - bef.tx_packets
            drxb = aft.rx_bytes   - bef.rx_bytes
            dtxb = aft.tx_bytes   - bef.tx_bytes
            derr_rx  = aft.rx_errors - bef.rx_errors
            derr_tx  = aft.tx_errors - bef.tx_errors
            ddrop_rx = aft.rx_drops  - bef.rx_drops
            ddrop_tx = aft.tx_drops  - bef.tx_drops

            print(f"    {iface}: Rx +{drx} pkts (+{drxb} bytes), "
                  f"Tx +{dtx} pkts (+{dtxb} bytes)")

            if drx == 0 and dtx == 0:
                errors.append(
                    f"{iface}: No traffic detected "
                    "(Rx/Tx counters did not increment)"
                )
            elif drx == 0:
                warnings.append(f"{iface}: Rx packets did not increment (Tx +{dtx})")
            elif dtx == 0:
                warnings.append(f"{iface}: Tx packets did not increment (Rx +{drx})")

            if derr_rx > 0:
                errors.append(f"{iface}: Rx errors increased by {derr_rx}")
            if derr_tx > 0:
                errors.append(f"{iface}: Tx errors increased by {derr_tx}")
            if ddrop_rx > 0:
                errors.append(f"{iface}: Rx drops increased by {ddrop_rx}")
            if ddrop_tx > 0:
                errors.append(f"{iface}: Tx drops increased by {ddrop_tx}")

        results.append(SectionResult(label, len(errors) == 0, warnings, errors))

    return results


# ===================================================================
# TEST 3 :  Admin-State Toggle
# ===================================================================
def get_interface_state(
    client: paramiko.SSHClient, interface: str, timeout: int,
    debug: bool = False,
) -> Tuple[str, str]:
    """Return (admin_state, oper_status) for *interface*.

    DNOS format (single comma-separated line)::

        Admin state: enabled, Physical link state: up, Operational state: up, Uptime: ...
    """
    output = run_show_command(client, f"show interfaces {interface}", timeout)
    if debug:
        print(f"    [DEBUG] show interfaces {interface}:")
        for ln in output.splitlines():
            print(f"    [DEBUG]   {ln}")
    admin = "unknown"
    oper = "unknown"
    for line in output.splitlines():
        lower = line.lower().strip()
        # DNOS: "Admin state: enabled, ..." (must be start of line, not "IPv6 Admin" or "EFM: Admin")
        if admin == "unknown" and lower.startswith("admin state:"):
            m = re.search(r"admin\s+state:\s*(\w+)", lower)
            if m:
                admin = m.group(1).strip()
        # DNOS: "Operational state: up"
        if oper == "unknown":
            m = re.search(r"operational\s+state:\s*(\w+)", lower)
            if m:
                oper = m.group(1).strip()
        # If both found, stop searching
        if admin != "unknown" and oper != "unknown":
            break
    return (admin, oper)


def set_admin_state(
    client: paramiko.SSHClient,
    interface: str,
    enabled: bool,
    timeout: int,
    debug: bool = False,
) -> Tuple[bool, str]:
    """Set admin-state on *interface*.  Returns (success, detail)."""
    state = "enabled" if enabled else "disabled"
    output = run_config_commands(
        client,
        ["configure", "interfaces", interface,
         f"admin-state {state}", "exit", "commit", "exit"],
        timeout,
    )
    if debug:
        print(f"    [DEBUG] set admin-state {state} on {interface}:")
        for ln in output.splitlines():
            print(f"    [DEBUG]   {ln}")
    err, err_lines = has_cli_error(output)
    if err:
        return False, "; ".join(err_lines)
    return True, output


def _check_state(
    client: paramiko.SSHClient,
    iface: str,
    expect_admin: str,
    expect_oper: str,
    timeout: int,
    errors: List[str],
    label: str,
    debug: bool = False,
) -> None:
    """Helper: read interface state and append to *errors* if mismatch."""
    admin, oper = get_interface_state(client, iface, timeout, debug)
    if expect_admin and expect_admin not in admin:
        errors.append(
            f"{iface}: admin-state should contain '{expect_admin}', "
            f"got '{admin}' ({label})"
        )
    if expect_oper and expect_oper not in oper:
        errors.append(
            f"{iface}: oper-status should contain '{expect_oper}', "
            f"got '{oper}' ({label})"
        )


def run_admin_state_test(
    client: paramiko.SSHClient,
    pairs: List[Tuple[str, str]],
    timeout: int,
    settle: int,
    debug: bool = False,
) -> List[SectionResult]:
    """Test 3: Toggle admin-state on each pair and verify transitions."""
    results: List[SectionResult] = []

    for idx, (a, b) in enumerate(pairs):
        label = f"admin_state_pair{idx + 1}_{a}_{b}"
        warnings: List[str] = []
        errors: List[str] = []

        # ------ ensure both up ------
        print(f"  Pair {idx+1}: Ensuring both sides are up ...")
        set_admin_state(client, a, True, timeout, debug)
        set_admin_state(client, b, True, timeout, debug)
        time.sleep(settle)

        _check_state(client, a, "enabled", "up", timeout, errors, "initial", debug)
        _check_state(client, b, "enabled", "up", timeout, errors, "initial", debug)

        # ------ Scenario 1: shut side-A ------
        print(f"  Pair {idx+1}: Shutting side-A ({a}) ...")
        ok, msg = set_admin_state(client, a, False, timeout, debug)
        if not ok:
            errors.append(f"{a}: Failed to disable: {msg}")
        time.sleep(settle)
        _check_state(client, a, "disabled", "down", timeout, errors, "after shut A", debug)
        _check_state(client, b, "",         "down", timeout, errors, "peer of shut A", debug)

        print(f"  Pair {idx+1}: Bringing side-A ({a}) back up ...")
        ok, msg = set_admin_state(client, a, True, timeout, debug)
        if not ok:
            errors.append(f"{a}: Failed to enable: {msg}")
        time.sleep(settle)
        _check_state(client, a, "enabled", "up", timeout, errors, "recovery A", debug)
        _check_state(client, b, "",        "up", timeout, errors, "peer recovery A", debug)

        # ------ Scenario 2: shut side-B ------
        print(f"  Pair {idx+1}: Shutting side-B ({b}) ...")
        ok, msg = set_admin_state(client, b, False, timeout, debug)
        if not ok:
            errors.append(f"{b}: Failed to disable: {msg}")
        time.sleep(settle)
        _check_state(client, b, "disabled", "down", timeout, errors, "after shut B", debug)
        _check_state(client, a, "",         "down", timeout, errors, "peer of shut B", debug)

        print(f"  Pair {idx+1}: Bringing side-B ({b}) back up ...")
        ok, msg = set_admin_state(client, b, True, timeout, debug)
        if not ok:
            errors.append(f"{b}: Failed to enable: {msg}")
        time.sleep(settle)
        _check_state(client, a, "enabled", "up", timeout, errors, "recovery B", debug)
        _check_state(client, b, "",        "up", timeout, errors, "peer recovery B", debug)

        # ------ Scenario 3: shut both ------
        print(f"  Pair {idx+1}: Shutting both sides ...")
        ok_a, msg_a = set_admin_state(client, a, False, timeout, debug)
        ok_b, msg_b = set_admin_state(client, b, False, timeout, debug)
        if not ok_a:
            errors.append(f"{a}: Failed to disable: {msg_a}")
        if not ok_b:
            errors.append(f"{b}: Failed to disable: {msg_b}")
        time.sleep(settle)
        _check_state(client, a, "disabled", "down", timeout, errors, "both shut", debug)
        _check_state(client, b, "disabled", "down", timeout, errors, "both shut", debug)

        print(f"  Pair {idx+1}: Bringing both sides back up ...")
        ok_a, msg_a = set_admin_state(client, a, True, timeout, debug)
        ok_b, msg_b = set_admin_state(client, b, True, timeout, debug)
        if not ok_a:
            errors.append(f"{a}: Failed to enable: {msg_a}")
        if not ok_b:
            errors.append(f"{b}: Failed to enable: {msg_b}")
        time.sleep(settle)
        _check_state(client, a, "enabled", "up", timeout, errors, "both recovered", debug)
        _check_state(client, b, "enabled", "up", timeout, errors, "both recovered", debug)

        results.append(SectionResult(label, len(errors) == 0, warnings, errors))

    return results


# ===================================================================
# Summary / Reporting
# ===================================================================
def print_summary(all_results: List[SectionResult]) -> bool:
    """Print final summary.  Returns True when every test passed."""
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    any_failed = False
    for r in all_results:
        tag = "PASS" if r.ok else "FAIL"
        if r.ok and r.warnings:
            tag = "WARN"
        print(f"  [{tag}]  {r.name}")
        for msg in r.errors:
            print(f"          ERROR: {msg}")
        for msg in r.warnings:
            print(f"          WARN:  {msg}")
        if not r.ok:
            any_failed = True
    print("=" * 70)
    if any_failed:
        print("RESULT: FAIL - Some tests did not pass.")
    else:
        print("RESULT: PASS - All tests passed.")
    print("=" * 70)
    return not any_failed


# ===================================================================
# main
# ===================================================================
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Transceiver validation for DNOS"
    )
    parser.add_argument("--host",
                        help="Device hostname, IP, or serial number")
    parser.add_argument("--user", default="dnroot",
                        help="SSH user (default: dnroot)")
    parser.add_argument("--password", default="dnroot",
                        help="SSH password (default: dnroot)")
    parser.add_argument("--num-pairs", type=int, default=0,
                        help="Number of transceiver pairs to test "
                             "(prompted if not given)")
    parser.add_argument("--pairs", nargs="*", type=parse_pair_arg,
                        help="Pairs as ifaceA,ifaceB  (e.g. "
                             "ge100-0/0/1,ge100-0/0/2 ge100-0/0/3,ge100-0/0/4)")
    parser.add_argument("--timeout", type=int, default=30,
                        help="SSH / command timeout in seconds (default: 30)")
    parser.add_argument("--counter-wait", type=int, default=DEFAULT_COUNTER_WAIT,
                        help="Seconds between counter snapshots "
                             f"(default: {DEFAULT_COUNTER_WAIT})")
    parser.add_argument("--settle-time", type=int, default=ADMIN_STATE_SETTLE,
                        help="Seconds to wait after admin-state change "
                             f"(default: {ADMIN_STATE_SETTLE})")
    parser.add_argument("--skip-transceiver", action="store_true",
                        help="Skip transceiver info test")
    parser.add_argument("--skip-traffic", action="store_true",
                        help="Skip traffic counter test")
    parser.add_argument("--skip-admin", action="store_true",
                        help="Skip admin-state toggle test")
    parser.add_argument("--debug", action="store_true",
                        help="Dump raw CLI output for debugging parsers")
    args = parser.parse_args()

    # ---- resolve host ----
    host = prompt_if_missing(args.host, "Device hostname / IP / SN: ")

    # ---- resolve pairs ----
    pairs: List[Tuple[str, str]] = []
    if args.pairs:
        # Pairs supplied on CLI
        pairs = list(args.pairs)
    else:
        # Ask how many pairs to test
        num_pairs = args.num_pairs
        if num_pairs <= 0:
            while True:
                raw = input("How many transceiver pairs to test? ").strip()
                if raw.isdigit() and int(raw) > 0:
                    num_pairs = int(raw)
                    break
                print("  Please enter a positive number.")
        for n in range(1, num_pairs + 1):
            pairs.append(prompt_pair(n))

    print(f"\nTarget device: {host}")
    for i, (a, b) in enumerate(pairs):
        print(f"  Pair {i + 1}: {a} <-> {b}")
    print()

    # ---- connect ----
    print("Connecting to device ...")
    try:
        client = create_ssh_client(host, args.user, args.password, args.timeout)
    except Exception as exc:
        print(f"FAIL: Cannot connect to {host}: {exc}")
        return 1
    print("Connected.\n")

    all_results: List[SectionResult] = []

    try:
        # --- Test 1 ---
        if not args.skip_transceiver:
            print("=" * 50)
            print("TEST 1: Transceiver Info Validation")
            print("=" * 50)
            t1 = run_transceiver_test(client, pairs, args.timeout, args.debug)
            all_results.extend(t1)
            for r in t1:
                tag = "PASS" if r.ok else "FAIL"
                print(f"  -> [{tag}] {r.name}")
            print()

        # --- Test 2 ---
        if not args.skip_traffic:
            print("=" * 50)
            print("TEST 2: Traffic / Counter Validation")
            print("=" * 50)
            print("  (Ensure BD is configured and traffic is flowing)")
            t2 = run_traffic_test(
                client, pairs, args.timeout, args.counter_wait, args.debug
            )
            all_results.extend(t2)
            for r in t2:
                tag = "PASS" if r.ok else "FAIL"
                print(f"  -> [{tag}] {r.name}")
            print()

        # --- Test 3 ---
        if not args.skip_admin:
            print("=" * 50)
            print("TEST 3: Admin State Toggle")
            print("=" * 50)
            t3 = run_admin_state_test(
                client, pairs, args.timeout, args.settle_time, args.debug
            )
            all_results.extend(t3)
            for r in t3:
                tag = "PASS" if r.ok else "FAIL"
                print(f"  -> [{tag}] {r.name}")
            print()
    finally:
        client.close()

    # ---- final summary ----
    all_passed = print_summary(all_results)
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
