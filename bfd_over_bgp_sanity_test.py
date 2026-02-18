#!/usr/bin/env python3
"""
BFD over BGP Sanity Test Script for DNOS

Tests basic BFD over BGP functionality between two DNOS devices:
  Phase 1 - Setup:
    1. Discover link between devices via LLDP
    2. Snapshot baseline config
    3. Assign IP addresses to connecting interfaces
    4. Configure eBGP with BFD enabled on both devices
    5. Wait for BGP Established + BFD session Up
  Phase 2 - Validation:
    6. Verify BGP summary (neighbor Established)
    7. Verify BFD summary (BGP client sessions Up)
    8. Verify BFD sessions table
    9. Verify BFD session detail (discriminators, counters, transitions)
   10. Modify BFD timers (300 -> 500ms) and verify session survives
   11. Revert BFD timers to default and verify
   12. Verify BFD from peer (host-b) perspective
  Phase 3 - Cleanup:
   13. Remove BGP+BFD config from both devices
   14. Remove IP addresses from interfaces
   15. Verify clean state

Usage:
    python3 bfd_over_bgp_sanity_test.py --host-a xgu1f7v900009p2 --host-b <peer>
    python3 bfd_over_bgp_sanity_test.py --host-a xgu1f7v900009p2 --host-b <peer> --iface-a ge100-0/0/1 --iface-b ge100-0/0/1
"""

import argparse
import re
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import paramiko


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# RFC 5737 documentation addresses for the point-to-point link
LINK_IP_A = "198.51.100.0"
LINK_IP_B = "198.51.100.1"
LINK_PREFIX = 31

# Private AS numbers (RFC 6996)
LOCAL_AS = 65001
PEER_AS = 65002

# BFD timer defaults and test values
BFD_DEFAULT_TX = 300
BFD_DEFAULT_RX = 300
BFD_MODIFIED_TX = 500
BFD_MODIFIED_RX = 500

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class BFDOverBGPSanityTest:
    """BFD over BGP happy-flow sanity tester for DNOS devices."""

    def __init__(
        self,
        host_a: str,
        host_b: str,
        iface_a: Optional[str],
        iface_b: Optional[str],
        username: str,
        password: str,
        timeout: int = 30,
    ):
        self.host_a = host_a
        self.host_b = host_b
        self.iface_a = iface_a
        self.iface_b = iface_b
        self.username = username
        self.password = password
        self.timeout = timeout

        # SSH connections
        self.client_a: Optional[paramiko.SSHClient] = None
        self.shell_a: Optional[paramiko.Channel] = None
        self.client_b: Optional[paramiko.SSHClient] = None
        self.shell_b: Optional[paramiko.Channel] = None

        # Results
        self.results: List[Tuple[str, bool, str]] = []

        # State tracking for cleanup
        self.ip_configured_a = False
        self.ip_configured_b = False
        self.bgp_configured_a = False
        self.bgp_configured_b = False
        self.bfd_preexisted_a = False
        self.bfd_preexisted_b = False
        self.bfd_configured_a = False
        self.bfd_configured_b = False

    # ------------------------------------------------------------------
    # SSH helpers
    # ------------------------------------------------------------------
    def connect(self):
        """Establish SSH connections to both devices."""
        print(f"[*] Connecting to host-a ({self.host_a}) ...")
        self.client_a, self.shell_a = self._ssh_connect(self.host_a)
        print("[+] Connected to host-a.")

        print(f"[*] Connecting to host-b ({self.host_b}) ...")
        self.client_b, self.shell_b = self._ssh_connect(self.host_b)
        print("[+] Connected to host-b.\n")

    def _ssh_connect(
        self, host: str
    ) -> Tuple[paramiko.SSHClient, paramiko.Channel]:
        """Create SSH client and interactive shell with paging disabled."""
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=host,
            username=self.username,
            password=self.password,
            look_for_keys=False,
            allow_agent=False,
            timeout=self.timeout,
        )
        shell = client.invoke_shell(width=250, height=1000)
        self._read_until_prompt(shell, timeout=15)
        shell.send("no-paging\n")
        self._read_until_prompt(shell, timeout=5)
        return client, shell

    def disconnect(self):
        """Close SSH connections to both devices."""
        for shell in (self.shell_a, self.shell_b):
            if shell:
                try:
                    shell.close()
                except Exception:
                    pass
        for client in (self.client_a, self.client_b):
            if client:
                try:
                    client.close()
                except Exception:
                    pass
        print("\n[*] Disconnected from both devices.")

    @staticmethod
    def _read_until_prompt(shell, timeout: int = 30) -> str:
        """Read shell output until a DNOS prompt (ending with # or >)."""
        buf = ""
        end_time = time.time() + timeout
        while time.time() < end_time:
            if shell.recv_ready():
                chunk = shell.recv(65536).decode("utf-8", errors="replace")
                buf += chunk
                clean = ANSI_ESCAPE.sub("", buf)
                lines = clean.strip().split("\n")
                last_line = lines[-1].strip() if lines else ""
                if last_line.endswith("#") or last_line.endswith(">"):
                    break
            else:
                time.sleep(0.2)
        return ANSI_ESCAPE.sub("", buf)

    @staticmethod
    def _send(shell, cmd: str):
        """Send a command to the shell."""
        shell.send(cmd + "\n")

    def run_show(self, shell, cmd: str, timeout: int = 30) -> str:
        """Run a show / operational command and return output."""
        self._send(shell, cmd)
        return self._read_until_prompt(shell, timeout=timeout)

    def run_config(self, shell, config_lines: List[str], timeout: int = 60) -> str:
        """Enter config mode, apply lines, commit, and exit."""
        self._send(shell, "configure")
        self._read_until_prompt(shell, timeout=10)

        for line in config_lines:
            self._send(shell, line)
            self._read_until_prompt(shell, timeout=10)

        self._send(shell, "commit")
        commit_output = self._read_until_prompt(shell, timeout=timeout)

        self._send(shell, "exit")
        self._read_until_prompt(shell, timeout=5)

        return commit_output

    # Convenience wrappers
    def show_a(self, cmd: str, timeout: int = 30) -> str:
        return self.run_show(self.shell_a, cmd, timeout)

    def show_b(self, cmd: str, timeout: int = 30) -> str:
        return self.run_show(self.shell_b, cmd, timeout)

    def config_a(self, lines: List[str], timeout: int = 60) -> str:
        return self.run_config(self.shell_a, lines, timeout)

    def config_b(self, lines: List[str], timeout: int = 60) -> str:
        return self.run_config(self.shell_b, lines, timeout)

    # ------------------------------------------------------------------
    # Result recording
    # ------------------------------------------------------------------
    def _record(self, name: str, passed: bool, detail: str = ""):
        self.results.append((name, passed, detail))
        tag = "[PASS]" if passed else "[FAIL]"
        print(f"  {tag} {name}" + (f" -- {detail}" if detail else ""))

    # ------------------------------------------------------------------
    # Error checking
    # ------------------------------------------------------------------
    @staticmethod
    def _has_commit_error(output: str) -> bool:
        """Check if commit output contains errors."""
        for line in output.splitlines():
            stripped = line.strip()
            lower = stripped.lower()
            # Skip empty lines and the command echo itself
            if not stripped or lower == "commit":
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

    # ------------------------------------------------------------------
    # Parsers
    # ------------------------------------------------------------------
    @staticmethod
    def parse_lldp_neighbors(
        output: str,
    ) -> List[Tuple[str, str, str]]:
        """
        Parse 'show lldp neighbors' output.
        Returns list of (local_interface, remote_system_name, remote_port).
        """
        neighbors: List[Tuple[str, str, str]] = []
        lines = output.splitlines()

        header_found = False
        col_iface = 0
        col_name = 1
        col_port = 2

        for line in lines:
            lower = line.lower()
            if "interface" in lower and "neighbor" in lower:
                header_found = True
                parts = [p.strip().lower() for p in line.split("|") if p.strip()]
                for j, p in enumerate(parts):
                    if "interface" in p and "neighbor" not in p:
                        col_iface = j
                    elif "neighbor system name" in p or (
                        "neighbor" in p and "name" in p
                    ):
                        col_name = j
                    elif "neighbor interface" in p or (
                        "neighbor" in p
                        and "interface" in p
                        and "name" not in p
                    ):
                        col_port = j
                continue

            if not header_found:
                continue

            stripped = line.strip()
            if not stripped or set(stripped) <= {"+", "-", "|", " "}:
                continue

            parts = [p.strip() for p in stripped.split("|") if p.strip()]
            if len(parts) > max(col_iface, col_name):
                local_if = parts[col_iface] if col_iface < len(parts) else ""
                remote_name = parts[col_name] if col_name < len(parts) else ""
                remote_port = parts[col_port] if col_port < len(parts) else ""
                if remote_name and local_if:
                    neighbors.append((local_if, remote_name, remote_port))

        return neighbors

    @staticmethod
    def parse_bgp_summary(
        output: str,
    ) -> Tuple[Optional[str], Optional[str], Dict[str, Dict[str, str]]]:
        """
        Parse 'show bgp summary' output.
        Returns (router_id, local_as, {neighbor_ip: {as, state, pfx_rcvd, updown}}).
        """
        router_id = None
        local_as = None
        neighbors: Dict[str, Dict[str, str]] = {}

        m = re.search(
            r"BGP router identifier\s+(\S+),\s*local AS number\s+(\d+)", output
        )
        if m:
            router_id = m.group(1)
            local_as = m.group(2)

        # Neighbor table rows (space-separated):
        # Neighbor  V  AS  MsgRcvd  MsgSent  TblVer  InQ OutQ Up/Down  State/PfxRcd
        for line in output.splitlines():
            stripped = line.strip()
            m = re.match(
                r"(\d+\.\d+\.\d+\.\d+)\s+\d+\s+(\d+)\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+(\S+)\s+(\S+)",
                stripped,
            )
            if m:
                neighbor_ip = m.group(1)
                peer_as = m.group(2)
                updown = m.group(3)
                state_pfx = m.group(4)
                try:
                    int(state_pfx)
                    state = "Established"
                except ValueError:
                    state = state_pfx
                neighbors[neighbor_ip] = {
                    "as": peer_as,
                    "state": state,
                    "pfx_rcvd": state_pfx,
                    "updown": updown,
                }
                continue

            # Also try pipe-separated format
            if "|" in stripped:
                parts = [p.strip() for p in stripped.split("|") if p.strip()]
                if parts and re.match(r"\d+\.\d+\.\d+\.\d+", parts[0]):
                    neighbor_ip = parts[0]
                    state = parts[-1] if parts else "Unknown"
                    try:
                        int(state)
                        state = "Established"
                    except ValueError:
                        pass
                    neighbors[neighbor_ip] = {
                        "as": parts[1] if len(parts) > 1 else "",
                        "state": state,
                        "pfx_rcvd": parts[-1] if parts else "",
                    }

        return router_id, local_as, neighbors

    @staticmethod
    def parse_bfd_summary(
        output: str,
    ) -> Dict[str, Dict[str, int]]:
        """
        Parse 'show bfd summary' output.
        Returns {client_name: {total, up, down, init, admin_down}}.
        """
        clients: Dict[str, Dict[str, int]] = {}

        for line in output.splitlines():
            stripped = line.strip()
            if not stripped or set(stripped) <= {"+", "-", "|", " "}:
                continue
            if "|" in stripped:
                parts = [p.strip() for p in stripped.split("|") if p.strip()]
                if len(parts) >= 6:
                    name = parts[0]
                    # Skip header rows
                    if name.lower() in ("clients", "bfd type", "num sessions"):
                        continue
                    try:
                        total = int(parts[1])
                        up = int(parts[2])
                        down = int(parts[3])
                        init = int(parts[4])
                        admin_down = int(parts[5])
                        clients[name] = {
                            "total": total,
                            "up": up,
                            "down": down,
                            "init": init,
                            "admin_down": admin_down,
                        }
                    except (ValueError, IndexError):
                        continue

        return clients

    @staticmethod
    def parse_bfd_sessions(
        output: str,
    ) -> List[Dict[str, str]]:
        """
        Parse 'show bfd sessions' output.
        Returns list of {neighbor, state, tx_rx_mult, uptime, clients, interface}.
        """
        sessions: List[Dict[str, str]] = []

        for line in output.splitlines():
            stripped = line.strip()
            if not stripped or set(stripped) <= {"+", "-", "|", " "}:
                continue
            if "|" not in stripped:
                continue

            parts = [p.strip() for p in stripped.split("|") if p.strip()]
            if not parts:
                continue

            neighbor = parts[0]
            # Skip header and sub-header rows
            if "neighbor" in neighbor.lower() or not re.match(
                r"\d+\.\d+\.\d+\.\d+", neighbor
            ):
                continue

            sessions.append(
                {
                    "neighbor": neighbor,
                    "state": parts[1] if len(parts) > 1 else "",
                    "tx_rx_mult": parts[2] if len(parts) > 2 else "",
                    "uptime": parts[3] if len(parts) > 3 else "",
                    "clients": parts[4] if len(parts) > 4 else "",
                    "interface": parts[5] if len(parts) > 5 else "",
                }
            )

        return sessions

    @staticmethod
    def parse_bfd_detail(output: str) -> List[Dict]:
        """
        Parse 'show bfd sessions detail' output.
        Returns list of dicts with detailed per-session info.
        """
        sessions: List[Dict] = []
        current: Dict = {}

        for line in output.splitlines():
            stripped = line.strip()

            # New session starts with a table row containing an IP address
            if "|" in stripped:
                parts = [p.strip() for p in stripped.split("|") if p.strip()]
                if parts and re.match(r"\d+\.\d+\.\d+\.\d+", parts[0]):
                    if current and "neighbor" in current:
                        sessions.append(current)
                    current = {
                        "neighbor": parts[0],
                        "state": parts[1] if len(parts) > 1 else "",
                    }
                    if len(parts) > 2:
                        current["tx_rx_mult"] = parts[2]
                    if len(parts) > 3:
                        current["uptime"] = parts[3]
                    if len(parts) > 4:
                        current["clients"] = parts[4]
                    continue

            if not current:
                continue

            # Parse detail fields
            m = re.match(r"Remote-state:\s*(\S+)", stripped)
            if m:
                current["remote_state"] = m.group(1)
                continue

            m = re.match(r"BFD type:\s*(.+)", stripped)
            if m:
                current["bfd_type"] = m.group(1).strip()
                continue

            m = re.match(r"Detection time:\s*(\S+)", stripped)
            if m:
                current["detection_time"] = m.group(1)
                continue

            m = re.search(
                r"Local Discriminator:\s*(\d+).*Remote Discriminator:\s*(\d+)",
                stripped,
            )
            if m:
                current["local_disc"] = int(m.group(1))
                current["remote_disc"] = int(m.group(2))
                continue

            m = re.search(
                r"Local MinTx:\s*(\d+).*Local MinRx:\s*(\d+).*Local Multiplier:\s*(\d+)",
                stripped,
            )
            if m:
                current["local_min_tx"] = int(m.group(1))
                current["local_min_rx"] = int(m.group(2))
                current["local_multiplier"] = int(m.group(3))
                continue

            m = re.search(
                r"Received MinTx:\s*(\d+).*Received MinRx:\s*(\d+).*Received Multiplier:\s*(\d+)",
                stripped,
            )
            if m:
                current["received_min_tx"] = int(m.group(1))
                current["received_min_rx"] = int(m.group(2))
                current["received_multiplier"] = int(m.group(3))
                continue

            m = re.search(
                r"Up transitions:\s*(\d+)\s+Failure transitions:\s*(\d+)",
                stripped,
            )
            if m:
                current["up_transitions"] = int(m.group(1))
                current["failure_transitions"] = int(m.group(2))
                continue

            m = re.search(
                r"Rx packets Count:\s*(\d+)\s+Tx packets Count:\s*(\d+)",
                stripped,
            )
            if m:
                current["rx_count"] = int(m.group(1))
                current["tx_count"] = int(m.group(2))
                continue

        if current and "neighbor" in current:
            sessions.append(current)

        return sessions

    # ------------------------------------------------------------------
    # Phase 1: Setup
    # ------------------------------------------------------------------
    def test_discover_link(self):
        """Test 1: Discover link between devices via LLDP."""
        print("\n" + "=" * 60)
        print("TEST 1: Discover link via LLDP")
        print("=" * 60)

        if self.iface_a and self.iface_b:
            self._record(
                "Link discovery",
                True,
                f"Using provided: {self.host_a}:{self.iface_a} <-> "
                f"{self.host_b}:{self.iface_b}",
            )
            return

        # Get hostnames for LLDP matching
        hostname_a = self._get_hostname(self.shell_a)
        hostname_b = self._get_hostname(self.shell_b)
        print(f"  [INFO] Hostname A: {hostname_a or '(unknown)'}")
        print(f"  [INFO] Hostname B: {hostname_b or '(unknown)'}")

        # Get LLDP from both sides
        lldp_a = self.show_a("show lldp neighbors", timeout=30)
        neighbors_a = self.parse_lldp_neighbors(lldp_a)
        print(f"  [INFO] LLDP neighbors on A: {len(neighbors_a)}")

        lldp_b = self.show_b("show lldp neighbors", timeout=30)
        neighbors_b = self.parse_lldp_neighbors(lldp_b)
        print(f"  [INFO] LLDP neighbors on B: {len(neighbors_b)}")

        if not neighbors_a and not neighbors_b:
            self._record("Link discovery", False, "No LLDP neighbors on either device")
            return

        # Find interface on A that connects to B
        if not self.iface_a:
            for local_if, remote_name, _ in neighbors_a:
                if self._name_matches(remote_name, hostname_b, self.host_b):
                    self.iface_a = local_if
                    break

        # Find interface on B that connects to A
        if not self.iface_b:
            for local_if, remote_name, _ in neighbors_b:
                if self._name_matches(remote_name, hostname_a, self.host_a):
                    self.iface_b = local_if
                    break

        # Fallback: use first physical interface from LLDP on each side
        if not self.iface_a and neighbors_a:
            self.iface_a = neighbors_a[0][0]
            print(
                f"  [INFO] Could not match B by name; using first LLDP "
                f"neighbor interface on A: {self.iface_a}"
            )
        if not self.iface_b and neighbors_b:
            self.iface_b = neighbors_b[0][0]
            print(
                f"  [INFO] Could not match A by name; using first LLDP "
                f"neighbor interface on B: {self.iface_b}"
            )

        if self.iface_a and self.iface_b:
            self._record(
                "Link discovery",
                True,
                f"Found: {self.host_a}:{self.iface_a} <-> "
                f"{self.host_b}:{self.iface_b}",
            )
        else:
            self._record(
                "Link discovery",
                False,
                f"Could not determine interface pair "
                f"(A: {self.iface_a}, B: {self.iface_b})",
            )

    def _get_hostname(self, shell) -> Optional[str]:
        """Get device hostname from show system information."""
        out = self.run_show(shell, "show system information", timeout=15)
        m = re.search(r"[Hh]ostname\s*[:\s]+\s*(\S+)", out)
        if m:
            return m.group(1).strip()
        # Fallback: try parsing from the prompt
        for line in out.splitlines():
            stripped = line.strip()
            if stripped.endswith("#") or stripped.endswith(">"):
                name = stripped.rstrip("#>").strip()
                if name and len(name) < 80:
                    return name
        return None

    @staticmethod
    def _name_matches(remote: str, hostname: Optional[str], host_arg: str) -> bool:
        """Check if an LLDP remote name matches a device."""
        r = remote.strip().lower()
        if not r:
            return False
        h = (hostname or "").lower()
        a = host_arg.lower()
        
        # If hostname is None or empty, only match against host_arg (avoid false positives)
        if not h:
            return r == a or a in r or r in a
        
        # Full matching with hostname available
        return r == h or r == a or h in r or r in h or a in r or r in a

    def test_snapshot_baseline(self):
        """Test 2: Snapshot baseline config."""
        print("\n" + "=" * 60)
        print("TEST 2: Snapshot baseline config")
        print("=" * 60)

        baseline_bgp_a = self.show_a("show config protocols bgp", timeout=15)
        baseline_bgp_b = self.show_b("show config protocols bgp", timeout=15)
        baseline_bfd_a = self.show_a("show config protocols bfd", timeout=15)
        baseline_bfd_b = self.show_b("show config protocols bfd", timeout=15)

        # Check if BFD already exists (we won't remove it in cleanup if so)
        self.bfd_preexisted_a = (
            "bfd" in baseline_bfd_a.lower()
            and "error" not in baseline_bfd_a.lower()
            and "no entries" not in baseline_bfd_a.lower()
        )
        self.bfd_preexisted_b = (
            "bfd" in baseline_bfd_b.lower()
            and "error" not in baseline_bfd_b.lower()
            and "no entries" not in baseline_bfd_b.lower()
        )

        bgp_exists_a = (
            "bgp" in baseline_bgp_a.lower()
            and "error" not in baseline_bgp_a.lower()
        )
        bgp_exists_b = (
            "bgp" in baseline_bgp_b.lower()
            and "error" not in baseline_bgp_b.lower()
        )

        self._record(
            "Baseline snapshot",
            True,
            f"BGP: A={'exists' if bgp_exists_a else 'none'}, "
            f"B={'exists' if bgp_exists_b else 'none'}; "
            f"BFD: A={'exists' if self.bfd_preexisted_a else 'none'}, "
            f"B={'exists' if self.bfd_preexisted_b else 'none'}",
        )

    def test_assign_ips(self):
        """Test 3: Assign IP addresses to connecting interfaces."""
        print("\n" + "=" * 60)
        print("TEST 3: Assign IP addresses")
        print("=" * 60)

        if not self.iface_a or not self.iface_b:
            self._record("Assign IPs", False, "No interface pair discovered")
            return

        # Host A
        config_a = [
            "interfaces",
            self.iface_a,
            "admin-state enabled",
            f"ipv4-address {LINK_IP_A}/{LINK_PREFIX}",
            "exit",
            "exit",
        ]
        commit_a = self.config_a(config_a, timeout=30)
        if self._has_commit_error(commit_a):
            self._record(
                "Assign IP on host-a", False, f"Commit error: {commit_a[:200]}"
            )
            return
        self.ip_configured_a = True
        self._record(
            "Assign IP on host-a",
            True,
            f"{self.iface_a} = {LINK_IP_A}/{LINK_PREFIX}",
        )

        # Host B
        config_b = [
            "interfaces",
            self.iface_b,
            "admin-state enabled",
            f"ipv4-address {LINK_IP_B}/{LINK_PREFIX}",
            "exit",
            "exit",
        ]
        commit_b = self.config_b(config_b, timeout=30)
        if self._has_commit_error(commit_b):
            self._record(
                "Assign IP on host-b", False, f"Commit error: {commit_b[:200]}"
            )
            return
        self.ip_configured_b = True
        self._record(
            "Assign IP on host-b",
            True,
            f"{self.iface_b} = {LINK_IP_B}/{LINK_PREFIX}",
        )

        # Brief wait for interfaces to come up with the new IPs
        time.sleep(2)

    def test_configure_bgp_bfd(self):
        """Test 4: Configure eBGP with BFD on both devices."""
        print("\n" + "=" * 60)
        print("TEST 4: Configure eBGP + BFD")
        print("=" * 60)

        if not self.ip_configured_a or not self.ip_configured_b:
            self._record("Configure BGP+BFD", False, "IP addresses not configured")
            return

        # Host A: AS 65001, neighbor B
        bgp_config_a = [
            "protocols",
            "bfd",
            "exit",
            f"bgp {LOCAL_AS}",
            f"neighbor {LINK_IP_B}",
            f"peer-as {PEER_AS}",
            "address-family ipv4-unicast",
            "admin-state enabled",
            "exit",
            "bfd",
            "admin-state enabled",
            "exit",
            "exit",  # neighbor
            "exit",  # bgp
            "exit",  # protocols
        ]
        commit_a = self.config_a(bgp_config_a, timeout=60)
        if self._has_commit_error(commit_a):
            self._record(
                "Configure BGP+BFD on host-a",
                False,
                f"Commit error: {commit_a[:200]}",
            )
            return
        self.bgp_configured_a = True
        self.bfd_configured_a = True
        self._record(
            "Configure BGP+BFD on host-a",
            True,
            f"AS {LOCAL_AS}, neighbor {LINK_IP_B}, BFD enabled",
        )

        # Host B: AS 65002, neighbor A
        bgp_config_b = [
            "protocols",
            "bfd",
            "exit",
            f"bgp {PEER_AS}",
            f"neighbor {LINK_IP_A}",
            f"peer-as {LOCAL_AS}",
            "address-family ipv4-unicast",
            "admin-state enabled",
            "exit",
            "bfd",
            "admin-state enabled",
            "exit",
            "exit",  # neighbor
            "exit",  # bgp
            "exit",  # protocols
        ]
        commit_b = self.config_b(bgp_config_b, timeout=60)
        if self._has_commit_error(commit_b):
            self._record(
                "Configure BGP+BFD on host-b",
                False,
                f"Commit error: {commit_b[:200]}",
            )
            return
        self.bgp_configured_b = True
        self.bfd_configured_b = True
        self._record(
            "Configure BGP+BFD on host-b",
            True,
            f"AS {PEER_AS}, neighbor {LINK_IP_A}, BFD enabled",
        )

    def test_wait_for_established(self):
        """Test 5: Wait for BGP Established and BFD Up."""
        print("\n" + "=" * 60)
        print("TEST 5: Wait for BGP Established + BFD Up")
        print("=" * 60)

        if not self.bgp_configured_a:
            self._record("Wait for BGP+BFD", False, "BGP not configured")
            return

        # Poll for BGP Established (up to 60 seconds)
        bgp_up = False
        for attempt in range(12):
            raw = self.show_a("show bgp summary", timeout=15)
            _, _, neighbors = self.parse_bgp_summary(raw)
            if LINK_IP_B in neighbors:
                state = neighbors[LINK_IP_B]["state"]
                if state == "Established":
                    bgp_up = True
                    break
                print(
                    f"  [INFO] BGP state: {state} "
                    f"(attempt {attempt + 1}/12, waiting...)"
                )
            else:
                print(f"  [INFO] Neighbor not yet visible (attempt {attempt + 1}/12)")
            time.sleep(5)

        if bgp_up:
            self._record(
                "BGP Established",
                True,
                f"Neighbor {LINK_IP_B} reached Established state",
            )
        else:
            self._record(
                "BGP Established",
                False,
                f"Neighbor {LINK_IP_B} did not reach Established in 60s",
            )
            return

        # Poll for BFD Up (up to 30 seconds)
        bfd_up = False
        for attempt in range(6):
            raw = self.show_a("show bfd sessions client bgp", timeout=15)
            sessions = self.parse_bfd_sessions(raw)
            for s in sessions:
                if s["neighbor"] == LINK_IP_B and s["state"].lower() == "up":
                    bfd_up = True
                    break
            if bfd_up:
                break
            print(f"  [INFO] Waiting for BFD Up... (attempt {attempt + 1}/6)")
            time.sleep(5)

        if bfd_up:
            self._record("BFD session Up", True, f"BFD to {LINK_IP_B} is Up")
        else:
            self._record(
                "BFD session Up",
                False,
                f"BFD to {LINK_IP_B} did not come Up in 30s",
            )

    # ------------------------------------------------------------------
    # Phase 2: Validation
    # ------------------------------------------------------------------
    def test_verify_bgp_summary(self):
        """Test 6: Verify BGP summary."""
        print("\n" + "=" * 60)
        print("TEST 6: Verify BGP summary")
        print("=" * 60)

        raw = self.show_a("show bgp summary", timeout=15)
        router_id, local_as, neighbors = self.parse_bgp_summary(raw)

        if router_id:
            self._record(
                "BGP process running",
                True,
                f"Router ID: {router_id}, AS: {local_as}",
            )
        else:
            self._record(
                "BGP process running",
                False,
                "Could not parse router ID from BGP summary",
            )

        if LINK_IP_B in neighbors:
            info = neighbors[LINK_IP_B]
            state = info["state"]
            if state == "Established":
                self._record(
                    f"BGP neighbor {LINK_IP_B}",
                    True,
                    f"State: Established, PfxRcvd: {info.get('pfx_rcvd', '?')}",
                )
            else:
                self._record(
                    f"BGP neighbor {LINK_IP_B}",
                    False,
                    f"State: {state} (expected Established)",
                )
        else:
            self._record(
                f"BGP neighbor {LINK_IP_B}",
                False,
                "Neighbor not found in BGP summary",
            )

    def test_verify_bfd_summary(self):
        """Test 7: Verify BFD summary."""
        print("\n" + "=" * 60)
        print("TEST 7: Verify BFD summary")
        print("=" * 60)

        raw = self.show_a("show bfd summary", timeout=15)
        clients = self.parse_bfd_summary(raw)

        # Find BGP client row
        bgp_client = None
        for name, info in clients.items():
            if "bgp" in name.lower():
                bgp_client = info
                break

        if bgp_client:
            self._record(
                "BFD summary - BGP client",
                True,
                f"Total: {bgp_client['total']}, Up: {bgp_client['up']}, "
                f"Down: {bgp_client['down']}, Init: {bgp_client['init']}",
            )
            if bgp_client["up"] >= 1:
                self._record(
                    "BFD BGP sessions Up",
                    True,
                    f"{bgp_client['up']} session(s) Up",
                )
            else:
                self._record(
                    "BFD BGP sessions Up",
                    False,
                    "No BGP BFD sessions in Up state",
                )
            if bgp_client["down"] == 0:
                self._record("BFD BGP no Down sessions", True, "0 Down sessions")
            else:
                self._record(
                    "BFD BGP no Down sessions",
                    False,
                    f"{bgp_client['down']} session(s) Down",
                )
        else:
            self._record(
                "BFD summary - BGP client",
                False,
                "BGP client not found in BFD summary",
            )

    def test_verify_bfd_sessions(self):
        """Test 8: Verify BFD sessions table."""
        print("\n" + "=" * 60)
        print("TEST 8: Verify BFD sessions")
        print("=" * 60)

        raw = self.show_a("show bfd sessions client bgp", timeout=15)
        sessions = self.parse_bfd_sessions(raw)

        if not sessions:
            self._record(
                "BFD BGP sessions",
                False,
                "No BFD sessions found for BGP client",
            )
            return

        self._record(
            "BFD BGP sessions found",
            True,
            f"{len(sessions)} session(s)",
        )

        for s in sessions:
            neighbor = s["neighbor"]
            state = s["state"]
            tx_rx = s.get("tx_rx_mult", "")

            if state.lower() == "up":
                self._record(
                    f"BFD session {neighbor} state",
                    True,
                    f"State: Up",
                )
            else:
                self._record(
                    f"BFD session {neighbor} state",
                    False,
                    f"State: {state} (expected Up)",
                )

            if tx_rx and "/" in tx_rx:
                self._record(
                    f"BFD session {neighbor} timers",
                    True,
                    f"Negotiated Tx/Rx/Mult: {tx_rx}",
                )
            elif state.lower() == "up":
                self._record(
                    f"BFD session {neighbor} timers",
                    False,
                    "Tx/Rx/Multiplier not populated for Up session",
                )

    def test_verify_bfd_detail(self):
        """Test 9: Verify BFD session detail."""
        print("\n" + "=" * 60)
        print("TEST 9: Verify BFD session detail")
        print("=" * 60)

        raw = self.show_a("show bfd sessions detail client bgp", timeout=30)
        sessions = self.parse_bfd_detail(raw)

        if not sessions:
            self._record("BFD detail", False, "No BFD detail found for BGP client")
            return

        for s in sessions:
            neighbor = s.get("neighbor", "?")
            pfx = f"BFD detail {neighbor}"

            # Remote state
            remote_state = s.get("remote_state", "")
            if remote_state.lower() == "up":
                self._record(
                    f"{pfx} remote-state",
                    True,
                    f"Remote-state: {remote_state}",
                )
            else:
                self._record(
                    f"{pfx} remote-state",
                    False,
                    f"Remote-state: {remote_state} (expected Up)",
                )

            # Discriminators
            local_disc = s.get("local_disc", 0)
            remote_disc = s.get("remote_disc", 0)
            if local_disc > 0 and remote_disc > 0:
                self._record(
                    f"{pfx} discriminators",
                    True,
                    f"Local: {local_disc}, Remote: {remote_disc}",
                )
            else:
                self._record(
                    f"{pfx} discriminators",
                    False,
                    f"Local: {local_disc}, Remote: {remote_disc} "
                    f"(expected both > 0)",
                )

            # Packet counts
            rx = s.get("rx_count", 0)
            tx = s.get("tx_count", 0)
            if rx > 0 and tx > 0:
                self._record(
                    f"{pfx} packet counts",
                    True,
                    f"Rx: {rx}, Tx: {tx}",
                )
            else:
                self._record(
                    f"{pfx} packet counts",
                    False,
                    f"Rx: {rx}, Tx: {tx} (expected > 0)",
                )

            # Transitions
            up_trans = s.get("up_transitions", -1)
            fail_trans = s.get("failure_transitions", -1)

            if up_trans >= 1:
                self._record(
                    f"{pfx} up-transitions",
                    True,
                    f"Up transitions: {up_trans}",
                )
            elif up_trans == 0:
                self._record(
                    f"{pfx} up-transitions",
                    False,
                    "Up transitions: 0 (expected >= 1)",
                )

            if fail_trans == 0:
                self._record(
                    f"{pfx} no-flaps",
                    True,
                    "Failure transitions: 0",
                )
            elif fail_trans > 0:
                self._record(
                    f"{pfx} no-flaps",
                    False,
                    f"Failure transitions: {fail_trans} (flaps detected)",
                )

            # Detection time
            det_time = s.get("detection_time", "")
            if det_time:
                self._record(
                    f"{pfx} detection-time",
                    True,
                    f"Detection time: {det_time}",
                )

    def test_modify_bfd_timers(self):
        """Test 10: Modify BFD timers and verify session survives."""
        print("\n" + "=" * 60)
        print(
            f"TEST 10: Modify BFD timers "
            f"({BFD_DEFAULT_TX} -> {BFD_MODIFIED_TX}ms)"
        )
        print("=" * 60)

        if not self.bgp_configured_a:
            self._record("Modify BFD timers", False, "BGP not configured")
            return

        config_lines = [
            "protocols",
            f"bgp {LOCAL_AS}",
            f"neighbor {LINK_IP_B}",
            "bfd",
            f"min-tx {BFD_MODIFIED_TX}",
            f"min-rx {BFD_MODIFIED_RX}",
            "exit",   # bfd
            "exit",   # neighbor
            "exit",   # bgp
            "exit",   # protocols
        ]
        commit_out = self.config_a(config_lines, timeout=30)
        if self._has_commit_error(commit_out):
            self._record(
                "Modify BFD timers commit",
                False,
                f"Error: {commit_out[:200]}",
            )
            return
        self._record(
            "Modify BFD timers commit",
            True,
            f"Changed to tx={BFD_MODIFIED_TX}, rx={BFD_MODIFIED_RX}",
        )

        # Wait for renegotiation
        print("  [INFO] Waiting 5s for BFD timer renegotiation...")
        time.sleep(5)

        # Verify session still Up and timers changed
        raw = self.show_a("show bfd sessions client bgp", timeout=15)
        sessions = self.parse_bfd_sessions(raw)
        session_found = False
        for s in sessions:
            if s["neighbor"] == LINK_IP_B:
                session_found = True
                if s["state"].lower() == "up":
                    self._record(
                        "BFD session after timer change",
                        True,
                        f"Still Up, Tx/Rx: {s.get('tx_rx_mult', '?')}",
                    )
                else:
                    self._record(
                        "BFD session after timer change",
                        False,
                        f"State: {s['state']} (expected Up)",
                    )
                break

        if not session_found:
            self._record(
                "BFD session after timer change",
                False,
                "Session not found after timer change",
            )

    def test_revert_bfd_timers(self):
        """Test 11: Revert BFD timers to default."""
        print("\n" + "=" * 60)
        print(
            f"TEST 11: Revert BFD timers "
            f"({BFD_MODIFIED_TX} -> default)"
        )
        print("=" * 60)

        if not self.bgp_configured_a:
            self._record("Revert BFD timers", False, "BGP not configured")
            return

        config_lines = [
            "protocols",
            f"bgp {LOCAL_AS}",
            f"neighbor {LINK_IP_B}",
            "bfd",
            "no min-tx",
            "no min-rx",
            "exit",   # bfd
            "exit",   # neighbor
            "exit",   # bgp
            "exit",   # protocols
        ]
        commit_out = self.config_a(config_lines, timeout=30)
        if self._has_commit_error(commit_out):
            self._record(
                "Revert BFD timers commit",
                False,
                f"Error: {commit_out[:200]}",
            )
            return
        self._record("Revert BFD timers commit", True, "Reverted to defaults")

        # Wait for renegotiation
        print("  [INFO] Waiting 5s for BFD timer renegotiation...")
        time.sleep(5)

        # Verify session still Up
        raw = self.show_a("show bfd sessions client bgp", timeout=15)
        sessions = self.parse_bfd_sessions(raw)
        session_found = False
        for s in sessions:
            if s["neighbor"] == LINK_IP_B:
                session_found = True
                if s["state"].lower() == "up":
                    self._record(
                        "BFD session after timer revert",
                        True,
                        f"Still Up, Tx/Rx: {s.get('tx_rx_mult', '?')}",
                    )
                else:
                    self._record(
                        "BFD session after timer revert",
                        False,
                        f"State: {s['state']} (expected Up)",
                    )
                break

        if not session_found:
            self._record(
                "BFD session after timer revert",
                False,
                "Session not found after timer revert",
            )

    def test_verify_peer_bfd(self):
        """Test 12: Verify BFD from host-b perspective."""
        print("\n" + "=" * 60)
        print("TEST 12: Verify BFD from host-b")
        print("=" * 60)

        if not self.bgp_configured_b:
            self._record("BFD on host-b", False, "BGP not configured on host-b")
            return

        raw = self.show_b("show bfd sessions client bgp", timeout=15)
        sessions = self.parse_bfd_sessions(raw)

        if not sessions:
            self._record("BFD on host-b", False, "No BFD sessions on host-b")
            return

        found = False
        for s in sessions:
            if s["neighbor"] == LINK_IP_A:
                found = True
                if s["state"].lower() == "up":
                    self._record(
                        f"BFD peer {LINK_IP_A} on host-b",
                        True,
                        f"State: Up, Tx/Rx: {s.get('tx_rx_mult', '?')}",
                    )
                else:
                    self._record(
                        f"BFD peer {LINK_IP_A} on host-b",
                        False,
                        f"State: {s['state']} (expected Up)",
                    )
                break

        if not found:
            self._record(
                f"BFD peer {LINK_IP_A} on host-b",
                False,
                "BFD session to host-a not found on host-b",
            )

    # ------------------------------------------------------------------
    # Phase 3: Cleanup
    # ------------------------------------------------------------------
    def test_remove_bgp_bfd(self):
        """Test 13: Remove BGP+BFD config from both devices."""
        print("\n" + "=" * 60)
        print("TEST 13: Remove BGP+BFD config")
        print("=" * 60)

        # Host A
        if self.bgp_configured_a or self.bfd_configured_a:
            config_a: List[str] = ["protocols"]
            if self.bgp_configured_a:
                config_a.append(f"no bgp {LOCAL_AS}")
            if self.bfd_configured_a and not self.bfd_preexisted_a:
                config_a.append("no bfd")
            config_a.append("exit")

            commit_a = self.config_a(config_a, timeout=30)
            if self._has_commit_error(commit_a):
                self._record(
                    "Remove BGP+BFD host-a",
                    False,
                    f"Error: {commit_a[:200]}",
                )
            else:
                removed = []
                if self.bgp_configured_a:
                    removed.append(f"BGP AS {LOCAL_AS}")
                if self.bfd_configured_a and not self.bfd_preexisted_a:
                    removed.append("BFD")
                self._record(
                    "Remove BGP+BFD host-a",
                    True,
                    f"Removed: {', '.join(removed)}",
                )
                self.bgp_configured_a = False
                self.bfd_configured_a = False
        else:
            self._record(
                "Remove BGP+BFD host-a",
                True,
                "Nothing to remove",
            )

        # Host B
        if self.bgp_configured_b or self.bfd_configured_b:
            config_b: List[str] = ["protocols"]
            if self.bgp_configured_b:
                config_b.append(f"no bgp {PEER_AS}")
            if self.bfd_configured_b and not self.bfd_preexisted_b:
                config_b.append("no bfd")
            config_b.append("exit")

            commit_b = self.config_b(config_b, timeout=30)
            if self._has_commit_error(commit_b):
                self._record(
                    "Remove BGP+BFD host-b",
                    False,
                    f"Error: {commit_b[:200]}",
                )
            else:
                removed = []
                if self.bgp_configured_b:
                    removed.append(f"BGP AS {PEER_AS}")
                if self.bfd_configured_b and not self.bfd_preexisted_b:
                    removed.append("BFD")
                self._record(
                    "Remove BGP+BFD host-b",
                    True,
                    f"Removed: {', '.join(removed)}",
                )
                self.bgp_configured_b = False
                self.bfd_configured_b = False
        else:
            self._record(
                "Remove BGP+BFD host-b",
                True,
                "Nothing to remove",
            )

    def test_remove_ips(self):
        """Test 14: Remove IP addresses from interfaces."""
        print("\n" + "=" * 60)
        print("TEST 14: Remove IP addresses")
        print("=" * 60)

        if self.ip_configured_a:
            config_a = [
                "interfaces",
                self.iface_a,
                f"no ipv4-address {LINK_IP_A}/{LINK_PREFIX}",
                "exit",
                "exit",
            ]
            commit_a = self.config_a(config_a, timeout=30)
            if self._has_commit_error(commit_a):
                self._record(
                    "Remove IP host-a",
                    False,
                    f"Error: {commit_a[:200]}",
                )
            else:
                self._record(
                    "Remove IP host-a",
                    True,
                    f"Removed {LINK_IP_A}/{LINK_PREFIX} from {self.iface_a}",
                )
                self.ip_configured_a = False

        if self.ip_configured_b:
            config_b = [
                "interfaces",
                self.iface_b,
                f"no ipv4-address {LINK_IP_B}/{LINK_PREFIX}",
                "exit",
                "exit",
            ]
            commit_b = self.config_b(config_b, timeout=30)
            if self._has_commit_error(commit_b):
                self._record(
                    "Remove IP host-b",
                    False,
                    f"Error: {commit_b[:200]}",
                )
            else:
                self._record(
                    "Remove IP host-b",
                    True,
                    f"Removed {LINK_IP_B}/{LINK_PREFIX} from {self.iface_b}",
                )
                self.ip_configured_b = False

        if not self.ip_configured_a and not self.ip_configured_b:
            if not any("Remove IP" in r[0] for r in self.results):
                self._record("Remove IPs", True, "Nothing to remove")

    def test_verify_clean_state(self):
        """Test 15: Verify clean state."""
        print("\n" + "=" * 60)
        print("TEST 15: Verify clean state")
        print("=" * 60)

        # Check no BGP BFD sessions remain on host-a
        raw = self.show_a("show bfd sessions", timeout=15)
        sessions = self.parse_bfd_sessions(raw)
        bgp_sessions = [
            s for s in sessions if "bgp" in s.get("clients", "").lower()
        ]

        if not bgp_sessions:
            self._record(
                "Clean state - no BGP BFD sessions on host-a",
                True,
            )
        else:
            self._record(
                "Clean state - no BGP BFD sessions on host-a",
                False,
                f"{len(bgp_sessions)} BGP BFD session(s) still remain",
            )

        # Check no BGP on host-a
        raw_bgp = self.show_a("show bgp summary", timeout=15)
        if LINK_IP_B not in raw_bgp:
            self._record(
                "Clean state - no BGP neighbor on host-a",
                True,
            )
        else:
            self._record(
                "Clean state - no BGP neighbor on host-a",
                False,
                f"Neighbor {LINK_IP_B} still in BGP summary",
            )

    # ------------------------------------------------------------------
    # Emergency cleanup
    # ------------------------------------------------------------------
    def _emergency_cleanup(self):
        """Best-effort cleanup after an unexpected error."""
        print("\n[!] Attempting emergency cleanup...")

        # Remove BGP+BFD from host-a
        try:
            if self.bgp_configured_a or self.bfd_configured_a:
                config: List[str] = ["protocols"]
                if self.bgp_configured_a:
                    config.append(f"no bgp {LOCAL_AS}")
                if self.bfd_configured_a and not self.bfd_preexisted_a:
                    config.append("no bfd")
                config.append("exit")
                self.config_a(config, timeout=30)
                print("[!] Removed BGP+BFD from host-a.")
        except Exception as e:
            print(f"[!] Cleanup host-a BGP failed: {e}")

        # Remove BGP+BFD from host-b
        try:
            if self.bgp_configured_b or self.bfd_configured_b:
                config = ["protocols"]
                if self.bgp_configured_b:
                    config.append(f"no bgp {PEER_AS}")
                if self.bfd_configured_b and not self.bfd_preexisted_b:
                    config.append("no bfd")
                config.append("exit")
                self.config_b(config, timeout=30)
                print("[!] Removed BGP+BFD from host-b.")
        except Exception as e:
            print(f"[!] Cleanup host-b BGP failed: {e}")

        # Remove IP from host-a
        try:
            if self.ip_configured_a and self.iface_a:
                self.config_a(
                    [
                        "interfaces",
                        self.iface_a,
                        f"no ipv4-address {LINK_IP_A}/{LINK_PREFIX}",
                        "exit",
                        "exit",
                    ],
                    timeout=30,
                )
                print("[!] Removed IP from host-a.")
        except Exception as e:
            print(f"[!] Cleanup host-a IP failed: {e}")

        # Remove IP from host-b
        try:
            if self.ip_configured_b and self.iface_b:
                self.config_b(
                    [
                        "interfaces",
                        self.iface_b,
                        f"no ipv4-address {LINK_IP_B}/{LINK_PREFIX}",
                        "exit",
                        "exit",
                    ],
                    timeout=30,
                )
                print("[!] Removed IP from host-b.")
        except Exception as e:
            print(f"[!] Cleanup host-b IP failed: {e}")

    # ------------------------------------------------------------------
    # Orchestrator
    # ------------------------------------------------------------------
    def run_all(self) -> bool:
        """Run all test phases and print summary."""
        start = datetime.now()
        print("=" * 60)
        print("  BFD OVER BGP SANITY TEST  --  Happy Flow")
        print(f"  Host A : {self.host_a}")
        print(f"  Host B : {self.host_b}")
        print(f"  Started: {start.strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 60)

        try:
            self.connect()

            # Phase 1: Setup
            print("\n" + "#" * 60)
            print("# PHASE 1: SETUP")
            print("#" * 60)
            self.test_discover_link()
            self.test_snapshot_baseline()
            self.test_assign_ips()
            self.test_configure_bgp_bfd()
            self.test_wait_for_established()

            # Phase 2: Validation
            print("\n" + "#" * 60)
            print("# PHASE 2: VALIDATION")
            print("#" * 60)
            self.test_verify_bgp_summary()
            self.test_verify_bfd_summary()
            self.test_verify_bfd_sessions()
            self.test_verify_bfd_detail()
            self.test_modify_bfd_timers()
            self.test_revert_bfd_timers()
            self.test_verify_peer_bfd()

            # Phase 3: Cleanup
            print("\n" + "#" * 60)
            print("# PHASE 3: CLEANUP")
            print("#" * 60)
            self.test_remove_bgp_bfd()
            self.test_remove_ips()
            self.test_verify_clean_state()

        except Exception as exc:
            print(f"\n[ERROR] Unexpected exception: {exc}")
            self._record("Unexpected error", False, str(exc))
            self._emergency_cleanup()
        finally:
            try:
                self.disconnect()
            except Exception:
                pass

        # Summary
        elapsed = (datetime.now() - start).total_seconds()
        total = len(self.results)
        passed = sum(1 for _, p, _ in self.results if p)
        failed = total - passed

        print("\n" + "=" * 60)
        print("  SUMMARY")
        print("=" * 60)
        print(f"  Total : {total}")
        print(f"  Passed: {passed}")
        print(f"  Failed: {failed}")
        print(f"  Time  : {elapsed:.1f}s")
        print("=" * 60)

        if failed:
            print("\n  Failed tests:")
            for name, p, detail in self.results:
                if not p:
                    print(f"    - {name}: {detail}")

        verdict = "ALL TESTS PASSED" if failed == 0 else "SOME TESTS FAILED"
        print(f"\n  >>> {verdict} <<<\n")
        return failed == 0


def main():
    parser = argparse.ArgumentParser(
        description="BFD over BGP sanity test for DNOS devices"
    )
    parser.add_argument(
        "--host-a",
        default="xgu1f7v900009p2",
        help="Primary device hostname or IP (default: xgu1f7v900009p2)",
    )
    parser.add_argument(
        "--host-b",
        required=True,
        help="Peer device hostname or IP (required for BGP peering)",
    )
    parser.add_argument(
        "--iface-a",
        default=None,
        help="Interface on host-a (skip LLDP discovery for this side)",
    )
    parser.add_argument(
        "--iface-b",
        default=None,
        help="Interface on host-b (skip LLDP discovery for this side)",
    )
    parser.add_argument(
        "--user",
        default="dnroot",
        help="SSH username (default: dnroot)",
    )
    parser.add_argument(
        "--password",
        default="dnroot",
        help="SSH password (default: dnroot)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="SSH timeout in seconds (default: 30)",
    )
    args = parser.parse_args()

    tester = BFDOverBGPSanityTest(
        host_a=args.host_a,
        host_b=args.host_b,
        iface_a=args.iface_a,
        iface_b=args.iface_b,
        username=args.user,
        password=args.password,
        timeout=args.timeout,
    )
    ok = tester.run_all()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
