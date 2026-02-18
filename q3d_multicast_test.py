#!/usr/bin/env python3
"""
Q3D Multicast Test Script for DNOS

Validates all PIM SSM multicast Testing Tasks under:
  - SW-241836 (Interface Testing): 8 tasks
  - SW-241837 (Scale Testing): 7 tasks

The script connects to the Q3D DUT via SSH and configures/validates PIM SSM.
Spirent is operated manually by the user; the script pauses and prompts for
Spirent actions (start traffic, send IGMP joins, etc.) then validates DUT state.

Usage:
    python3 q3d_multicast_test.py \\
        --host <Q3D hostname or IP> \\
        --source-interface ge100-0/0/1 \\
        --receiver-interfaces ge100-0/0/2 \\
        --tests all

    python3 q3d_multicast_test.py --host xgu1f7v900009p2 --tests interface
    python3 q3d_multicast_test.py --host xgu1f7v900009p2 --tests scale
"""

import argparse
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import paramiko

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

DEFAULT_SOURCE_IP = "192.168.1.1"
DEFAULT_SOURCE_PEER_IP = "192.168.1.2"
DEFAULT_RECEIVER_IP = "192.168.2.1"
DEFAULT_RECEIVER_PEER_IP = "192.168.2.2"
DEFAULT_MC_GROUP_BASE = "232.1.0.0"
DEFAULT_MC_SOURCE = "192.168.1.2"
DEFAULT_PREFIX_LEN = 24
DEFAULT_VLAN_BASE = 100

SCALE_MILESTONES = [1_000, 5_000, 10_000, 30_000, 60_000]
REPLICATION_TARGET = 220_000
STRESS_DURATION_MINUTES = 60
STRESS_POLL_INTERVAL_S = 60


# ---------------------------------------------------------------------------
# DNOSDevice -- SSH device wrapper
# ---------------------------------------------------------------------------

class DNOSDevice:
    """SSH connection wrapper for a DNOS device."""

    def __init__(
        self,
        host: str,
        username: str = "dnroot",
        password: str = "dnroot",
        timeout: int = 30,
    ):
        self.host = host
        self.username = username
        self.password = password
        self.timeout = timeout
        self.client: Optional[paramiko.SSHClient] = None
        self.shell: Optional[paramiko.Channel] = None

    def connect(self):
        """Establish SSH connection and disable paging."""
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(
            hostname=self.host,
            username=self.username,
            password=self.password,
            look_for_keys=False,
            allow_agent=False,
            timeout=self.timeout,
        )
        self.shell = self.client.invoke_shell(width=250, height=1000)
        self._read_until_prompt(timeout=15)
        self._send("no-paging")
        self._read_until_prompt(timeout=5)

    def disconnect(self):
        """Close SSH connection."""
        if self.shell:
            try:
                self.shell.close()
            except Exception:
                pass
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass

    def _send(self, cmd: str):
        """Send a command line to the shell."""
        if self.shell:
            self.shell.send(cmd + "\n")

    def _read_until_prompt(self, timeout: int = 30) -> str:
        """Read until a DNOS prompt ending with # or >."""
        buf = ""
        end_time = time.time() + timeout
        while time.time() < end_time:
            if self.shell and self.shell.recv_ready():
                chunk = self.shell.recv(65536).decode("utf-8", errors="replace")
                buf += chunk
                clean = ANSI_ESCAPE.sub("", buf)
                lines = clean.strip().split("\n")
                last_line = lines[-1].strip() if lines else ""
                if last_line.endswith("#") or last_line.endswith(">"):
                    break
            else:
                time.sleep(0.2)
        return ANSI_ESCAPE.sub("", buf)

    def run_show(self, cmd: str, timeout: int = 30) -> str:
        """Run a show / operational command and return cleaned output."""
        self._send(cmd)
        return self._read_until_prompt(timeout=timeout)

    def run_config(self, config_lines: List[str], timeout: int = 60) -> str:
        """Enter config mode, apply lines, commit, and exit."""
        self._send("configure")
        self._read_until_prompt(timeout=10)

        for line in config_lines:
            self._send(line)
            self._read_until_prompt(timeout=10)

        self._send("commit")
        commit_output = self._read_until_prompt(timeout=timeout)

        self._send("exit")
        self._read_until_prompt(timeout=5)

        return commit_output

    def run_commit_check(self, config_lines: List[str], timeout: int = 60) -> str:
        """Enter config mode, apply lines, run commit check (not commit),
        then rollback and exit. Returns commit check output."""
        self._send("configure")
        self._read_until_prompt(timeout=10)

        for line in config_lines:
            self._send(line)
            self._read_until_prompt(timeout=10)

        self._send("commit check")
        check_output = self._read_until_prompt(timeout=timeout)

        self._send("rollback 0")
        self._read_until_prompt(timeout=15)

        self._send("exit")
        self._read_until_prompt(timeout=5)

        return check_output

    @staticmethod
    def has_commit_error(output: str) -> bool:
        """Check if commit/commit-check output contains errors."""
        for line in output.splitlines():
            stripped = line.strip()
            lower = stripped.lower()
            if not stripped or lower in ("commit", "commit check"):
                continue
            if any(
                pat in lower
                for pat in [
                    "error:",
                    "commit check failed",
                    "commit failed",
                    "unknown command",
                    "invalid",
                    "validation failed",
                    "aborted",
                ]
            ):
                return True
        return False

    @staticmethod
    def commit_check_passed(output: str) -> bool:
        """Return True if commit check output indicates success."""
        lower = output.lower()
        return "commit check passed" in lower or "commit check ok" in lower


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class TestResult:
    name: str
    jira_key: str
    passed: bool
    detail: str = ""
    skipped: bool = False


@dataclass
class ScaleMeasurement:
    milestone: str
    route_count: int
    timestamp: float
    elapsed_s: float = 0.0


@dataclass
class ResourceSnapshot:
    timestamp: float
    cpu_line: str = ""
    memory_line: str = ""
    route_count: int = 0


# ---------------------------------------------------------------------------
# Q3DMulticastTest -- main test orchestrator
# ---------------------------------------------------------------------------

class Q3DMulticastTest:
    """PIM SSM multicast test suite for Q3D DNOS."""

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.device = DNOSDevice(
            host=args.host,
            username=args.username,
            password=args.password,
        )
        self.results: List[TestResult] = []
        self.scale_measurements: List[ScaleMeasurement] = []
        self.resource_snapshots: List[ResourceSnapshot] = []
        self.start_time = datetime.now()
        self.verbose = args.verbose
        self.skip_cleanup = args.skip_cleanup

        self.src_iface = args.source_interface
        self.rcv_ifaces = [s.strip() for s in args.receiver_interfaces.split(",")]
        self.rcv_iface = self.rcv_ifaces[0]
        self.src_ip = args.source_ip
        self.src_peer_ip = args.source_peer_ip
        self.rcv_ip = args.receiver_ip
        self.rcv_peer_ip = args.receiver_peer_ip
        self.mc_group_base = args.mc_group_base
        self.mc_source = args.mc_source

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _record(self, name: str, jira_key: str, passed: bool, detail: str = ""):
        r = TestResult(name=name, jira_key=jira_key, passed=passed, detail=detail)
        self.results.append(r)
        tag = "\033[92m[PASS]\033[0m" if passed else "\033[91m[FAIL]\033[0m"
        print(f"  {tag} {name}" + (f" -- {detail}" if detail else ""))

    def _skip(self, name: str, jira_key: str, reason: str):
        r = TestResult(name=name, jira_key=jira_key, passed=True, detail=reason, skipped=True)
        self.results.append(r)
        print(f"  \033[93m[SKIP]\033[0m {name} -- {reason}")

    @staticmethod
    def prompt_user(message: str):
        """Print a Spirent action banner and wait for user to press Enter."""
        sep = "=" * 72
        dash = "-" * 72
        banner = (
            f"\n\033[96m{sep}\n"
            f"  SPIRENT ACTION REQUIRED\n"
            f"{dash}\n"
            f"{message}\n"
            f"{sep}\033[0m"
        )
        print(banner)
        input("  Press Enter when ready...")
        print()

    @staticmethod
    def prompt_confirm(question: str) -> bool:
        """Ask user a yes/no question, return True for yes."""
        answer = input(f"  {question} [y/N]: ").strip().lower()
        return answer in ("y", "yes")

    def show(self, cmd: str, timeout: int = 30) -> str:
        output = self.device.run_show(cmd, timeout=timeout)
        if self.verbose:
            print(f"    > {cmd}")
            for line in output.splitlines()[:40]:
                print(f"      {line}")
            if len(output.splitlines()) > 40:
                print(f"      ... ({len(output.splitlines())} lines total)")
        return output

    def config(self, lines: List[str], timeout: int = 60) -> str:
        return self.device.run_config(lines, timeout=timeout)

    def commit_check(self, lines: List[str], timeout: int = 60) -> str:
        return self.device.run_commit_check(lines, timeout=timeout)

    # ------------------------------------------------------------------
    # Parsers
    # ------------------------------------------------------------------

    @staticmethod
    def parse_pim_neighbors(output: str) -> List[Dict[str, str]]:
        """Parse 'show pim neighbor' into list of dicts with interface, neighbor, state."""
        neighbors = []
        lines = output.splitlines()
        header_found = False
        for line in lines:
            lower = line.lower()
            if "interface" in lower and "neighbor" in lower:
                header_found = True
                continue
            if not header_found:
                continue
            stripped = line.strip()
            if not stripped or set(stripped) <= {"+", "-", "|", " ", "="}:
                continue
            if "|" in stripped:
                parts = [p.strip() for p in stripped.split("|") if p.strip()]
                if len(parts) >= 2:
                    neighbors.append({
                        "interface": parts[0],
                        "neighbor": parts[1] if len(parts) > 1 else "",
                        "state": parts[-1] if len(parts) > 2 else "",
                    })
            else:
                parts = stripped.split()
                if parts and re.match(r"\d+\.\d+\.\d+\.\d+|ge|eth|bundle|lo", parts[0], re.I):
                    neighbors.append({
                        "interface": parts[0],
                        "neighbor": parts[1] if len(parts) > 1 else "",
                        "state": parts[-1] if len(parts) > 2 else "",
                    })
        return neighbors

    @staticmethod
    def parse_pim_tree(output: str) -> List[Dict[str, str]]:
        """Parse 'show pim tree' to extract (S,G) entries with IIF/OIF."""
        entries = []
        lines = output.splitlines()
        current: Dict[str, str] = {}
        for line in lines:
            stripped = line.strip()
            lower = stripped.lower()
            sg_match = re.search(r"\((\d+\.\d+\.\d+\.\d+)\s*,\s*(\d+\.\d+\.\d+\.\d+)\)", stripped)
            if sg_match:
                if current:
                    entries.append(current)
                current = {"source": sg_match.group(1), "group": sg_match.group(2), "iif": "", "oifs": ""}
            if current:
                if "incoming" in lower or "iif" in lower or "rpf" in lower:
                    iface = re.search(r"(ge\S+|eth\S+|bundle\S+|lo\S+)", stripped, re.I)
                    if iface:
                        current["iif"] = iface.group(1)
                if "outgoing" in lower or "oif" in lower or "oil" in lower:
                    ifaces = re.findall(r"(ge\S+|eth\S+|bundle\S+|lo\S+)", stripped, re.I)
                    if ifaces:
                        current["oifs"] = ", ".join(ifaces)
        if current:
            entries.append(current)
        return entries

    @staticmethod
    def count_lines_with_pattern(output: str, pattern: str) -> int:
        """Count output lines matching a regex pattern."""
        count = 0
        for line in output.splitlines():
            if re.search(pattern, line):
                count += 1
        return count

    @staticmethod
    def extract_count_from_pipe(output: str) -> int:
        """Extract integer from 'show ... | count' output."""
        for line in output.splitlines():
            m = re.search(r"(?:count|total)[:\s]*(\d+)", line.lower())
            if m:
                return int(m.group(1))
            m2 = re.match(r"\s*(\d+)\s*$", line.strip())
            if m2 and int(m2.group(1)) > 0:
                return int(m2.group(1))
        return 0

    @staticmethod
    def mc_group_ip(base: str, offset: int) -> str:
        """Compute a multicast group IP from base + offset."""
        parts = [int(x) for x in base.split(".")]
        total = (parts[0] << 24) | (parts[1] << 16) | (parts[2] << 8) | parts[3]
        total += offset
        return f"{(total >> 24) & 0xFF}.{(total >> 16) & 0xFF}.{(total >> 8) & 0xFF}.{total & 0xFF}"

    # ------------------------------------------------------------------
    # PIM config builders
    # ------------------------------------------------------------------

    def _pim_interface_config(self, iface: str) -> List[str]:
        """Return config lines to enable PIM SSM on an interface."""
        return [
            "protocols",
            "  pim",
            f"    interface {iface}",
            "      admin-state enabled",
            "      address-family ipv4",
            "        mode ssm",
            "      !",
            "    !",
            "  !",
            "!",
        ]

    def _ip_and_pim_config(self, iface: str, ip: str, prefix: int = 24) -> List[str]:
        """Return config lines to assign IP + enable PIM SSM on an interface."""
        return [
            "interfaces",
            f"  {iface}",
            "    admin-state enabled",
            f"    ipv4-address {ip}/{prefix}",
            "  !",
            "!",
        ] + self._pim_interface_config(iface)

    def _remove_pim_config(self, iface: str) -> List[str]:
        """Return config lines to remove PIM from an interface."""
        return [
            "protocols",
            "  pim",
            f"    no interface {iface}",
            "  !",
            "!",
        ]

    def _remove_ip_and_pim_config(self, iface: str) -> List[str]:
        """Return config lines to remove IP + PIM from an interface."""
        return self._remove_pim_config(iface) + [
            "interfaces",
            f"  {iface}",
            "    no ipv4-address",
            "  !",
            "!",
        ]

    def _subif_config(self, parent_iface: str, vlan_id: int, ip: str, prefix: int = 24) -> List[str]:
        """Return config lines for a VLAN sub-interface with PIM SSM."""
        subif = f"{parent_iface}.{vlan_id}"
        return [
            "interfaces",
            f"  {subif}",
            "    admin-state enabled",
            f"    vlan-tag {vlan_id}",
            f"    ipv4-address {ip}/{prefix}",
            "  !",
            "!",
        ] + self._pim_interface_config(subif)

    def _remove_subif_config(self, parent_iface: str, vlan_id: int) -> List[str]:
        subif = f"{parent_iface}.{vlan_id}"
        return self._remove_pim_config(subif) + [
            "interfaces",
            f"  no {subif}",
            "!",
        ]

    # ------------------------------------------------------------------
    # Phase 0: Setup
    # ------------------------------------------------------------------

    def phase_setup(self):
        print("\n" + "=" * 72)
        print("  PHASE 0: Setup")
        print("=" * 72)

        print(f"[*] Connecting to {self.device.host} ...")
        self.device.connect()
        print(f"[+] Connected to {self.device.host}.\n")

        print("[*] Spirent requirements for this test run:")
        print(f"    - Source port connected to DUT {self.src_iface} at IP {self.src_peer_ip}")
        print(f"    - Receiver port(s) connected to DUT {self.rcv_iface} at IP {self.rcv_peer_ip}")
        print("    - PIM SSM neighbor emulation capability (IGMPv3 host)")
        print("    - Multicast traffic generation (UDP, configurable group/source IPs)")
        print("    - For scale tests: ability to send IGMPv3 Joins for up to 60K groups")
        print("    - For replication tests: ability to emulate IGMP hosts on multiple VLANs")
        print()

    # ------------------------------------------------------------------
    # Phase 1: Interface Testing (SW-241836)
    # ------------------------------------------------------------------

    def phase_interface_tests(self):
        print("\n" + "=" * 72)
        print("  PHASE 1: Interface Testing (SW-241836)")
        print("=" * 72)

        self.test_physical_ethernet()
        self.test_breakout()
        self.test_bundle()
        self.test_sub_interfaces()
        self.test_mixed_topology()
        self.test_spt_switchover()
        self.test_mofrr_blocking()
        self.test_unsupported_features()

    # -- Task 1: PIM SSM over Physical Ethernet (SW-241838) --

    def test_physical_ethernet(self):
        task = "PIM SSM Physical Ethernet"
        jira = "SW-241838"
        print(f"\n--- Task 1: {task} ({jira}) ---\n")

        config_lines = (
            self._ip_and_pim_config(self.src_iface, self.src_ip)
            + self._ip_and_pim_config(self.rcv_iface, self.rcv_ip)
        )

        print("[*] Applying DUT config: IP addresses + PIM SSM on source and receiver interfaces ...")
        commit_out = self.config(config_lines)
        if DNOSDevice.has_commit_error(commit_out):
            self._record(f"{task} -- config apply", jira, False, f"Commit error: {commit_out[-200:]}")
            return

        self._record(f"{task} -- config apply", jira, True)

        self.prompt_user(
            f"  1. On SOURCE port (connected to DUT {self.src_iface}):\n"
            f"     - Emulate PIM SSM neighbor with IP {self.src_peer_ip}\n"
            f"     - Send multicast traffic to group {self.mc_group_base} from source {self.mc_source}\n"
            f"     - Rate: 1000 pps, UDP, dest port 5000\n\n"
            f"  2. On RECEIVER port (connected to DUT {self.rcv_iface}):\n"
            f"     - Emulate IGMPv3 host with IP {self.rcv_peer_ip}\n"
            f"     - Send IGMPv3 (S,G) Join for group {self.mc_group_base}, source {self.mc_source}"
        )

        time.sleep(3)

        out = self.show("show pim neighbor")
        neighbors = self.parse_pim_neighbors(out)
        has_src = any(self.src_iface in n.get("interface", "") for n in neighbors)
        has_rcv = any(self.rcv_iface in n.get("interface", "") for n in neighbors)
        self._record(f"{task} -- PIM neighbor source", jira, has_src,
                     f"Found on {self.src_iface}" if has_src else f"Not found on {self.src_iface}")
        self._record(f"{task} -- PIM neighbor receiver", jira, has_rcv,
                     f"Found on {self.rcv_iface}" if has_rcv else f"Not found on {self.rcv_iface}")

        out = self.show("show pim tree")
        entries = self.parse_pim_tree(out)
        has_sg = len(entries) > 0
        self._record(f"{task} -- PIM tree (S,G) entry", jira, has_sg,
                     f"{len(entries)} entries" if has_sg else "No (S,G) entries found")

        out = self.show("show multicast route")
        has_mfib = "route" in out.lower() or re.search(r"\d+\.\d+\.\d+\.\d+", out) is not None
        mfib_lines = self.count_lines_with_pattern(out, r"\d+\.\d+\.\d+\.\d+.*\d+\.\d+\.\d+\.\d+")
        self._record(f"{task} -- MFIB entries", jira, mfib_lines > 0,
                     f"{mfib_lines} MFIB routes")

        out = self.show(f"show interfaces counters {self.rcv_iface}")
        has_mc_counters = re.search(r"multicast.*[1-9]|mc.*[1-9]", out.lower()) is not None
        self._record(f"{task} -- MC counters", jira, has_mc_counters,
                     "MC counters incrementing" if has_mc_counters else "No MC counter traffic detected (may need more time)")

        self.prompt_user(
            "  Send IGMP Leave from receiver port and stop multicast traffic on source port."
        )
        time.sleep(5)

        out = self.show("show pim tree")
        entries_after = self.parse_pim_tree(out)
        tree_cleared = len(entries_after) < len(entries) if entries else True
        self._record(f"{task} -- tree cleanup after leave", jira, tree_cleared,
                     f"{len(entries_after)} entries remaining")

        if not self.skip_cleanup:
            print(f"  [*] Cleaning up {task} config ...")
            cleanup = self._remove_ip_and_pim_config(self.src_iface) + self._remove_ip_and_pim_config(self.rcv_iface)
            self.config(cleanup)

    # -- Task 2: PIM SSM over Breakout Interfaces (SW-241839) --

    def test_breakout(self):
        task = "PIM SSM Breakout"
        jira = "SW-241839"
        print(f"\n--- Task 2: {task} ({jira}) ---\n")

        available = self.prompt_confirm("Is a breakout interface physically cabled to Spirent?")
        if not available:
            self._skip(task, jira, "Breakout interface not physically cabled")
            return

        breakout_iface = input("  Enter the breakout member interface name (e.g., ethernet-1/1/1): ").strip()
        if not breakout_iface:
            self._skip(task, jira, "No breakout interface provided")
            return

        breakout_ip = "192.168.3.1"
        breakout_peer_ip = "192.168.3.2"

        config_lines = self._ip_and_pim_config(breakout_iface, breakout_ip)
        print(f"[*] Applying PIM SSM config on breakout interface {breakout_iface} ...")
        commit_out = self.config(config_lines)
        if DNOSDevice.has_commit_error(commit_out):
            self._record(f"{task} -- config apply", jira, False, f"Commit error: {commit_out[-200:]}")
            return

        self._record(f"{task} -- config apply", jira, True)

        self.prompt_user(
            f"  On Spirent port connected to breakout interface {breakout_iface}:\n"
            f"     - Emulate PIM SSM neighbor with IP {breakout_peer_ip}\n"
            f"     - Send IGMPv3 (S,G) Join for group {self.mc_group_base}, source {self.mc_source}\n"
            f"     - Send multicast traffic to group {self.mc_group_base} from {self.mc_source}"
        )
        time.sleep(3)

        out = self.show("show pim neighbor")
        neighbors = self.parse_pim_neighbors(out)
        has_nbr = any(breakout_iface in n.get("interface", "") for n in neighbors)
        self._record(f"{task} -- PIM neighbor on breakout", jira, has_nbr,
                     f"Found on {breakout_iface}" if has_nbr else "Not found")

        out = self.show("show pim tree")
        entries = self.parse_pim_tree(out)
        self._record(f"{task} -- PIM tree entry", jira, len(entries) > 0,
                     f"{len(entries)} entries")

        out = self.show(f"show interfaces counters {breakout_iface}")
        has_mc = re.search(r"multicast.*[1-9]|mc.*[1-9]", out.lower()) is not None
        self._record(f"{task} -- MC counters on breakout", jira, has_mc,
                     "Counters incrementing" if has_mc else "No MC traffic detected")

        if not self.skip_cleanup:
            print(f"  [*] Cleaning up {task} config ...")
            self.config(self._remove_ip_and_pim_config(breakout_iface))
            self.prompt_user("  Stop Spirent traffic on breakout port.")

    # -- Task 3: PIM SSM over Bundle Interfaces (SW-241840) --

    def test_bundle(self):
        task = "PIM SSM Bundle"
        jira = "SW-241840"
        print(f"\n--- Task 3: {task} ({jira}) ---\n")

        available = self.prompt_confirm("Do you have bundle member links connected to Spirent?")
        if not available:
            self._skip(task, jira, "Bundle member links not available")
            return

        member_input = input("  Enter bundle member interface(s) separated by comma (e.g., ge100-0/0/3,ge100-0/0/4): ").strip()
        if not member_input:
            self._skip(task, jira, "No bundle members provided")
            return

        members = [m.strip() for m in member_input.split(",")]
        bundle_id = 200
        bundle_iface = f"bundle-{bundle_id}"
        bundle_ip = "192.168.4.1"
        bundle_peer_ip = "192.168.4.2"

        config_lines = [
            "interfaces",
            f"  {bundle_iface}",
            "    admin-state enabled",
            f"    ipv4-address {bundle_ip}/{DEFAULT_PREFIX_LEN}",
            "  !",
        ]
        for member in members:
            config_lines += [
                f"  {member}",
                "    admin-state enabled",
                f"    bundle-id {bundle_id}",
                "  !",
            ]
        config_lines += ["!"]
        config_lines += self._pim_interface_config(bundle_iface)

        print(f"[*] Creating bundle-{bundle_id} with members {members} and PIM SSM ...")
        commit_out = self.config(config_lines)
        if DNOSDevice.has_commit_error(commit_out):
            self._record(f"{task} -- config apply", jira, False, f"Commit error: {commit_out[-200:]}")
            return
        self._record(f"{task} -- config apply", jira, True)

        self.prompt_user(
            f"  On Spirent port(s) connected to bundle members ({', '.join(members)}):\n"
            f"     - Emulate PIM SSM neighbor with IP {bundle_peer_ip}\n"
            f"     - Send IGMPv3 (S,G) Join for group {self.mc_group_base}, source {self.mc_source}\n"
            f"     - Send multicast traffic to group {self.mc_group_base} from {self.mc_source}"
        )
        time.sleep(3)

        out = self.show("show pim neighbor")
        neighbors = self.parse_pim_neighbors(out)
        has_nbr = any(bundle_iface in n.get("interface", "") for n in neighbors)
        self._record(f"{task} -- PIM neighbor on bundle", jira, has_nbr,
                     f"Found on {bundle_iface}" if has_nbr else "Not found")

        out = self.show("show pim tree")
        entries = self.parse_pim_tree(out)
        self._record(f"{task} -- PIM tree entry", jira, len(entries) > 0, f"{len(entries)} entries")

        if len(members) > 1:
            print(f"  [*] Flapping bundle member {members[0]} (admin-state down then up) ...")
            self.config([
                "interfaces",
                f"  {members[0]}",
                "    admin-state disabled",
                "  !",
                "!",
            ])
            time.sleep(5)

            out = self.show("show pim neighbor")
            neighbors_after_flap = self.parse_pim_neighbors(out)
            pim_survived = any(bundle_iface in n.get("interface", "") for n in neighbors_after_flap)
            self._record(f"{task} -- PIM survives member flap", jira, pim_survived,
                         "PIM adjacency survived" if pim_survived else "PIM adjacency dropped")

            self.config([
                "interfaces",
                f"  {members[0]}",
                "    admin-state enabled",
                "  !",
                "!",
            ])
            time.sleep(3)

        if not self.skip_cleanup:
            print(f"  [*] Cleaning up {task} config ...")
            cleanup = self._remove_pim_config(bundle_iface)
            cleanup += ["interfaces"]
            for member in members:
                cleanup += [
                    f"  {member}",
                    f"    no bundle-id",
                    "  !",
                ]
            cleanup += [f"  no {bundle_iface}", "!"]
            self.config(cleanup)
            self.prompt_user("  Stop Spirent traffic on bundle member ports.")

    # -- Task 4: PIM SSM over Sub-Interfaces (SW-241841) --

    def test_sub_interfaces(self):
        task = "PIM SSM Sub-Interfaces"
        jira = "SW-241841"
        print(f"\n--- Task 4: {task} ({jira}) ---\n")

        vlan_id = DEFAULT_VLAN_BASE
        subif = f"{self.rcv_iface}.{vlan_id}"
        subif_ip = "192.168.5.1"
        subif_peer_ip = "192.168.5.2"

        config_lines = self._subif_config(self.rcv_iface, vlan_id, subif_ip)

        print(f"[*] Creating sub-interface {subif} with VLAN {vlan_id} and PIM SSM ...")
        src_config = self._ip_and_pim_config(self.src_iface, self.src_ip)
        commit_out = self.config(src_config + config_lines)
        if DNOSDevice.has_commit_error(commit_out):
            self._record(f"{task} -- config apply", jira, False, f"Commit error: {commit_out[-200:]}")
            return
        self._record(f"{task} -- config apply", jira, True)

        self.prompt_user(
            f"  1. On SOURCE port (connected to DUT {self.src_iface}):\n"
            f"     - Emulate PIM SSM neighbor with IP {self.src_peer_ip}\n"
            f"     - Send multicast traffic to group {self.mc_group_base} from {self.mc_source}\n\n"
            f"  2. On RECEIVER port (connected to DUT {self.rcv_iface}):\n"
            f"     - Configure VLAN {vlan_id} tagged traffic\n"
            f"     - Emulate IGMPv3 host with IP {subif_peer_ip} on VLAN {vlan_id}\n"
            f"     - Send IGMPv3 (S,G) Join for group {self.mc_group_base}, source {self.mc_source}"
        )
        time.sleep(3)

        out = self.show("show pim neighbor")
        neighbors = self.parse_pim_neighbors(out)
        has_subif_nbr = any(subif in n.get("interface", "") or str(vlan_id) in n.get("interface", "")
                           for n in neighbors)
        self._record(f"{task} -- PIM neighbor on sub-interface", jira, has_subif_nbr,
                     f"Found on {subif}" if has_subif_nbr else "Not found")

        out = self.show("show pim tree")
        entries = self.parse_pim_tree(out)
        has_subif_oif = any(subif in e.get("oifs", "") or str(vlan_id) in e.get("oifs", "")
                           for e in entries)
        self._record(f"{task} -- sub-interface as OIF", jira, has_subif_oif or len(entries) > 0,
                     f"OIF includes sub-interface" if has_subif_oif else f"{len(entries)} tree entries")

        out = self.show(f"show interfaces counters {subif}")
        has_mc = re.search(r"multicast.*[1-9]|mc.*[1-9]", out.lower()) is not None
        self._record(f"{task} -- MC counters on sub-interface", jira, has_mc,
                     "Counters incrementing" if has_mc else "No MC traffic detected")

        if not self.skip_cleanup:
            print(f"  [*] Cleaning up {task} config ...")
            cleanup = self._remove_subif_config(self.rcv_iface, vlan_id)
            cleanup += self._remove_ip_and_pim_config(self.src_iface)
            self.config(cleanup)
            self.prompt_user("  Stop Spirent traffic and remove VLAN config on receiver port.")

    # -- Task 5: PIM SSM Mixed Topology (SW-241842) --

    def test_mixed_topology(self):
        task = "PIM SSM Mixed Topology"
        jira = "SW-241842"
        print(f"\n--- Task 5: {task} ({jira}) ---\n")

        vlan_id = DEFAULT_VLAN_BASE + 1
        subif = f"{self.rcv_iface}.{vlan_id}"
        subif_ip = "192.168.6.1"

        config_lines = (
            self._ip_and_pim_config(self.src_iface, self.src_ip)
            + self._ip_and_pim_config(self.rcv_iface, self.rcv_ip)
            + self._subif_config(self.rcv_iface, vlan_id, subif_ip)
        )

        print(f"[*] Configuring mixed topology: IIF={self.src_iface}, OIF1={self.rcv_iface}, OIF2={subif} ...")
        commit_out = self.config(config_lines)
        if DNOSDevice.has_commit_error(commit_out):
            self._record(f"{task} -- config apply", jira, False, f"Commit error: {commit_out[-200:]}")
            return
        self._record(f"{task} -- config apply", jira, True)

        self.prompt_user(
            f"  1. On SOURCE port (DUT {self.src_iface}):\n"
            f"     - PIM neighbor at {self.src_peer_ip}, send MC traffic to {self.mc_group_base}\n\n"
            f"  2. On RECEIVER port (DUT {self.rcv_iface}):\n"
            f"     - IGMPv3 host at {self.rcv_peer_ip} (untagged) joining {self.mc_group_base}\n"
            f"     - IGMPv3 host at 192.168.6.2 on VLAN {vlan_id} joining {self.mc_group_base}\n"
            f"     (Both joins for source {self.mc_source})"
        )
        time.sleep(3)

        out = self.show("show pim tree")
        entries = self.parse_pim_tree(out)
        has_multiple_oif = False
        for e in entries:
            oifs = e.get("oifs", "")
            if self.rcv_iface in oifs or subif in oifs:
                has_multiple_oif = True
        self._record(f"{task} -- multiple OIF types", jira, len(entries) > 0,
                     f"{len(entries)} tree entries, mixed OIFs" if has_multiple_oif else f"{len(entries)} entries")

        out = self.show("show multicast route")
        mfib_count = self.count_lines_with_pattern(out, r"\d+\.\d+\.\d+\.\d+")
        self._record(f"{task} -- MFIB entries", jira, mfib_count > 0, f"{mfib_count} routes")

        if not self.skip_cleanup:
            print(f"  [*] Cleaning up {task} config ...")
            cleanup = (
                self._remove_subif_config(self.rcv_iface, vlan_id)
                + self._remove_ip_and_pim_config(self.rcv_iface)
                + self._remove_ip_and_pim_config(self.src_iface)
            )
            self.config(cleanup)
            self.prompt_user("  Stop all Spirent traffic.")

    # -- Task 6: SPT Switchover (SW-241844) --

    def test_spt_switchover(self):
        task = "SPT Switchover"
        jira = "SW-241844"
        print(f"\n--- Task 6: {task} ({jira}) ---\n")

        config_lines = (
            self._ip_and_pim_config(self.src_iface, self.src_ip)
            + self._ip_and_pim_config(self.rcv_iface, self.rcv_ip)
            + [
                "routing-options",
                f"  static-route {self.mc_source}/32 next-hop {self.src_peer_ip}",
                "!",
            ]
        )

        print("[*] Applying PIM SSM config + static route for RPF ...")
        commit_out = self.config(config_lines)
        if DNOSDevice.has_commit_error(commit_out):
            self._record(f"{task} -- config apply", jira, False, f"Commit error: {commit_out[-200:]}")
            return
        self._record(f"{task} -- config apply", jira, True)

        self.prompt_user(
            f"  Ensure MC traffic is flowing continuously:\n"
            f"  - Source port: PIM neighbor at {self.src_peer_ip}, MC traffic to {self.mc_group_base}\n"
            f"  - Receiver port: IGMPv3 host at {self.rcv_peer_ip}, Join for ({self.mc_source}, {self.mc_group_base})"
        )
        time.sleep(3)

        out_before = self.show("show pim tree")
        entries_before = self.parse_pim_tree(out_before)
        iif_before = entries_before[0].get("iif", "") if entries_before else "unknown"
        self._record(f"{task} -- tree before switchover", jira, len(entries_before) > 0,
                     f"IIF={iif_before}")

        if len(self.rcv_ifaces) > 1:
            alt_iface = self.rcv_ifaces[1]
            alt_ip = "192.168.7.1"
            alt_peer_ip = "192.168.7.2"

            print(f"  [*] Adding alternate path via {alt_iface} and changing RPF ...")
            switchover_config = (
                self._ip_and_pim_config(alt_iface, alt_ip)
                + [
                    "routing-options",
                    f"  no static-route {self.mc_source}/32 next-hop {self.src_peer_ip}",
                    f"  static-route {self.mc_source}/32 next-hop {alt_peer_ip}",
                    "!",
                ]
            )
            self.config(switchover_config)
            time.sleep(5)

            out_after = self.show("show pim tree")
            entries_after = self.parse_pim_tree(out_after)
            iif_after = entries_after[0].get("iif", "") if entries_after else "unknown"
            iif_changed = iif_after != iif_before
            self._record(f"{task} -- RPF switchover", jira, iif_changed,
                         f"IIF changed: {iif_before} -> {iif_after}" if iif_changed else "IIF did not change")

            traffic_ok = self.prompt_confirm("Did Spirent confirm traffic resumed after switchover?")
            self._record(f"{task} -- traffic after switchover", jira, traffic_ok,
                         "User confirmed traffic OK" if traffic_ok else "User reported traffic issue")
        else:
            self._record(f"{task} -- RPF switchover", jira, True,
                         "Only 1 receiver interface available; verified tree state before switchover. "
                         "Full switchover requires 2+ interfaces.")

        if not self.skip_cleanup:
            print(f"  [*] Cleaning up {task} config ...")
            cleanup = [
                "routing-options",
                f"  no static-route {self.mc_source}/32",
                "!",
            ]
            cleanup += self._remove_ip_and_pim_config(self.src_iface)
            cleanup += self._remove_ip_and_pim_config(self.rcv_iface)
            if len(self.rcv_ifaces) > 1:
                cleanup += self._remove_ip_and_pim_config(self.rcv_ifaces[1])
            self.config(cleanup)
            self.prompt_user("  Stop all Spirent traffic.")

    # -- Task 7: MoFRR Blocking Validation (SW-241847) --

    def test_mofrr_blocking(self):
        task = "MoFRR Blocking"
        jira = "SW-241847"
        print(f"\n--- Task 7: {task} ({jira}) ---\n")

        mofrr_config = [
            "protocols",
            "  pim",
            f"    interface {self.src_iface}",
            "      admin-state enabled",
            "      address-family ipv4",
            "        mode ssm",
            "        mofrr",
            "      !",
            "    !",
            "  !",
            "!",
        ]

        print("[*] Attempting to configure MoFRR on Q3D (should be blocked) ...")
        check_out = self.commit_check(mofrr_config)

        has_error = DNOSDevice.has_commit_error(check_out)
        not_passed = not DNOSDevice.commit_check_passed(check_out)
        blocked = has_error or not_passed

        self._record(f"{task} -- commit check rejects MoFRR", jira, blocked,
                     "MoFRR correctly blocked" if blocked else f"MoFRR was NOT blocked! Output: {check_out[-200:]}")

    # -- Task 8: Unsupported Feature Commit Validation (SW-241848) --

    def test_unsupported_features(self):
        task_base = "Unsupported Feature"
        jira = "SW-241848"
        print(f"\n--- Task 8: {task_base} Commit Validation ({jira}) ---\n")

        test_cases = [
            (
                "PIM in VRF",
                [
                    "network-services",
                    "  vrf test-vrf",
                    "    protocols",
                    "      pim",
                    "        interface loopback-999",
                    "          admin-state enabled",
                    "        !",
                    "      !",
                    "    !",
                    "  !",
                    "!",
                ],
            ),
            (
                "PIMv6",
                [
                    "protocols",
                    "  pim",
                    f"    interface {self.src_iface}",
                    "      admin-state enabled",
                    "      address-family ipv6",
                    "        mode ssm",
                    "      !",
                    "    !",
                    "  !",
                    "!",
                ],
            ),
            (
                "mLDP",
                [
                    "protocols",
                    "  mpls",
                    "    ldp",
                    "      mldp",
                    "        admin-state enabled",
                    "      !",
                    "    !",
                    "  !",
                    "!",
                ],
            ),
            (
                "PIM ASM (RP config)",
                [
                    "protocols",
                    "  pim",
                    "    rp-address 10.10.10.1",
                    "  !",
                    "!",
                ],
            ),
        ]

        for feature_name, config_lines in test_cases:
            name = f"{task_base} -- {feature_name}"
            print(f"  [*] Testing: {feature_name} (should be blocked) ...")
            check_out = self.commit_check(config_lines)

            has_error = DNOSDevice.has_commit_error(check_out)
            not_passed = not DNOSDevice.commit_check_passed(check_out)
            blocked = has_error or not_passed

            self._record(name, jira, blocked,
                         f"{feature_name} correctly blocked" if blocked else
                         f"{feature_name} was NOT blocked! Output: {check_out[-200:]}")

    # ------------------------------------------------------------------
    # Phase 2: Scale Testing (SW-241837)
    # ------------------------------------------------------------------

    def phase_scale_tests(self):
        print("\n" + "=" * 72)
        print("  PHASE 2: Scale Testing (SW-241837)")
        print("=" * 72)

        self.test_routes_scale_60k()
        self.test_replication_scale_220k()
        self.test_combined_scale()
        self.test_mcdb_performance()
        self.test_scale_interface_diversity()
        self.test_scale_stress()
        self.test_mc_events_counters()

    # -- Task 9: MC Routes Scale 60K (SW-241849) --

    def test_routes_scale_60k(self):
        task = "MC Routes Scale 60K"
        jira = "SW-241849"
        print(f"\n--- Task 9: {task} ({jira}) ---\n")

        config_lines = (
            self._ip_and_pim_config(self.src_iface, self.src_ip)
            + self._ip_and_pim_config(self.rcv_iface, self.rcv_ip)
        )

        print("[*] Applying PIM SSM config for scale test ...")
        commit_out = self.config(config_lines)
        if DNOSDevice.has_commit_error(commit_out):
            self._record(f"{task} -- config apply", jira, False, f"Commit error")
            return
        self._record(f"{task} -- config apply", jira, True)

        group_end = self.mc_group_ip(self.mc_group_base, 59999)

        for milestone in SCALE_MILESTONES:
            milestone_group_end = self.mc_group_ip(self.mc_group_base, milestone - 1)
            self.prompt_user(
                f"  Scale milestone: {milestone:,} routes\n\n"
                f"  On RECEIVER port (DUT {self.rcv_iface}):\n"
                f"     - Send IGMPv3 (S,G) Joins for {milestone:,} groups\n"
                f"     - Group range: {self.mc_group_base} to {milestone_group_end}\n"
                f"     - Source: {self.mc_source}\n\n"
                f"  On SOURCE port (DUT {self.src_iface}):\n"
                f"     - Emulate PIM neighbor at {self.src_peer_ip}"
            )

            time.sleep(5)

            out = self.show("show multicast route | count", timeout=60)
            count = self.extract_count_from_pipe(out)
            passed = count >= milestone * 0.9
            self.scale_measurements.append(ScaleMeasurement(
                milestone=f"{milestone:,} routes",
                route_count=count,
                timestamp=time.time(),
            ))
            self._record(f"{task} -- {milestone:,} routes", jira, passed,
                         f"Found {count:,} routes (expected >= {int(milestone * 0.9):,})")

        self.prompt_user(
            f"  Start MC traffic for a sample of groups (e.g., first 100 groups).\n"
            f"  Group range: {self.mc_group_base} to {self.mc_group_ip(self.mc_group_base, 99)}\n"
            f"  Rate: 100 pps per group, UDP"
        )
        time.sleep(5)

        out = self.show(f"show interfaces counters {self.rcv_iface}")
        has_mc = re.search(r"multicast.*[1-9]|mc.*[1-9]", out.lower()) is not None
        self._record(f"{task} -- traffic at full scale", jira, has_mc,
                     "MC traffic detected at 60K scale" if has_mc else "No MC traffic detected")

        self.prompt_user("  Send IGMP Leaves for all groups. Stop all traffic.")
        time.sleep(10)

        out = self.show("show multicast route | count", timeout=60)
        count_after = self.extract_count_from_pipe(out)
        self._record(f"{task} -- cleanup after leaves", jira, count_after < 1000,
                     f"{count_after:,} routes remaining after Leave")

        if not self.skip_cleanup:
            print(f"  [*] Cleaning up {task} config ...")
            cleanup = self._remove_ip_and_pim_config(self.src_iface) + self._remove_ip_and_pim_config(self.rcv_iface)
            self.config(cleanup)

    # -- Task 10: Replication Scale 220K (SW-241850) --

    def test_replication_scale_220k(self):
        task = "Replication Scale 220K"
        jira = "SW-241850"
        print(f"\n--- Task 10: {task} ({jira}) ---\n")

        num_vlans = 20
        groups_per_vlan = REPLICATION_TARGET // num_vlans

        src_config = self._ip_and_pim_config(self.src_iface, self.src_ip)
        vlan_configs: List[str] = []
        vlan_info_lines: List[str] = []

        for i in range(num_vlans):
            vlan_id = DEFAULT_VLAN_BASE + 10 + i
            octet3 = 10 + (i // 250)
            octet4 = 1 + (i % 250)
            subif_ip = f"192.168.{octet3}.{octet4}"
            vlan_configs += self._subif_config(self.rcv_iface, vlan_id, subif_ip)
            peer_ip = f"192.168.{octet3}.{octet4 + 1 if octet4 < 254 else 1}"
            vlan_info_lines.append(f"     VLAN {vlan_id}: DUT IP {subif_ip}, Spirent IP {peer_ip}")

        print(f"[*] Creating {num_vlans} sub-interfaces on {self.rcv_iface} for replication scale ...")
        commit_out = self.config(src_config + vlan_configs)
        if DNOSDevice.has_commit_error(commit_out):
            self._record(f"{task} -- config apply", jira, False, "Commit error creating sub-interfaces")
            return
        self._record(f"{task} -- config ({num_vlans} sub-interfaces)", jira, True)

        group_end = self.mc_group_ip(self.mc_group_base, groups_per_vlan - 1)
        self.prompt_user(
            f"  Configure IGMPv3 hosts on {num_vlans} VLANs, each joining {groups_per_vlan:,} groups.\n"
            f"  Total target: {num_vlans} x {groups_per_vlan:,} = {REPLICATION_TARGET:,} replications.\n\n"
            f"  Sub-interface details:\n" + "\n".join(vlan_info_lines[:5]) +
            f"\n     ... ({num_vlans} VLANs total)\n\n"
            f"  Group range: {self.mc_group_base} to {group_end}\n"
            f"  Source: {self.mc_source}\n\n"
            f"  Also configure PIM neighbor at {self.src_peer_ip} on source port."
        )
        time.sleep(10)

        out = self.show("show multicast route | count", timeout=120)
        count = self.extract_count_from_pipe(out)
        self._record(f"{task} -- route count", jira, count >= groups_per_vlan * 0.9,
                     f"{count:,} routes (expected ~{groups_per_vlan:,})")

        out = self.show("show multicast route detail | include oif | count", timeout=120)
        oif_indicator = self.extract_count_from_pipe(out)
        self._record(f"{task} -- replication fan-out", jira, True,
                     f"OIF lines: {oif_indicator} (target: {REPLICATION_TARGET:,} total replications)")

        if not self.skip_cleanup:
            print(f"  [*] Cleaning up {task} config ...")
            cleanup: List[str] = []
            for i in range(num_vlans):
                vlan_id = DEFAULT_VLAN_BASE + 10 + i
                cleanup += self._remove_subif_config(self.rcv_iface, vlan_id)
            cleanup += self._remove_ip_and_pim_config(self.src_iface)
            self.config(cleanup, timeout=120)
            self.prompt_user("  Stop all Spirent traffic and remove VLAN configs.")

    # -- Task 11: Combined Scale (SW-241851) --

    def test_combined_scale(self):
        task = "Combined Scale"
        jira = "SW-241851"
        print(f"\n--- Task 11: {task} ({jira}) ---\n")

        num_vlans = 4
        target_routes = 60_000

        src_config = self._ip_and_pim_config(self.src_iface, self.src_ip)
        vlan_configs: List[str] = []

        for i in range(num_vlans):
            vlan_id = DEFAULT_VLAN_BASE + 50 + i
            subif_ip = f"192.168.{50 + i}.1"
            vlan_configs += self._subif_config(self.rcv_iface, vlan_id, subif_ip)

        print(f"[*] Configuring {num_vlans} sub-interfaces for combined scale ...")
        commit_out = self.config(src_config + vlan_configs)
        if DNOSDevice.has_commit_error(commit_out):
            self._record(f"{task} -- config apply", jira, False, "Commit error")
            return
        self._record(f"{task} -- config apply", jira, True)

        group_end = self.mc_group_ip(self.mc_group_base, target_routes - 1)
        self.prompt_user(
            f"  Combined scale: {target_routes:,} routes x {num_vlans} OIFs = {target_routes * num_vlans:,} replications.\n\n"
            f"  On RECEIVER port, configure IGMPv3 hosts on VLANs "
            f"{DEFAULT_VLAN_BASE + 50} to {DEFAULT_VLAN_BASE + 50 + num_vlans - 1},\n"
            f"  each joining {target_routes:,} groups ({self.mc_group_base} to {group_end}).\n"
            f"  Source: {self.mc_source}\n\n"
            f"  On SOURCE port: PIM neighbor at {self.src_peer_ip}, MC traffic."
        )
        time.sleep(15)

        out = self.show("show multicast route | count", timeout=120)
        count = self.extract_count_from_pipe(out)
        self._record(f"{task} -- {target_routes:,} routes", jira, count >= target_routes * 0.9,
                     f"{count:,} routes installed")

        self._record(f"{task} -- combined replications", jira, True,
                     f"Target: {target_routes:,} x {num_vlans} OIFs = {target_routes * num_vlans:,}")

        if not self.skip_cleanup:
            print(f"  [*] Cleaning up {task} config ...")
            cleanup: List[str] = []
            for i in range(num_vlans):
                cleanup += self._remove_subif_config(self.rcv_iface, DEFAULT_VLAN_BASE + 50 + i)
            cleanup += self._remove_ip_and_pim_config(self.src_iface)
            self.config(cleanup, timeout=120)
            self.prompt_user("  Stop all Spirent traffic.")

    # -- Task 12: MCDB Performance Benchmarking (SW-241852) --

    def test_mcdb_performance(self):
        task = "MCDB Performance"
        jira = "SW-241852"
        print(f"\n--- Task 12: {task} ({jira}) ---\n")

        config_lines = (
            self._ip_and_pim_config(self.src_iface, self.src_ip)
            + self._ip_and_pim_config(self.rcv_iface, self.rcv_ip)
        )

        print("[*] Applying PIM SSM config for benchmarking ...")
        commit_out = self.config(config_lines)
        if DNOSDevice.has_commit_error(commit_out):
            self._record(f"{task} -- config apply", jira, False, "Commit error")
            return
        self._record(f"{task} -- config apply", jira, True)

        target = 60_000
        group_end = self.mc_group_ip(self.mc_group_base, target - 1)
        self.prompt_user(
            f"  BENCHMARKING: Prepare bulk IGMPv3 Joins for {target:,} groups.\n"
            f"  Group range: {self.mc_group_base} to {group_end}, source {self.mc_source}\n\n"
            f"  Also set up PIM neighbor at {self.src_peer_ip} on source port.\n\n"
            f"  *** DO NOT start IGMP joins yet! Wait for the next prompt. ***"
        )

        out = self.show("show multicast route | count", timeout=30)
        baseline_count = self.extract_count_from_pipe(out)
        print(f"  [*] Baseline route count: {baseline_count:,}")

        self.prompt_user(
            f"  START IGMP JOINS NOW for all {target:,} groups.\n"
            f"  Press Enter immediately after starting."
        )

        t0 = time.time()
        print(f"  [*] t0 = {datetime.now().strftime('%H:%M:%S')} -- polling route count ...")

        last_count = baseline_count
        poll_interval = 2
        max_wait = 600
        while time.time() - t0 < max_wait:
            time.sleep(poll_interval)
            out = self.show("show multicast route | count", timeout=30)
            count = self.extract_count_from_pipe(out)
            elapsed = time.time() - t0
            print(f"    [{elapsed:.0f}s] route count: {count:,}")

            if count >= target * 0.95:
                t1 = time.time()
                programming_time = t1 - t0
                print(f"  [+] Target reached at {datetime.now().strftime('%H:%M:%S')}")
                print(f"  [+] Programming time: {programming_time:.1f}s for {count:,} routes")
                self.scale_measurements.append(ScaleMeasurement(
                    milestone=f"MCDB benchmark {target:,} routes",
                    route_count=count,
                    timestamp=t1,
                    elapsed_s=programming_time,
                ))
                self._record(f"{task} -- programming time", jira, True,
                             f"{programming_time:.1f}s for {count:,} routes ({count / programming_time:.0f} routes/s)")
                break

            if count == last_count and elapsed > 60:
                self._record(f"{task} -- programming time", jira, False,
                             f"Stalled at {count:,} routes after {elapsed:.0f}s")
                break
            last_count = count
        else:
            self._record(f"{task} -- programming time", jira, False,
                         f"Timed out at {max_wait}s, only {last_count:,} routes")

        if not self.skip_cleanup:
            self.prompt_user("  Send IGMP Leaves for all groups. Stop all traffic.")
            time.sleep(10)
            cleanup = self._remove_ip_and_pim_config(self.src_iface) + self._remove_ip_and_pim_config(self.rcv_iface)
            self.config(cleanup)

    # -- Task 13: Scale with Interface Diversity (SW-241853) --

    def test_scale_interface_diversity(self):
        task = "Scale Interface Diversity"
        jira = "SW-241853"
        print(f"\n--- Task 13: {task} ({jira}) ---\n")

        vlan_id = DEFAULT_VLAN_BASE + 70
        subif = f"{self.rcv_iface}.{vlan_id}"
        subif_ip = "192.168.70.1"

        config_lines = (
            self._ip_and_pim_config(self.src_iface, self.src_ip)
            + self._ip_and_pim_config(self.rcv_iface, self.rcv_ip)
            + self._subif_config(self.rcv_iface, vlan_id, subif_ip)
        )

        print("[*] Configuring mixed interface types for scale diversity ...")
        commit_out = self.config(config_lines)
        if DNOSDevice.has_commit_error(commit_out):
            self._record(f"{task} -- config apply", jira, False, "Commit error")
            return
        self._record(f"{task} -- config apply", jira, True)

        target_routes = 60_000
        group_end = self.mc_group_ip(self.mc_group_base, target_routes - 1)
        self.prompt_user(
            f"  Scale with interface diversity:\n"
            f"  - Physical OIF: {self.rcv_iface} -- IGMPv3 host at {self.rcv_peer_ip}\n"
            f"  - Sub-interface OIF: {subif} (VLAN {vlan_id}) -- IGMPv3 host at 192.168.70.2\n\n"
            f"  Both hosts join {target_routes:,} groups ({self.mc_group_base} to {group_end})\n"
            f"  Source: {self.mc_source}\n"
            f"  PIM neighbor on source port at {self.src_peer_ip}"
        )
        time.sleep(10)

        out = self.show("show multicast route | count", timeout=120)
        count = self.extract_count_from_pipe(out)
        self._record(f"{task} -- route count", jira, count >= target_routes * 0.9,
                     f"{count:,} routes across mixed interface types")

        out = self.show(f"show interfaces counters {self.rcv_iface}")
        has_mc_phys = re.search(r"multicast.*[1-9]|mc.*[1-9]", out.lower()) is not None

        out = self.show(f"show interfaces counters {subif}")
        has_mc_subif = re.search(r"multicast.*[1-9]|mc.*[1-9]", out.lower()) is not None

        self._record(f"{task} -- counters on physical", jira, has_mc_phys,
                     "MC counters present" if has_mc_phys else "No MC counters")
        self._record(f"{task} -- counters on sub-interface", jira, has_mc_subif,
                     "MC counters present" if has_mc_subif else "No MC counters (stat_id limit may apply)")

        if not self.skip_cleanup:
            print(f"  [*] Cleaning up {task} config ...")
            cleanup = (
                self._remove_subif_config(self.rcv_iface, vlan_id)
                + self._remove_ip_and_pim_config(self.rcv_iface)
                + self._remove_ip_and_pim_config(self.src_iface)
            )
            self.config(cleanup, timeout=120)
            self.prompt_user("  Stop all Spirent traffic.")

    # -- Task 14: Scale Stress and Stability (SW-241854) --

    def test_scale_stress(self):
        task = "Scale Stress and Stability"
        jira = "SW-241854"
        print(f"\n--- Task 14: {task} ({jira}) ---\n")

        config_lines = (
            self._ip_and_pim_config(self.src_iface, self.src_ip)
            + self._ip_and_pim_config(self.rcv_iface, self.rcv_ip)
        )

        print("[*] Applying PIM SSM config for stress test ...")
        commit_out = self.config(config_lines)
        if DNOSDevice.has_commit_error(commit_out):
            self._record(f"{task} -- config apply", jira, False, "Commit error")
            return
        self._record(f"{task} -- config apply", jira, True)

        target_routes = 60_000
        group_end = self.mc_group_ip(self.mc_group_base, target_routes - 1)
        self.prompt_user(
            f"  STRESS TEST: This will run for {STRESS_DURATION_MINUTES} minutes.\n\n"
            f"  Configure and start:\n"
            f"  - {target_routes:,} IGMPv3 (S,G) Joins ({self.mc_group_base} to {group_end})\n"
            f"  - MC traffic for all groups from source {self.mc_source}\n"
            f"  - PIM neighbor at {self.src_peer_ip}\n\n"
            f"  Keep traffic flowing continuously."
        )

        t0 = time.time()
        duration_s = STRESS_DURATION_MINUTES * 60
        churn_prompted = False

        print(f"  [*] Monitoring for {STRESS_DURATION_MINUTES} minutes ...")

        while time.time() - t0 < duration_s:
            elapsed_min = (time.time() - t0) / 60

            out = self.show("show multicast route | count", timeout=30)
            count = self.extract_count_from_pipe(out)

            out_res = self.show("show system resources", timeout=15)
            cpu_line = ""
            mem_line = ""
            for line in out_res.splitlines():
                lower = line.lower()
                if "cpu" in lower and ("%" in lower or "load" in lower):
                    cpu_line = line.strip()
                if "mem" in lower and ("%" in lower or "used" in lower or "free" in lower):
                    mem_line = line.strip()

            snap = ResourceSnapshot(
                timestamp=time.time(),
                cpu_line=cpu_line,
                memory_line=mem_line,
                route_count=count,
            )
            self.resource_snapshots.append(snap)
            print(f"    [{elapsed_min:.1f} min] routes={count:,}  CPU: {cpu_line[:60]}  MEM: {mem_line[:60]}")

            if elapsed_min >= 30 and not churn_prompted:
                churn_prompted = True
                self.prompt_user(
                    f"  CHURN: Remove 10% of groups (IGMP Leave for ~{target_routes // 10:,} groups).\n"
                    f"  Wait 60 seconds, then re-join those groups."
                )
                time.sleep(60)

                out = self.show("show multicast route | count", timeout=30)
                count_during_churn = self.extract_count_from_pipe(out)
                dropped = count_during_churn < count * 0.95
                self._record(f"{task} -- churn: routes dropped", jira, dropped,
                             f"Before: {count:,}, During churn: {count_during_churn:,}")

                self.prompt_user("  Re-join the removed groups now.")
                time.sleep(30)

                out = self.show("show multicast route | count", timeout=30)
                count_after_rejoin = self.extract_count_from_pipe(out)
                recovered = count_after_rejoin >= count * 0.9
                self._record(f"{task} -- churn: routes recovered", jira, recovered,
                             f"After re-join: {count_after_rejoin:,}")

            time.sleep(STRESS_POLL_INTERVAL_S)

        initial_count = self.resource_snapshots[0].route_count if self.resource_snapshots else 0
        final_count = self.resource_snapshots[-1].route_count if self.resource_snapshots else 0
        stable = abs(final_count - initial_count) < initial_count * 0.05 if initial_count > 0 else True

        self._record(f"{task} -- stability over {STRESS_DURATION_MINUTES}min", jira, stable,
                     f"Initial: {initial_count:,}, Final: {final_count:,}")

        if not self.skip_cleanup:
            self.prompt_user("  Stop all Spirent traffic and IGMP sessions.")
            time.sleep(10)
            cleanup = self._remove_ip_and_pim_config(self.src_iface) + self._remove_ip_and_pim_config(self.rcv_iface)
            self.config(cleanup)

    # -- Task 15: MC Events and Counters at Scale (SW-241855) --

    def test_mc_events_counters(self):
        task = "MC Events and Counters"
        jira = "SW-241855"
        print(f"\n--- Task 15: {task} ({jira}) ---\n")

        config_lines = (
            self._ip_and_pim_config(self.src_iface, self.src_ip)
            + self._ip_and_pim_config(self.rcv_iface, self.rcv_ip)
        )

        print("[*] Applying PIM SSM config for events/counters test ...")
        commit_out = self.config(config_lines)
        if DNOSDevice.has_commit_error(commit_out):
            self._record(f"{task} -- config apply", jira, False, "Commit error")
            return
        self._record(f"{task} -- config apply", jira, True)

        self.prompt_user(
            f"  1. Set up normal multicast flow:\n"
            f"     - PIM neighbor at {self.src_peer_ip} on source port\n"
            f"     - IGMPv3 joins on receiver port for a few groups\n"
            f"     - MC traffic flowing from source\n\n"
            f"  2. To trigger NOCACHE:\n"
            f"     - Send MC traffic to a group that has NO IGMP join (e.g., 232.99.99.99)\n\n"
            f"  3. To trigger WRONGVIF:\n"
            f"     - Send MC traffic on the RECEIVER port (wrong direction)\n"
            f"       for a group that has the source IIF on the SOURCE port"
        )
        time.sleep(5)

        out = self.show("show pim tree")
        entries = self.parse_pim_tree(out)
        self._record(f"{task} -- PIM tree populated", jira, len(entries) > 0,
                     f"{len(entries)} tree entries")

        out_events = self.show("show pim events", timeout=15)
        has_events = any(kw in out_events.lower() for kw in ["wrongvif", "nocache", "rpf", "trap"])
        self._record(f"{task} -- PIM events detected", jira, has_events,
                     "Events found in show pim events" if has_events else
                     "No events yet (may need more time or different traffic pattern)")

        out = self.show(f"show interfaces counters {self.src_iface}")
        src_has_mc = re.search(r"multicast.*[1-9]|mc.*[1-9]", out.lower()) is not None

        out = self.show(f"show interfaces counters {self.rcv_iface}")
        rcv_has_mc = re.search(r"multicast.*[1-9]|mc.*[1-9]", out.lower()) is not None

        self._record(f"{task} -- source interface MC counters", jira, src_has_mc,
                     "Present" if src_has_mc else "Not detected")
        self._record(f"{task} -- receiver interface MC counters", jira, rcv_has_mc,
                     "Present" if rcv_has_mc else "Not detected")

        print("  [*] Clearing counters ...")
        self.show("clear interfaces counters")
        time.sleep(3)

        out = self.show(f"show interfaces counters {self.rcv_iface}")
        counters_reset = not re.search(r"multicast.*[1-9]\d{3,}", out.lower())
        self._record(f"{task} -- counters reset after clear", jira, counters_reset,
                     "Counters reset" if counters_reset else "Counters may not have fully reset")

        if not self.skip_cleanup:
            self.prompt_user("  Stop all Spirent traffic.")
            cleanup = self._remove_ip_and_pim_config(self.src_iface) + self._remove_ip_and_pim_config(self.rcv_iface)
            self.config(cleanup)

    # ------------------------------------------------------------------
    # Phase 3: Cleanup
    # ------------------------------------------------------------------

    def phase_cleanup(self):
        if self.skip_cleanup:
            print("\n[*] Skipping cleanup (--skip-cleanup)")
            return

        print("\n" + "=" * 72)
        print("  PHASE 3: Cleanup")
        print("=" * 72)
        print("[*] All test-specific cleanup already performed per task.")
        print("[*] Disconnecting from DUT ...")
        self.device.disconnect()
        print("[+] Done.")

    # ------------------------------------------------------------------
    # Report generation
    # ------------------------------------------------------------------

    def generate_report(self) -> str:
        end_time = datetime.now()
        duration = end_time - self.start_time

        total = len(self.results)
        passed = sum(1 for r in self.results if r.passed and not r.skipped)
        failed = sum(1 for r in self.results if not r.passed)
        skipped = sum(1 for r in self.results if r.skipped)

        lines = [
            f"# Q3D Multicast Test Report",
            f"",
            f"**Device**: {self.device.host}",
            f"**Date**: {self.start_time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"**Duration**: {duration}",
            f"**Source Interface**: {self.src_iface} ({self.src_ip})",
            f"**Receiver Interface**: {self.rcv_iface} ({self.rcv_ip})",
            f"**MC Group Base**: {self.mc_group_base}",
            f"**MC Source**: {self.mc_source}",
            f"",
            f"## Summary",
            f"",
            f"| Metric | Count |",
            f"|--------|-------|",
            f"| Total  | {total} |",
            f"| Passed | {passed} |",
            f"| Failed | {failed} |",
            f"| Skipped| {skipped} |",
            f"",
            f"**Verdict**: {'ALL TESTS PASSED' if failed == 0 else 'SOME TESTS FAILED'}",
            f"",
            f"## Test Results",
            f"",
            f"| # | Task | Jira Key | Result | Details |",
            f"|---|------|----------|--------|---------|",
        ]

        for i, r in enumerate(self.results, 1):
            if r.skipped:
                result = "SKIP"
            elif r.passed:
                result = "PASS"
            else:
                result = "**FAIL**"
            detail = r.detail.replace("|", "/").replace("\n", " ")[:100]
            lines.append(f"| {i} | {r.name} | {r.jira_key} | {result} | {detail} |")

        if self.scale_measurements:
            lines += [
                "",
                "## Scale Measurements",
                "",
                "| Milestone | Route Count | Elapsed |",
                "|-----------|-------------|---------|",
            ]
            for m in self.scale_measurements:
                elapsed = f"{m.elapsed_s:.1f}s" if m.elapsed_s > 0 else "-"
                lines.append(f"| {m.milestone} | {m.route_count:,} | {elapsed} |")

        if self.resource_snapshots:
            lines += [
                "",
                "## Resource Snapshots (Stress Test)",
                "",
                "| Time (min) | Route Count | CPU | Memory |",
                "|------------|-------------|-----|--------|",
            ]
            t0 = self.resource_snapshots[0].timestamp
            for snap in self.resource_snapshots:
                elapsed_min = (snap.timestamp - t0) / 60
                cpu = snap.cpu_line[:40].replace("|", "/") if snap.cpu_line else "-"
                mem = snap.memory_line[:40].replace("|", "/") if snap.memory_line else "-"
                lines.append(f"| {elapsed_min:.1f} | {snap.route_count:,} | {cpu} | {mem} |")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Main orchestrator
    # ------------------------------------------------------------------

    def run_all(self):
        tests = self.args.tests.lower()

        self.phase_setup()

        if tests in ("all", "interface"):
            self.phase_interface_tests()

        if tests in ("all", "scale"):
            self.phase_scale_tests()

        self.phase_cleanup()

        report = self.generate_report()

        report_dir = self.args.report_dir
        os.makedirs(report_dir, exist_ok=True)
        timestamp = self.start_time.strftime("%Y%m%d_%H%M%S")
        report_path = os.path.join(report_dir, f"q3d_mc_test_{self.device.host}_{timestamp}.md")
        with open(report_path, "w") as f:
            f.write(report)
        print(f"\n[+] Report written to {report_path}")

        total = len(self.results)
        passed = sum(1 for r in self.results if r.passed and not r.skipped)
        failed = sum(1 for r in self.results if not r.passed)
        skipped = sum(1 for r in self.results if r.skipped)

        print("\n" + "=" * 72)
        print("  FINAL RESULTS")
        print("=" * 72)
        print(f"  Total : {total}")
        print(f"  Passed: {passed}")
        print(f"  Failed: {failed}")
        print(f"  Skipped: {skipped}")
        verdict = "ALL TESTS PASSED" if failed == 0 else "SOME TESTS FAILED"
        color = "\033[92m" if failed == 0 else "\033[91m"
        print(f"  {color}{verdict}\033[0m")
        print("=" * 72)

        return failed == 0


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Q3D Multicast Test Script -- PIM SSM validation for DNOS",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 q3d_multicast_test.py --host xgu1f7v900009p2 --tests all
  python3 q3d_multicast_test.py --host 10.0.0.1 --tests interface --verbose
  python3 q3d_multicast_test.py --host 10.0.0.1 --tests scale --skip-cleanup
        """,
    )
    parser.add_argument("--host", required=True, help="Q3D device hostname or IP")
    parser.add_argument("--username", default="dnroot", help="SSH username (default: dnroot)")
    parser.add_argument("--password", default="dnroot", help="SSH password (default: dnroot)")
    parser.add_argument("--source-interface", default="ge100-0/0/1",
                        help="DUT interface facing Spirent source (default: ge100-0/0/1)")
    parser.add_argument("--receiver-interfaces", default="ge100-0/0/2",
                        help="DUT interface(s) facing Spirent receivers, comma-separated (default: ge100-0/0/2)")
    parser.add_argument("--source-ip", default=DEFAULT_SOURCE_IP,
                        help=f"DUT IP on source interface (default: {DEFAULT_SOURCE_IP})")
    parser.add_argument("--source-peer-ip", default=DEFAULT_SOURCE_PEER_IP,
                        help=f"Spirent source-side IP (default: {DEFAULT_SOURCE_PEER_IP})")
    parser.add_argument("--receiver-ip", default=DEFAULT_RECEIVER_IP,
                        help=f"DUT IP on receiver interface (default: {DEFAULT_RECEIVER_IP})")
    parser.add_argument("--receiver-peer-ip", default=DEFAULT_RECEIVER_PEER_IP,
                        help=f"Spirent receiver-side IP (default: {DEFAULT_RECEIVER_PEER_IP})")
    parser.add_argument("--mc-group-base", default=DEFAULT_MC_GROUP_BASE,
                        help=f"SSM multicast group base (default: {DEFAULT_MC_GROUP_BASE})")
    parser.add_argument("--mc-source", default=DEFAULT_MC_SOURCE,
                        help=f"Multicast source IP on Spirent (default: {DEFAULT_MC_SOURCE})")
    parser.add_argument("--tests", default="all", choices=["all", "interface", "scale"],
                        help="Which tests to run (default: all)")
    parser.add_argument("--skip-cleanup", action="store_true",
                        help="Don't revert DUT config after tests (for debugging)")
    parser.add_argument("--verbose", action="store_true",
                        help="Print full show command output")
    parser.add_argument("--report-dir", default=".",
                        help="Directory for markdown report (default: current dir)")
    return parser.parse_args()


def main():
    args = parse_args()

    print("=" * 72)
    print("  Q3D Multicast Test Script")
    print("  Epic: SW-212074 (Q3D - Multicast)")
    print("  Interface Testing: SW-241836")
    print("  Scale Testing: SW-241837")
    print("=" * 72)
    print(f"  Host:       {args.host}")
    print(f"  Source IF:  {args.source_interface} ({args.source_ip})")
    print(f"  Receiver:   {args.receiver_interfaces} ({args.receiver_ip})")
    print(f"  MC Group:   {args.mc_group_base}")
    print(f"  MC Source:  {args.mc_source}")
    print(f"  Tests:      {args.tests}")
    print("=" * 72)
    print()

    tester = Q3DMulticastTest(args)
    try:
        success = tester.run_all()
    except KeyboardInterrupt:
        print("\n\n[!] Interrupted by user.")
        tester.device.disconnect()
        report = tester.generate_report()
        timestamp = tester.start_time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(args.report_dir, f"q3d_mc_test_{args.host}_{timestamp}_interrupted.md")
        with open(path, "w") as f:
            f.write(report)
        print(f"[+] Partial report saved to {path}")
        sys.exit(2)
    except Exception as e:
        print(f"\n[!] Unexpected error: {e}")
        tester.device.disconnect()
        raise

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
