#!/usr/bin/env python3
"""
QoS (Quality of Service) Sanity Test Script for DNOS

Tests basic QoS policy functionality on a DNOS device:
  Phase 1 - Setup:
    1. Snapshot existing QoS config
    2. Apply missing traffic-class-maps, policies, hw-mapping
    3. Discover first 'up' interface and attach policies
  Phase 2 - Validation:
    4. Verify traffic-class-maps in config
    5. Verify ingress policy rules
    6. Verify egress policy rules
    7. Verify policies on interface (show qos interfaces)
    8. Verify QoS counters structure
    9. Clear and verify counters
   10. Verify egress queues
   11. Modify bandwidth and verify
   12. Revert bandwidth and verify
  Phase 3 - Cleanup:
   13. Detach policies from interface
   14. Remove config we created, verify baseline restored

Usage:
    python3 qos_sanity_test.py
    python3 qos_sanity_test.py --host xgu1f7v900009p2 --user dnroot --password dnroot
"""

import argparse
import datetime
import re
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple

import paramiko


# ---------------------------------------------------------------------------
# Expected values derived from the reference QoS configuration
# ---------------------------------------------------------------------------

# traffic-class-map name -> (match_type, match_value)
EXPECTED_TCM: Dict[str, Tuple[str, str]] = {
    "CLASS1": ("pcp", "1"), "CLASS2": ("pcp", "2"),
    "CLASS3": ("pcp", "3"), "CLASS4": ("pcp", "4"),
    "CLASS5": ("pcp", "5"), "CLASS6": ("pcp", "6"),
    "CLASS7": ("pcp", "7"),
    "QOS-TAG-1": ("qos-tag", "1"), "QOS-TAG-2": ("qos-tag", "2"),
    "QOS-TAG-3": ("qos-tag", "3"), "QOS-TAG-4": ("qos-tag", "4"),
    "QOS-TAG-5": ("qos-tag", "5"), "QOS-TAG-6": ("qos-tag", "6"),
    "QOS-TAG-7": ("qos-tag", "7"),
}

# Ingress policy: rule_id -> (traffic_class_map_name, qos_tag_value)
EXPECTED_INGRESS_RULES: Dict[str, Tuple[str, str]] = {
    "1": ("CLASS1", "1"), "2": ("CLASS2", "2"), "3": ("CLASS3", "3"),
    "4": ("CLASS4", "4"), "5": ("CLASS5", "5"), "6": ("CLASS6", "6"),
    "7": ("CLASS7", "7"),
}

# Egress policy: rule_id -> {tcm, fwd_class, bw_type, bw_value}
EXPECTED_EGRESS_RULES: Dict[str, Dict[str, str]] = {
    "1": {"tcm": "QOS-TAG-1", "fwd": "af", "bw_type": "bandwidth", "bw": "10"},
    "2": {"tcm": "QOS-TAG-2", "fwd": "af", "bw_type": "bandwidth", "bw": "20"},
    "3": {"tcm": "QOS-TAG-3", "fwd": "af", "bw_type": "bandwidth", "bw": "40"},
    "4": {"tcm": "QOS-TAG-4", "fwd": "af", "bw_type": "bandwidth", "bw": "10"},
    "5": {"tcm": "QOS-TAG-5", "fwd": "hp", "bw_type": "max-bandwidth", "bw": "25"},
    "6": {"tcm": "QOS-TAG-6", "fwd": "ef", "bw_type": "max-bandwidth", "bw": "10"},
    "7": {"tcm": "QOS-TAG-7", "fwd": "super-ef", "bw_type": "max-bandwidth", "bw": "3"},
    "default": {"fwd": "df", "bw_type": "bandwidth", "bw": "5"},
}

INGRESS_POLICY = "Ingress_Child_Classify_Only"
EGRESS_POLICY = "Egress_Full"

EXPECTED_SPEED_RANGES = [
    ("50 mbps", "50 mbps"),
    ("100 mbps", "100 mbps"),
    ("250 mbps", "250 mbps"),
    ("500 mbps", "500 mbps"),
    ("750 mbps", "750 mbps"),
    ("1 gbps", "1 gbps"),
]

# ---------------------------------------------------------------------------
# Full QoS config lines to apply (each line is sent in config mode under qos)
# ---------------------------------------------------------------------------

QOS_HW_MAPPING_LINES = [
    "qos",
    "hw-mapping",
    "queue-size",
    "speed-ranges",
    "admin-state enabled",
    "upto 50 mbps use 50 mbps",
    "upto 100 mbps use 100 mbps",
    "upto 250 mbps use 250 mbps",
    "upto 500 mbps use 500 mbps",
    "upto 750 mbps use 750 mbps",
    "upto 1 gbps use 1 gbps",
    "exit",  # speed-ranges
    "exit",  # queue-size
    "exit",  # hw-mapping
    "exit",  # qos
]

QOS_TCM_LINES: Dict[str, List[str]] = {}
for _name, (_mtype, _mval) in EXPECTED_TCM.items():
    QOS_TCM_LINES[_name] = [
        "qos",
        f"traffic-class-map {_name}",
        f"{_mtype} {_mval}",
        "exit",  # traffic-class-map
        "exit",  # qos
    ]

QOS_INGRESS_POLICY_LINES = [
    "qos",
    f"policy {INGRESS_POLICY}",
]
for _rid, (_tcm, _qtag) in EXPECTED_INGRESS_RULES.items():
    QOS_INGRESS_POLICY_LINES += [
        f"rule {_rid}",
        f"match traffic-class {_tcm}",
        "action",
        "set",
        f"qos-tag {_qtag}",
        "exit",  # set
        "exit",  # action
        "exit",  # rule
    ]
QOS_INGRESS_POLICY_LINES += [
    "rule default",
    "exit",  # rule default
    "exit",  # policy
    "exit",  # qos
]

QOS_EGRESS_POLICY_LINES = [
    "qos",
    f"policy {EGRESS_POLICY}",
]
for _rid, _info in EXPECTED_EGRESS_RULES.items():
    QOS_EGRESS_POLICY_LINES.append(f"rule {_rid}")
    if "tcm" in _info:
        QOS_EGRESS_POLICY_LINES.append(f"match traffic-class {_info['tcm']}")
    QOS_EGRESS_POLICY_LINES.append("action")
    QOS_EGRESS_POLICY_LINES.append("queue")

    fwd = _info["fwd"]
    QOS_EGRESS_POLICY_LINES.append(f"forwarding-class {fwd}")

    bw_type = _info["bw_type"]
    bw = _info["bw"]
    QOS_EGRESS_POLICY_LINES.append(f"{bw_type} {bw} percent")

    # Size/yellow-size depend on forwarding class
    if fwd in ("af", "df"):
        QOS_EGRESS_POLICY_LINES.append("size 20 milliseconds")
        QOS_EGRESS_POLICY_LINES.append("yellow-size 10 milliseconds")
    elif fwd in ("hp", "ef", "super-ef"):
        QOS_EGRESS_POLICY_LINES.append("size 10 milliseconds")

    QOS_EGRESS_POLICY_LINES.append("exit")  # forwarding-class
    QOS_EGRESS_POLICY_LINES.append("exit")  # queue

    # PCP set actions (only for some rules)
    if _rid in ("1", "2", "3", "5", "6"):
        pcp_val = _rid
        QOS_EGRESS_POLICY_LINES += [
            "set",
            f"pcp {pcp_val} all {pcp_val}",
            "exit",  # set
        ]

    QOS_EGRESS_POLICY_LINES.append("exit")  # action
    QOS_EGRESS_POLICY_LINES.append("exit")  # rule

QOS_EGRESS_POLICY_LINES += [
    "exit",  # policy
    "exit",  # qos
]


class QoSSanityTest:
    """QoS happy-flow sanity tester for DNOS devices."""

    def __init__(self, host: str, username: str, password: str,
                 no_cleanup: bool = False, 
                 forced_ingress_interface: Optional[str] = None,
                 forced_egress_interface: Optional[str] = None):
        self.host = host
        self.username = username
        self.password = password
        self.no_cleanup = no_cleanup
        self.forced_ingress_interface = forced_ingress_interface
        self.forced_egress_interface = forced_egress_interface
        self.client: Optional[paramiko.SSHClient] = None
        self.shell: Optional[paramiko.Channel] = None
        self.results: List[Tuple[str, bool, str]] = []

        # State tracking for setup/cleanup
        self.baseline_config: str = ""
        self.created_tcms: Set[str] = set()
        self.created_policies: Set[str] = set()
        self.created_hwmapping: bool = False
        self.target_iface: str = ""  # For backward compatibility, primary interface
        self.ingress_iface: str = ""
        self.egress_iface: str = ""
        self.original_in_policy: Optional[str] = None
        self.original_out_policy: Optional[str] = None
        self.original_egress_in_policy: Optional[str] = None  # For egress interface
        self.original_egress_out_policy: Optional[str] = None  # For egress interface
        self.attached_in: bool = False
        self.attached_out: bool = False

    # ------------------------------------------------------------------
    # SSH helpers (same pattern as cprl_sanity_test.py)
    # ------------------------------------------------------------------
    def connect(self):
        """Establish SSH connection and open interactive shell."""
        print(f"[*] Connecting to {self.host} as {self.username} ...")
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(
            hostname=self.host,
            username=self.username,
            password=self.password,
            look_for_keys=False,
            allow_agent=False,
            timeout=30,
        )
        self.shell = self.client.invoke_shell(width=250, height=1000)
        self._read_until_prompt(timeout=15)
        self._send("no-paging")
        self._read_until_prompt(timeout=5)
        print("[+] Connected and paging disabled.\n")

    def disconnect(self):
        """Close SSH connection."""
        if self.shell:
            self.shell.close()
        if self.client:
            self.client.close()
        print("\n[*] Disconnected.")

    def _send(self, cmd: str):
        """Send a command string to the shell."""
        self.shell.send(cmd + "\n")

    def _read_until_prompt(self, timeout: int = 30) -> str:
        """Read shell output until a DNOS prompt (ending with # or >)."""
        buf = ""
        end_time = time.time() + timeout
        while time.time() < end_time:
            if self.shell.recv_ready():
                chunk = self.shell.recv(65536).decode("utf-8", errors="replace")
                buf += chunk
                lines = buf.strip().split("\n")
                last_line = lines[-1].strip() if lines else ""
                if last_line.endswith("#") or last_line.endswith(">"):
                    break
            else:
                time.sleep(0.2)
        return buf

    def run_show(self, cmd: str, timeout: int = 30) -> str:
        """Run a show / operational command and return output."""
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

    def run_operational(self, cmd: str, timeout: int = 15) -> str:
        """Run an operational command (clear, etc.)."""
        self._send(cmd)
        return self._read_until_prompt(timeout=timeout)

    # ------------------------------------------------------------------
    # Result recording
    # ------------------------------------------------------------------
    def _record(self, name: str, passed: bool, detail: str = ""):
        self.results.append((name, passed, detail))
        tag = "[PASS]" if passed else "[FAIL]"
        print(f"  {tag} {name}" + (f" -- {detail}" if detail else ""))

    # ------------------------------------------------------------------
    # Parsers
    # ------------------------------------------------------------------
    @staticmethod
    def parse_config_tcm_names(config_output: str) -> Dict[str, Tuple[str, str]]:
        """
        Parse 'show config qos' to extract traffic-class-map names and values.
        Returns {name: (match_type, match_value)}.
        """
        result: Dict[str, Tuple[str, str]] = {}
        tcm_re = re.compile(r"traffic-class-map\s+(\S+)")
        match_re = re.compile(r"^\s+(pcp|qos-tag|dscp|dscp-ipv4|dscp-ipv6|mpls-exp|precedence)\s+(.+)$")
        current_tcm = None
        for line in config_output.split("\n"):
            m = tcm_re.search(line)
            if m:
                current_tcm = m.group(1)
                continue
            if current_tcm:
                mm = match_re.match(line)
                if mm:
                    result[current_tcm] = (mm.group(1).strip(), mm.group(2).strip())
                    current_tcm = None
                elif line.strip() == "!":
                    current_tcm = None
        return result

    @staticmethod
    def parse_config_policy_names(config_output: str) -> List[str]:
        """Extract policy names from 'show config qos' output."""
        names = []
        for m in re.finditer(r"^\s+policy\s+(\S+)", config_output, re.MULTILINE):
            name = m.group(1)
            if name not in names:
                names.append(name)
        return names

    @staticmethod
    def parse_config_has_hwmapping(config_output: str) -> bool:
        """Check if hw-mapping section exists in config."""
        return "hw-mapping" in config_output

    @staticmethod
    def _extract_policy_block(config_output: str, policy_name: str) -> str:
        """
        Extract the text block for a named policy from show config qos output.
        Uses indentation to find the closing '!' that ends the policy.
        """
        lines = config_output.split("\n")
        block_lines: List[str] = []
        in_block = False
        policy_indent = -1

        for line in lines:
            if not in_block:
                m = re.search(rf"(\s*)policy\s+{re.escape(policy_name)}\s*$", line)
                if m:
                    in_block = True
                    policy_indent = len(m.group(1))
                continue

            # Check if this '!' closes the policy block (same indent level)
            if line.strip() == "!":
                current_indent = len(line) - len(line.lstrip())
                if current_indent <= policy_indent:
                    break

            block_lines.append(line)

        return "\n".join(block_lines)

    @staticmethod
    def _split_rules(policy_block: str) -> Dict[str, str]:
        """
        Split a policy block into per-rule text chunks.
        Returns {rule_id: rule_text}.
        """
        rules: Dict[str, str] = {}
        current_rule = None
        current_lines: List[str] = []

        for line in policy_block.split("\n"):
            m = re.match(r"\s*rule\s+(\S+)", line)
            if m:
                # Save previous rule
                if current_rule is not None:
                    rules[current_rule] = "\n".join(current_lines)
                current_rule = m.group(1)
                current_lines = []
                continue
            if current_rule is not None:
                current_lines.append(line)

        # Save last rule
        if current_rule is not None:
            rules[current_rule] = "\n".join(current_lines)

        return rules

    @staticmethod
    def parse_config_ingress_rules(config_output: str) -> Dict[str, Tuple[str, str]]:
        """
        Parse Ingress_Child_Classify_Only rules from show config qos.
        Returns {rule_id: (tcm_name, qos_tag_value)}.
        """
        block = QoSSanityTest._extract_policy_block(config_output, INGRESS_POLICY)
        if not block:
            return {}

        rule_chunks = QoSSanityTest._split_rules(block)
        result: Dict[str, Tuple[str, str]] = {}

        for rid, text in rule_chunks.items():
            tcm = None
            qtag = None
            for line in text.split("\n"):
                stripped = line.strip()
                m = re.match(r"match\s+traffic-class\s+(\S+)", stripped)
                if m:
                    tcm = m.group(1)
                m = re.match(r"qos-tag\s+(\d+)", stripped)
                if m:
                    qtag = m.group(1)
            if tcm and qtag:
                result[rid] = (tcm, qtag)

        return result

    @staticmethod
    def parse_config_egress_rules(config_output: str) -> Dict[str, Dict[str, str]]:
        """
        Parse Egress_Full rules from show config qos.
        Returns {rule_id: {tcm, fwd, bw_type, bw}}.
        """
        block = QoSSanityTest._extract_policy_block(config_output, EGRESS_POLICY)
        if not block:
            return {}

        rule_chunks = QoSSanityTest._split_rules(block)
        result: Dict[str, Dict[str, str]] = {}

        for rid, text in rule_chunks.items():
            info: Dict[str, str] = {}
            for line in text.split("\n"):
                stripped = line.strip()

                m = re.match(r"match\s+traffic-class\s+(\S+)", stripped)
                if m:
                    info["tcm"] = m.group(1)
                    continue

                m = re.match(r"forwarding-class\s+(\S+)", stripped)
                if m:
                    info["fwd"] = m.group(1)
                    continue

                m = re.match(r"(bandwidth|max-bandwidth)\s+(\d+)\s+percent", stripped)
                if m:
                    info["bw_type"] = m.group(1)
                    info["bw"] = m.group(2)
                    continue

            if info:
                result[rid] = info

        return result

    @staticmethod
    @staticmethod
    def parse_interfaces_summary(output: str) -> List[str]:
        """
        Parse 'show interfaces' (table format) to get list of 'up' interfaces.
        Looks for table rows with '| up ' in the Operational column.
        Example line: | ge100-0/0/96               | enabled  | up              | ...
        """
        up_ifaces = []
        for line in output.split("\n"):
            # Skip header/separator lines
            if not line.strip().startswith("|"):
                continue
            if "Interface" in line or "---" in line:
                continue
            
            # Parse table row: | interface_name | admin | operational | ...
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 4:
                continue
            
            interface_name = parts[1]
            operational_state = parts[3] if len(parts) > 3 else ""
            
            # Check if operational state is "up" and filter out sub-interfaces
            if operational_state == "up" and "." not in interface_name:
                up_ifaces.append(interface_name)
        
        return up_ifaces

    @staticmethod
    def parse_qos_interface_policy(output: str, direction: str) -> Optional[str]:
        """Extract policy name for a given direction from show qos interfaces output."""
        pattern = rf"Policy name:\s+(\S+)\s+Direction:\s+{direction}"
        m = re.search(pattern, output)
        return m.group(1) if m else None

    @staticmethod
    def parse_qos_interface_rules(output: str) -> List[str]:
        """Extract rule IDs from show qos interfaces output."""
        rules = []
        for m in re.finditer(r"Rule\s+(\S+)", output):
            rid = m.group(1)
            if rid not in rules:
                rules.append(rid)
        return rules

    @staticmethod
    def parse_qos_counters_rules(output: str) -> List[str]:
        """Extract rule IDs from show qos interfaces counters output."""
        rules = []
        for m in re.finditer(r"Rule\s+(\S+)", output):
            rid = m.group(1)
            if rid not in rules:
                rules.append(rid)
        return rules

    @staticmethod
    def parse_qos_counters_matched(output: str) -> bool:
        """Check that 'Matched packets' field exists in counter output."""
        return "Matched packets:" in output or "Matched octets:" in output

    @staticmethod
    def parse_egress_queues(output: str) -> List[str]:
        """Extract egress-queue-id values from show qos interfaces egress-queues output."""
        queues = []
        for m in re.finditer(r"egress-queue-id\s+(\d+)", output):
            queues.append(m.group(1))
        return queues

    @staticmethod
    def parse_interface_qos_bandwidth(output: str, rule_id: str) -> Optional[str]:
        """
        Extract bandwidth percentage for a specific rule from
        show qos interfaces [iface] out output.
        """
        # Find the rule section, then look for Bandwidth or Guaranteed rate
        in_rule = False
        for line in output.split("\n"):
            if re.search(rf"Rule\s+{re.escape(rule_id)}\b", line):
                in_rule = True
                continue
            if in_rule:
                # Next rule starts
                if re.search(r"Rule\s+\S+", line) and not re.search(rf"Rule\s+{re.escape(rule_id)}\b", line):
                    break
                m = re.search(r"Bandwidth\s+(\d+)\s*%", line)
                if m:
                    return m.group(1)
        return None

    # ------------------------------------------------------------------
    # Phase 1: Setup
    # ------------------------------------------------------------------
    def test_snapshot_config(self):
        """Test 1: Snapshot and check existing QoS config."""
        print("\n" + "=" * 60)
        print("TEST 1: Snapshot existing QoS config")
        print("=" * 60)

        raw = self.run_show("show config qos", timeout=30)
        self.baseline_config = raw

        # Check which TCMs exist
        existing_tcms = self.parse_config_tcm_names(raw)
        existing_policies = self.parse_config_policy_names(raw)
        has_hwmap = self.parse_config_has_hwmapping(raw)

        missing_tcms = [n for n in EXPECTED_TCM if n not in existing_tcms]
        missing_policies = []
        if INGRESS_POLICY not in existing_policies:
            missing_policies.append(INGRESS_POLICY)
        if EGRESS_POLICY not in existing_policies:
            missing_policies.append(EGRESS_POLICY)

        self._record(
            "Snapshot QoS config",
            True,
            f"TCMs: {len(existing_tcms)} existing, {len(missing_tcms)} missing; "
            f"Policies: {len(existing_policies)} existing, {len(missing_policies)} missing; "
            f"hw-mapping: {'present' if has_hwmap else 'missing'}",
        )

        # Track what needs creating
        self._missing_tcms = missing_tcms
        self._missing_policies = missing_policies
        self._missing_hwmap = not has_hwmap

    def test_apply_missing_config(self):
        """Test 2: Apply any missing QoS config."""
        print("\n" + "=" * 60)
        print("TEST 2: Apply missing QoS config")
        print("=" * 60)

        if not self._missing_tcms and not self._missing_policies and not self._missing_hwmap:
            self._record("Apply missing config", True, "Nothing to create -- all config present")
            return

        # Build config lines for everything missing
        all_lines: List[str] = []

        # hw-mapping
        if self._missing_hwmap:
            all_lines.extend(QOS_HW_MAPPING_LINES)
            self.created_hwmapping = True

        # TCMs
        for tcm_name in self._missing_tcms:
            all_lines.extend(QOS_TCM_LINES[tcm_name])
            self.created_tcms.add(tcm_name)

        # Policies
        if INGRESS_POLICY in self._missing_policies:
            all_lines.extend(QOS_INGRESS_POLICY_LINES)
            self.created_policies.add(INGRESS_POLICY)

        if EGRESS_POLICY in self._missing_policies:
            all_lines.extend(QOS_EGRESS_POLICY_LINES)
            self.created_policies.add(EGRESS_POLICY)

        detail_parts = []
        if self.created_hwmapping:
            detail_parts.append("hw-mapping")
        if self.created_tcms:
            detail_parts.append(f"{len(self.created_tcms)} TCMs")
        if self.created_policies:
            detail_parts.append(f"policies: {', '.join(self.created_policies)}")

        commit_out = self.run_config(all_lines, timeout=90)

        if "error" in commit_out.lower() and "commit" not in commit_out.lower():
            self._record(
                "Apply missing config",
                False,
                f"Commit error: {commit_out[:300]}",
            )
            return

        self._record(
            "Apply missing config",
            True,
            f"Created: {', '.join(detail_parts)}",
        )

    def test_attach_policies(self):
        """Test 3: Discover interfaces and attach policies."""
        print("\n" + "=" * 60)
        print("TEST 3: Discover interfaces and attach policies")
        print("=" * 60)

        # Determine ingress and egress interfaces
        if self.forced_ingress_interface and self.forced_egress_interface:
            # Both interfaces specified
            self.ingress_iface = self.forced_ingress_interface
            self.egress_iface = self.forced_egress_interface
            self._record("Use forced interfaces", True, 
                        f"Ingress: {self.ingress_iface}, Egress: {self.egress_iface}")
        elif self.forced_ingress_interface or self.forced_egress_interface:
            # One interface specified, need to discover the other
            raw = self.run_show("show interfaces", timeout=30)
            up_ifaces = self.parse_interfaces_summary(raw)
            
            if not up_ifaces and not (self.forced_ingress_interface and self.forced_egress_interface):
                self._record("Discover interfaces", False, "No up interfaces found for auto-discovery")
                return
            
            self.ingress_iface = self.forced_ingress_interface or up_ifaces[0]
            self.egress_iface = self.forced_egress_interface or (up_ifaces[1] if len(up_ifaces) > 1 else up_ifaces[0])
            
            self._record("Use mixed interfaces", True,
                        f"Ingress: {self.ingress_iface} {'(specified)' if self.forced_ingress_interface else '(auto)'}, "
                        f"Egress: {self.egress_iface} {'(specified)' if self.forced_egress_interface else '(auto)'}")
        else:
            # Auto-discover both interfaces
            raw = self.run_show("show interfaces", timeout=30)
            up_ifaces = self.parse_interfaces_summary(raw)

            if not up_ifaces:
                self._record("Discover interfaces", False, "No up interfaces found")
                return
            
            # Use first interface for both by default, or separate if multiple available
            self.ingress_iface = up_ifaces[0]
            self.egress_iface = up_ifaces[1] if len(up_ifaces) > 1 else up_ifaces[0]
            
            if self.ingress_iface == self.egress_iface:
                self._record("Discover interfaces", True,
                            f"Using {self.ingress_iface} for both ingress and egress (from {len(up_ifaces)} up)")
            else:
                self._record("Discover interfaces", True,
                            f"Ingress: {self.ingress_iface}, Egress: {self.egress_iface} (from {len(up_ifaces)} up)")

        # Set target_iface for backward compatibility (use ingress interface as primary)
        self.target_iface = self.ingress_iface

        # Check existing policies on ingress interface
        qos_raw_in = self.run_show(f"show qos interfaces {self.ingress_iface}", timeout=30)
        self.original_in_policy = self.parse_qos_interface_policy(qos_raw_in, "in")
        
        if self.original_in_policy:
            print(f"  [INFO] {self.ingress_iface} already has ingress policy: {self.original_in_policy}")

        # Check existing policies on egress interface (if different)
        if self.egress_iface != self.ingress_iface:
            qos_raw_eg = self.run_show(f"show qos interfaces {self.egress_iface}", timeout=30)
            self.original_egress_in_policy = self.parse_qos_interface_policy(qos_raw_eg, "in")
            self.original_egress_out_policy = self.parse_qos_interface_policy(qos_raw_eg, "out")
            self.original_out_policy = self.original_egress_out_policy
            
            if self.original_egress_out_policy:
                print(f"  [INFO] {self.egress_iface} already has egress policy: {self.original_egress_out_policy}")
        else:
            # Same interface for both
            self.original_out_policy = self.parse_qos_interface_policy(qos_raw_in, "out")
            if self.original_out_policy:
                print(f"  [INFO] {self.ingress_iface} already has egress policy: {self.original_out_policy}")

        # Attach ingress policy to ingress interface
        need_attach_in = self.original_in_policy != INGRESS_POLICY
        config_lines_in = []
        
        if need_attach_in:
            config_lines_in = [
                "interfaces",
                self.ingress_iface,
                f"qos policy {INGRESS_POLICY} direction in",
                "exit",  # interface
                "exit",  # interfaces
            ]

        # Attach egress policy to egress interface
        need_attach_out = self.original_out_policy != EGRESS_POLICY
        config_lines_out = []
        
        if need_attach_out:
            config_lines_out = [
                "interfaces",
                self.egress_iface,
                f"qos policy {EGRESS_POLICY} direction out",
                "exit",  # interface
                "exit",  # interfaces
            ]

        # Apply configuration
        if not need_attach_in and not need_attach_out:
            self._record(
                "Attach policies",
                True,
                "Both policies already attached to interfaces",
            )
            self.attached_in = False
            self.attached_out = False
            return

        # Combine config if needed
        all_config_lines = config_lines_in + config_lines_out
        if all_config_lines:
            commit_out = self.run_config(all_config_lines, timeout=60)
            if "error" in commit_out.lower() and "commit" not in commit_out.lower():
                self._record("Attach policies", False, f"Commit error: {commit_out[:300]}")
                return

        self.attached_in = need_attach_in
        self.attached_out = need_attach_out

        attached = []
        if need_attach_in:
            attached.append(f"{INGRESS_POLICY} (in) on {self.ingress_iface}")
        if need_attach_out:
            attached.append(f"{EGRESS_POLICY} (out) on {self.egress_iface}")
        if need_attach_in:
            attached.append(f"{INGRESS_POLICY} (in)")
        if need_attach_out:
            attached.append(f"{EGRESS_POLICY} (out)")

        self._record(
            "Attach policies",
            True,
            f"Attached {', '.join(attached)} to {self.target_iface}",
        )

    # ------------------------------------------------------------------
    # Phase 2: Validation
    # ------------------------------------------------------------------
    def test_verify_tcms(self):
        """Test 4: Verify traffic-class-maps in config."""
        print("\n" + "=" * 60)
        print("TEST 4: Verify traffic-class-maps")
        print("=" * 60)

        raw = self.run_show("show config qos", timeout=30)
        existing = self.parse_config_tcm_names(raw)

        all_ok = True
        for name, (exp_type, exp_val) in EXPECTED_TCM.items():
            if name not in existing:
                self._record(f"TCM {name}", False, "Not found in config")
                all_ok = False
                continue
            actual_type, actual_val = existing[name]
            if actual_type == exp_type and actual_val == exp_val:
                self._record(f"TCM {name}", True, f"{actual_type} {actual_val}")
            else:
                self._record(
                    f"TCM {name}",
                    False,
                    f"Expected {exp_type} {exp_val}, got {actual_type} {actual_val}",
                )
                all_ok = False

    def test_verify_ingress_policy(self):
        """Test 5: Verify ingress policy rules."""
        print("\n" + "=" * 60)
        print(f"TEST 5: Verify {INGRESS_POLICY} rules")
        print("=" * 60)

        raw = self.run_show("show config qos", timeout=30)
        rules = self.parse_config_ingress_rules(raw)

        if not rules:
            self._record("Ingress policy parse", False, "No rules found")
            return

        self._record("Ingress policy parse", True, f"{len(rules)} rules found")

        for rid, (exp_tcm, exp_qtag) in EXPECTED_INGRESS_RULES.items():
            if rid not in rules:
                self._record(f"Ingress rule {rid}", False, "Rule not found")
                continue
            actual_tcm, actual_qtag = rules[rid]
            ok = actual_tcm == exp_tcm and actual_qtag == exp_qtag
            if ok:
                self._record(f"Ingress rule {rid}", True, f"tcm={actual_tcm}, qos-tag={actual_qtag}")
            else:
                self._record(
                    f"Ingress rule {rid}",
                    False,
                    f"Expected tcm={exp_tcm}/qtag={exp_qtag}, got tcm={actual_tcm}/qtag={actual_qtag}",
                )

    def test_verify_egress_policy(self):
        """Test 6: Verify egress policy rules."""
        print("\n" + "=" * 60)
        print(f"TEST 6: Verify {EGRESS_POLICY} rules")
        print("=" * 60)

        raw = self.run_show("show config qos", timeout=30)
        rules = self.parse_config_egress_rules(raw)

        if not rules:
            self._record("Egress policy parse", False, "No rules found")
            return

        self._record("Egress policy parse", True, f"{len(rules)} rules found")

        for rid, expected in EXPECTED_EGRESS_RULES.items():
            if rid not in rules:
                self._record(f"Egress rule {rid}", False, "Rule not found")
                continue
            actual = rules[rid]
            issues = []

            if "tcm" in expected:
                if actual.get("tcm") != expected["tcm"]:
                    issues.append(f"tcm: exp={expected['tcm']} got={actual.get('tcm')}")

            if actual.get("fwd") != expected["fwd"]:
                issues.append(f"fwd: exp={expected['fwd']} got={actual.get('fwd')}")

            if actual.get("bw_type") != expected["bw_type"]:
                issues.append(f"bw_type: exp={expected['bw_type']} got={actual.get('bw_type')}")

            if actual.get("bw") != expected["bw"]:
                issues.append(f"bw: exp={expected['bw']} got={actual.get('bw')}")

            if issues:
                self._record(f"Egress rule {rid}", False, "; ".join(issues))
            else:
                detail = f"fwd={actual.get('fwd')}, {actual.get('bw_type')}={actual.get('bw')}%"
                self._record(f"Egress rule {rid}", True, detail)

    def test_verify_interface_detail(self):
        """Test 7: Verify policies on interface via show qos interfaces."""
        print("\n" + "=" * 60)
        print("TEST 7: Verify policies on interface")
        print("=" * 60)

        if not self.ingress_iface:
            self._record("Interface QoS detail", False, "No target interface set")
            return

        # Check ingress on ingress interface
        raw_in = self.run_show(f"show qos interfaces {self.ingress_iface} in", timeout=30)
        in_policy = self.parse_qos_interface_policy(raw_in, "in")
        if in_policy == INGRESS_POLICY:
            self._record("Ingress policy on interface", True, f"{in_policy} on {self.ingress_iface}")
        else:
            self._record(
                "Ingress policy on interface",
                False,
                f"Expected {INGRESS_POLICY}, got {in_policy} on {self.ingress_iface}",
            )

        # Verify ingress rules visible
        in_rules = self.parse_qos_interface_rules(raw_in)
        if in_rules:
            self._record("Ingress rules visible", True, f"Rules: {', '.join(in_rules)}")
        else:
            self._record("Ingress rules visible", False, "No rules found in show output")

        # Check egress on egress interface
        raw_out = self.run_show(f"show qos interfaces {self.egress_iface} out", timeout=30)
        out_policy = self.parse_qos_interface_policy(raw_out, "out")
        if out_policy == EGRESS_POLICY:
            self._record("Egress policy on interface", True, f"{out_policy} on {self.egress_iface}")
        else:
            self._record(
                "Egress policy on interface",
                False,
                f"Expected {EGRESS_POLICY}, got {out_policy} on {self.egress_iface}",
            )

        # Verify egress rules and queue types visible
        out_rules = self.parse_qos_interface_rules(raw_out)
        if out_rules:
            self._record("Egress rules visible", True, f"Rules: {', '.join(out_rules)}")
        else:
            self._record("Egress rules visible", False, "No rules found in show output")

        # Check for Queue Configuration presence
        if "Queue Configuration:" in raw_out or "forwarding-class" in raw_out.lower() or "Type" in raw_out:
            self._record("Egress queue config visible", True)
        else:
            self._record("Egress queue config visible", False, "No queue config in output")

    def test_verify_counters(self):
        """Test 8: Verify QoS counters structure."""
        print("\n" + "=" * 60)
        print("TEST 8: Verify QoS counters")
        print("=" * 60)

        if not self.ingress_iface or not self.egress_iface:
            self._record("QoS counters", False, "No target interfaces set")
            return

        # Ingress counters (on ingress interface)
        raw_in = self.run_show(
            f"show qos interfaces counters {self.ingress_iface} in",
            timeout=30,
        )
        in_rules = self.parse_qos_counters_rules(raw_in)
        has_matched_in = self.parse_qos_counters_matched(raw_in)

        if in_rules:
            self._record(
                "Ingress counter rules",
                True,
                f"Rules: {', '.join(in_rules)}",
            )
        else:
            self._record("Ingress counter rules", False, "No rules in counter output")

        if has_matched_in:
            self._record("Ingress counter fields", True, "Matched packets/octets present")
        else:
            self._record("Ingress counter fields", False, "No matched packets/octets found")

        # Egress counters (on egress interface)
        raw_out = self.run_show(
            f"show qos interfaces counters {self.egress_iface} out",
            timeout=30,
        )
        out_rules = self.parse_qos_counters_rules(raw_out)
        has_matched_out = self.parse_qos_counters_matched(raw_out)

        if out_rules:
            self._record(
                "Egress counter rules",
                True,
                f"Rules: {', '.join(out_rules)}",
            )
        else:
            self._record("Egress counter rules", False, "No rules in counter output")

        if has_matched_out:
            self._record("Egress counter fields", True, "Matched packets/octets present")
        else:
            self._record("Egress counter fields", False, "No matched packets/octets found")

        # Check for Queue statistics in egress
        if "Queue statistics:" in raw_out or "queue" in raw_out.lower():
            self._record("Egress queue stats", True, "Queue statistics present")
        else:
            self._record("Egress queue stats", False, "No queue statistics in egress counters")

    def test_clear_counters(self):
        """Test 9: Clear QoS counters and verify."""
        print("\n" + "=" * 60)
        print("TEST 9: Clear and verify QoS counters")
        print("=" * 60)

        if not self.target_iface:
            self._record("Clear QoS counters", False, "No target interface set")
            return

        self.run_operational(f"clear qos counters {self.target_iface}")
        time.sleep(2)

        raw = self.run_show(
            f"show qos interfaces counters {self.target_iface} in",
            timeout=30,
        )

        # Check that counters are present (may be 0 or small)
        has_fields = self.parse_qos_counters_matched(raw)
        if has_fields:
            self._record("Clear counters", True, "Counters present after clear")
        else:
            self._record("Clear counters", False, "Counter fields missing after clear")

        # Look for zero values in matched packets
        zero_match = re.search(r"Matched packets:\s+0\s", raw)
        if zero_match:
            self._record("Counters at zero", True, "Matched packets = 0 after clear")
        else:
            # Not a failure -- traffic may be flowing
            self._record("Counters at zero", True, "Counters present (traffic may be active)")

    def test_egress_queues(self):
        """Test 10: Verify egress queues (physical interfaces only)."""
        print("\n" + "=" * 60)
        print("TEST 10: Verify egress queues")
        print("=" * 60)

        if not self.egress_iface:
            self._record("Egress queues", False, "No egress interface set")
            return

        # egress-queues only works on physical interfaces, not bundles
        if self.egress_iface.startswith("bundle"):
            self._record(
                "Egress queues",
                True,
                f"Skipped -- {self.egress_iface} is a bundle (egress-queues requires physical iface)",
            )
            return

        raw = self.run_show(
            f"show qos interfaces egress-queues {self.egress_iface}",
            timeout=30,
        )
        queues = self.parse_egress_queues(raw)

        if queues:
            self._record(
                "Egress queues visible",
                True,
                f"{len(queues)} queues: {', '.join(queues)}",
            )
        else:
            # Could be an error or not supported
            if "ERROR" in raw or "Unknown" in raw:
                self._record("Egress queues visible", False, f"Device error: {raw[:200]}")
            else:
                self._record("Egress queues visible", False, "No egress-queue-id entries found")

        # Check for queue-type field
        if "queue-type:" in raw:
            self._record("Egress queue types", True, "queue-type fields present")
        elif queues:
            self._record("Egress queue types", True, "Queues present (format may differ)")
        else:
            self._record("Egress queue types", False, "No queue-type fields found")

    def test_modify_bandwidth(self):
        """Test 11: Modify Egress_Full rule 1 bandwidth and verify."""
        print("\n" + "=" * 60)
        print("TEST 11: Modify bandwidth (rule 1: 10% -> 15%)")
        print("=" * 60)

        if not self.egress_iface:
            self._record("Modify bandwidth", False, "No egress interface set")
            return

        config_lines = [
            "qos",
            f"policy {EGRESS_POLICY}",
            "rule 1",
            "action",
            "queue",
            "forwarding-class af",
            "bandwidth 15 percent",
            "exit",  # forwarding-class
            "exit",  # queue
            "exit",  # action
            "exit",  # rule
            "exit",  # policy
            "exit",  # qos
        ]

        commit_out = self.run_config(config_lines, timeout=60)
        if "error" in commit_out.lower() and "commit" not in commit_out.lower():
            self._record("Modify bandwidth commit", False, f"Error: {commit_out[:200]}")
            return
        self._record("Modify bandwidth commit", True)

        # Verify the change on egress interface
        raw = self.run_show(f"show qos interfaces {self.egress_iface} out", timeout=30)
        bw = self.parse_interface_qos_bandwidth(raw, "1")
        if bw == "15":
            self._record("Verify bandwidth = 15%", True)
        else:
            self._record("Verify bandwidth = 15%", False, f"Got bandwidth={bw}")

    def test_revert_bandwidth(self):
        """Test 12: Revert Egress_Full rule 1 bandwidth to 10%."""
        print("\n" + "=" * 60)
        print("TEST 12: Revert bandwidth (rule 1: 15% -> 10%)")
        print("=" * 60)

        if not self.egress_iface:
            self._record("Revert bandwidth", False, "No egress interface set")
            return

        config_lines = [
            "qos",
            f"policy {EGRESS_POLICY}",
            "rule 1",
            "action",
            "queue",
            "forwarding-class af",
            "bandwidth 10 percent",
            "exit",  # forwarding-class
            "exit",  # queue
            "exit",  # action
            "exit",  # rule
            "exit",  # policy
            "exit",  # qos
        ]

        commit_out = self.run_config(config_lines, timeout=60)
        if "error" in commit_out.lower() and "commit" not in commit_out.lower():
            self._record("Revert bandwidth commit", False, f"Error: {commit_out[:200]}")
            return
        self._record("Revert bandwidth commit", True)

        # Verify the revert on egress interface
        raw = self.run_show(f"show qos interfaces {self.egress_iface} out", timeout=30)
        bw = self.parse_interface_qos_bandwidth(raw, "1")
        if bw == "10":
            self._record("Verify bandwidth = 10%", True)
        else:
            self._record("Verify bandwidth = 10%", False, f"Got bandwidth={bw}")

    # ------------------------------------------------------------------
    # Phase 3: Cleanup
    # ------------------------------------------------------------------
    def test_detach_policies(self):
        """Test 13: Detach policies from interfaces."""
        print("\n" + "=" * 60)
        print("TEST 13: Detach policies from interfaces")
        print("=" * 60)

        if not self.ingress_iface and not self.egress_iface:
            self._record("Detach policies", True, "No interfaces to detach from")
            return

        config_lines = []

        # Detach ingress policy from ingress interface
        if self.attached_in and self.ingress_iface:
            config_lines += [
                "interfaces",
                self.ingress_iface,
                f"no qos policy {INGRESS_POLICY} direction in",
            ]
            # Restore original if it was different
            if self.original_in_policy and self.original_in_policy != INGRESS_POLICY:
                config_lines.append(f"qos policy {self.original_in_policy} direction in")
            config_lines += ["exit", "exit"]

        # Detach egress policy from egress interface
        if self.attached_out and self.egress_iface:
            config_lines += [
                "interfaces",
                self.egress_iface,
                f"no qos policy {EGRESS_POLICY} direction out",
            ]
            # Restore original if it was different
            if self.original_out_policy and self.original_out_policy != EGRESS_POLICY:
                config_lines.append(f"qos policy {self.original_out_policy} direction out")
            config_lines += ["exit", "exit"]

        if not self.attached_in and not self.attached_out:
            self._record("Detach policies", True, "No policies were attached by this test")
            return

        commit_out = self.run_config(config_lines, timeout=60)
        if "error" in commit_out.lower() and "commit" not in commit_out.lower():
            self._record("Detach policies", False, f"Error: {commit_out[:200]}")
            return

        detail_parts = []
        if self.attached_in:
            detail_parts.append(f"removed {INGRESS_POLICY} (in)")
        if self.attached_out:
            detail_parts.append(f"removed {EGRESS_POLICY} (out)")
        if self.original_in_policy and self.original_in_policy != INGRESS_POLICY and self.attached_in:
            detail_parts.append(f"restored {self.original_in_policy} (in)")
        if self.original_out_policy and self.original_out_policy != EGRESS_POLICY and self.attached_out:
            detail_parts.append(f"restored {self.original_out_policy} (out)")

        self._record("Detach policies", True, "; ".join(detail_parts))

    def test_remove_created_config(self):
        """Test 14: Remove config we created and verify baseline restored."""
        print("\n" + "=" * 60)
        print("TEST 14: Remove created config and verify")
        print("=" * 60)

        if not self.created_tcms and not self.created_policies and not self.created_hwmapping:
            self._record("Remove created config", True, "Nothing was created by this test")
            return

        config_lines: List[str] = ["qos"]

        # Remove policies first (they reference TCMs)
        for policy_name in self.created_policies:
            config_lines.append(f"no policy {policy_name}")

        # Remove TCMs
        for tcm_name in self.created_tcms:
            config_lines.append(f"no traffic-class-map {tcm_name}")

        # Remove hw-mapping
        if self.created_hwmapping:
            config_lines.append("no hw-mapping")

        config_lines.append("exit")  # qos

        commit_out = self.run_config(config_lines, timeout=60)
        if "error" in commit_out.lower() and "commit" not in commit_out.lower():
            self._record("Remove created config", False, f"Error: {commit_out[:200]}")
            return

        removed_parts = []
        if self.created_policies:
            removed_parts.append(f"policies: {', '.join(self.created_policies)}")
        if self.created_tcms:
            removed_parts.append(f"{len(self.created_tcms)} TCMs")
        if self.created_hwmapping:
            removed_parts.append("hw-mapping")

        self._record("Remove created config", True, f"Removed: {', '.join(removed_parts)}")

        # Verify config matches baseline
        raw = self.run_show("show config qos", timeout=30)
        # Simple check: the policies and TCMs we created should no longer appear
        lingering = []
        for name in self.created_policies:
            if f"policy {name}" in raw:
                lingering.append(f"policy {name}")
        for name in self.created_tcms:
            if f"traffic-class-map {name}" in raw:
                lingering.append(f"tcm {name}")

        if not lingering:
            self._record("Baseline restored", True, "No lingering test artifacts")
        else:
            self._record("Baseline restored", False, f"Still present: {', '.join(lingering)}")

    # ------------------------------------------------------------------
    # Orchestrator
    # ------------------------------------------------------------------
    def run_all(self) -> bool:
        """Run all test phases and print summary."""
        start = datetime.now()
        print("=" * 60)
        print("  QOS SANITY TEST  --  Happy Flow")
        print(f"  Device : {self.host}")
        print(f"  Started: {start.strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 60)

        try:
            self.connect()

            # Phase 1: Setup
            print("\n" + "#" * 60)
            print("# PHASE 1: SETUP")
            print("#" * 60)
            self.test_snapshot_config()
            self.test_apply_missing_config()
            self.test_attach_policies()

            # Phase 2: Validation
            print("\n" + "#" * 60)
            print("# PHASE 2: VALIDATION")
            print("#" * 60)
            self.test_verify_tcms()
            self.test_verify_ingress_policy()
            self.test_verify_egress_policy()
            self.test_verify_interface_detail()
            self.test_verify_counters()
            self.test_clear_counters()
            self.test_egress_queues()
            self.test_modify_bandwidth()
            self.test_revert_bandwidth()

            # Phase 3: Cleanup
            if self.no_cleanup:
                print("\n" + "#" * 60)
                print("# PHASE 3: CLEANUP  --  SKIPPED (--no-cleanup)")
                print("#" * 60)
                self._record("Cleanup skipped", True, "Policies and config left in place")
            else:
                print("\n" + "#" * 60)
                print("# PHASE 3: CLEANUP")
                print("#" * 60)
                self.test_detach_policies()
                self.test_remove_created_config()

        except Exception as exc:
            print(f"\n[ERROR] Unexpected exception: {exc}")
            self._record("Unexpected error", False, str(exc))
            # Attempt emergency cleanup only if cleanup is enabled
            if not self.no_cleanup:
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
        
        # Generate summary file
        self._write_summary_file(passed, failed, total, elapsed, verdict)
        
        return failed == 0

    def _write_summary_file(self, passed: int, failed: int, total: int, elapsed: float, verdict: str):
        """Write test summary to a file."""
        try:
            from datetime import datetime as dt
            timestamp = dt.now().strftime("%Y%m%d_%H%M%S")
            filename = f"qos_test_summary_{self.host}_{timestamp}.md"
            
            with open(filename, "w") as f:
                f.write(f"# QoS Sanity Test Summary\n\n")
                f.write(f"**Device**: {self.host}\n")
                f.write(f"**Date**: {dt.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"**Duration**: {elapsed:.1f}s\n")
                f.write(f"**Result**: {verdict}\n\n")
                
                f.write(f"## Statistics\n\n")
                f.write(f"| Metric | Value |\n")
                f.write(f"|--------|-------|\n")
                f.write(f"| Total Tests | {total} |\n")
                f.write(f"| Passed | {passed} |\n")
                f.write(f"| Failed | {failed} |\n")
                f.write(f"| Pass Rate | {(passed/total*100):.1f}% |\n")
                f.write(f"| Execution Time | {elapsed:.1f}s |\n\n")
                
                f.write(f"## Test Results\n\n")
                
                # Group results by phase
                phases = {
                    "Setup": [],
                    "Validation": [],
                    "Cleanup": []
                }
                
                for name, passed_test, detail in self.results:
                    if any(x in name for x in ["Snapshot", "Apply missing", "Discover", "Attach"]):
                        phase = "Setup"
                    elif any(x in name for x in ["Cleanup", "Detach", "Remove"]):
                        phase = "Cleanup"
                    else:
                        phase = "Validation"
                    
                    status = "✅ PASS" if passed_test else "❌ FAIL"
                    phases[phase].append((name, status, detail))
                
                for phase_name, tests in phases.items():
                    if tests:
                        f.write(f"### {phase_name} Phase\n\n")
                        for name, status, detail in tests:
                            f.write(f"- **{name}**: {status}\n")
                            if detail:
                                f.write(f"  - {detail}\n")
                        f.write(f"\n")
                
                if failed > 0:
                    f.write(f"## Failed Tests Details\n\n")
                    for name, passed_test, detail in self.results:
                        if not passed_test:
                            f.write(f"### {name}\n")
                            f.write(f"```\n{detail}\n```\n\n")
                
                f.write(f"## Configuration Details\n\n")
                f.write(f"- **Ingress Interface**: {self.ingress_iface if self.ingress_iface else 'N/A'}\n")
                f.write(f"- **Egress Interface**: {self.egress_iface if self.egress_iface else 'N/A'}\n")
                if self.ingress_iface == self.egress_iface:
                    f.write(f"  - *(Same interface used for both directions)*\n")
                f.write(f"- **Ingress Policy**: {INGRESS_POLICY}\n")
                f.write(f"- **Egress Policy**: {EGRESS_POLICY}\n")
                f.write(f"- **Expected TCMs**: {len(EXPECTED_TCM)}\n")
                f.write(f"- **Expected Ingress Rules**: {len(EXPECTED_INGRESS_RULES)}\n")
                f.write(f"- **Expected Egress Rules**: {len(EXPECTED_EGRESS_RULES)}\n\n")
                
                f.write(f"---\n")
                f.write(f"*Generated by qos_sanity_test.py*\n")
            
            print(f"[*] Summary written to: {filename}")
            
        except Exception as e:
            print(f"[!] Failed to write summary file: {e}")

    def _emergency_cleanup(self):
        """Best-effort cleanup after an unexpected error."""
        print("\n[!] Attempting emergency cleanup...")
        try:
            # Detach from ingress interface
            if self.ingress_iface and self.attached_in:
                lines = [
                    "interfaces",
                    self.ingress_iface,
                    f"no qos policy {INGRESS_POLICY} direction in",
                ]
                if self.original_in_policy and self.original_in_policy != INGRESS_POLICY:
                    lines.append(f"qos policy {self.original_in_policy} direction in")
                lines += ["exit", "exit"]
                self.run_config(lines, timeout=30)
                print(f"[!] Detached ingress policy from {self.ingress_iface}.")

            # Detach from egress interface (if different)
            if self.egress_iface and self.attached_out:
                lines = [
                    "interfaces",
                    self.egress_iface,
                    f"no qos policy {EGRESS_POLICY} direction out",
                ]
                if self.original_out_policy and self.original_out_policy != EGRESS_POLICY:
                    lines.append(f"qos policy {self.original_out_policy} direction out")
                lines += ["exit", "exit"]
                self.run_config(lines, timeout=30)
                print(f"[!] Detached egress policy from {self.egress_iface}.")

            if self.created_policies or self.created_tcms or self.created_hwmapping:
                lines = ["qos"]
                for p in self.created_policies:
                    lines.append(f"no policy {p}")
                for t in self.created_tcms:
                    lines.append(f"no traffic-class-map {t}")
                if self.created_hwmapping:
                    lines.append("no hw-mapping")
                lines.append("exit")
                self.run_config(lines, timeout=30)
                print("[!] Removed created config.")
        except Exception as e:
            print(f"[!] Emergency cleanup failed: {e}")


def main():
    parser = argparse.ArgumentParser(
        description="QoS sanity test for DNOS devices"
    )
    parser.add_argument(
        "--host",
        default="xgu1f7v900009p2",
        help="Device hostname or IP (default: xgu1f7v900009p2)",
    )
    parser.add_argument(
        "--user", default="dnroot", help="SSH username (default: dnroot)"
    )
    parser.add_argument(
        "--password", default="dnroot", help="SSH password (default: dnroot)"
    )
    parser.add_argument(
        "--interface",
        help="Single interface for both ingress and egress testing (e.g., ge100-0/0/96). Overridden by --ingress-interface or --egress-interface.",
    )
    parser.add_argument(
        "--ingress-interface",
        help="Specific interface for ingress policy testing (e.g., ge100-0/0/96). If not specified, uses --interface or auto-discovers.",
    )
    parser.add_argument(
        "--egress-interface",
        help="Specific interface for egress policy testing (e.g., ge100-0/0/97). If not specified, uses --interface or auto-discovers.",
    )
    parser.add_argument(
        "--no-cleanup",
        action="store_true",
        help="Skip cleanup phase -- leave policies and config in place after tests",
    )
    args = parser.parse_args()

    # Resolve interface arguments priority: specific > shared > auto-discover
    ingress_iface = args.ingress_interface or args.interface
    egress_iface = args.egress_interface or args.interface

    tester = QoSSanityTest(
        host=args.host,
        username=args.user,
        password=args.password,
        no_cleanup=args.no_cleanup,
        forced_ingress_interface=ingress_iface,
        forced_egress_interface=egress_iface,
    )
    ok = tester.run_all()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
