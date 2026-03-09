#!/usr/bin/env python3
"""
GI Day Validation Test Script for DNOS

Automates the post-GI (Golden Image) day validations as defined in:
  https://drivenets.atlassian.net/wiki/spaces/QA/pages/4115303055

Sections:
  1. FW Validations        -- verify firmware versions
  2. Install Verification  -- show system install tasks, patches
  3. Show Commands          -- run and validate 14+ show commands
  4. HA Validations         -- SO/FO recovery, cold/warm restarts (opt-in)
  5. Config Validations     -- diff check, load override + rollback, commit test
  6. System Resources       -- CPU and memory usage sanity
  7. AI / J2C+ Validations  -- ICE interfaces, BGP, syncer connectivity

Usage:
    python3 gi_day_validation_test.py --host <device>
    python3 gi_day_validation_test.py --host <device> --include-ha
    python3 gi_day_validation_test.py --host <device> --skip-config-test
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import paramiko

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class GIDayValidationTest:
    """GI day post-deployment validation tester for DNOS devices."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        include_ha: bool = False,
        skip_config_test: bool = False,
        expected_version: Optional[str] = None,
        timeout: int = 30,
    ):
        self.host = host
        self.username = username
        self.password = password
        self.include_ha = include_ha
        self.skip_config_test = skip_config_test
        self.expected_version = expected_version
        self.timeout = timeout

        self.client: Optional[paramiko.SSHClient] = None
        self.shell: Optional[paramiko.Channel] = None
        self.results: List[Tuple[str, bool, str]] = []

        # Saved outputs for cross-referencing
        self.saved_outputs: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # SSH helpers
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
            timeout=self.timeout,
        )
        self.shell = self.client.invoke_shell(width=250, height=1000)
        self._read_until_prompt(timeout=15)
        self._send("no-paging")
        self._read_until_prompt(timeout=5)
        print("[+] Connected and paging disabled.\n")

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
        print("\n[*] Disconnected.")

    def reconnect(self):
        """Re-establish SSH after a disruptive operation (HA, restart)."""
        self.disconnect()
        max_wait = 600
        poll_interval = 15
        start = time.time()
        print(f"[*] Waiting for {self.host} to become reachable (up to {max_wait}s)...")
        while time.time() - start < max_wait:
            time.sleep(poll_interval)
            try:
                self.connect()
                print(f"[+] Reconnected after {time.time() - start:.0f}s.")
                return True
            except Exception:
                elapsed = time.time() - start
                print(f"  [INFO] Not reachable yet ({elapsed:.0f}s elapsed)...")
        return False

    def _send(self, cmd: str):
        self.shell.send(cmd + "\n")

    def _read_until_prompt(self, timeout: int = 30) -> str:
        buf = ""
        end_time = time.time() + timeout
        while time.time() < end_time:
            if self.shell.recv_ready():
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
        self._send(cmd)
        output = self._read_until_prompt(timeout=timeout)
        return output

    def run_config(self, config_lines: List[str], timeout: int = 60) -> str:
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

    def run_operational(self, cmd: str, timeout: int = 30) -> str:
        self._send(cmd)
        output = self._read_until_prompt(timeout=timeout)
        return output

    def run_operational_with_confirm(self, cmd: str, confirm: str = "yes", timeout: int = 30) -> str:
        self._send(cmd)
        output = self._read_until_prompt(timeout=timeout)
        if "yes/no" in output.lower() or "[yes,no]" in output.lower():
            self._send(confirm)
            output += self._read_until_prompt(timeout=timeout)
        return output

    # ------------------------------------------------------------------
    # Result recording
    # ------------------------------------------------------------------
    def _record(self, name: str, passed: bool, detail: str = ""):
        self.results.append((name, passed, detail))
        tag = "[PASS]" if passed else "[FAIL]"
        print(f"  {tag} {name}" + (f" -- {detail}" if detail else ""))

    @staticmethod
    def _has_error(output: str) -> bool:
        lower = output.lower()
        return any(
            pat in lower
            for pat in [
                "error:",
                "unknown command",
                "invalid",
                "commit check failed",
                "commit failed",
                "validation failed",
                "aborted",
                "syntax error",
            ]
        )

    # ------------------------------------------------------------------
    # Parsers
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_table_rows(output: str) -> List[Dict[str, str]]:
        """Generic pipe-delimited table parser. Returns list of row dicts."""
        lines = output.splitlines()
        header = None
        rows = []
        for line in lines:
            stripped = line.strip()
            if not stripped or set(stripped) <= {"+", "-", "|", " ", "="}:
                continue
            if "|" not in stripped:
                continue
            parts = [p.strip() for p in stripped.split("|") if p.strip()]
            if not parts:
                continue
            if header is None:
                if any(
                    kw in stripped.lower()
                    for kw in ["name", "type", "serial", "state", "status", "interface", "version"]
                ):
                    header = parts
                    continue
            if header and len(parts) == len(header):
                rows.append(dict(zip(header, parts)))
            elif header and len(parts) >= 2:
                row = {}
                for i, val in enumerate(parts):
                    key = header[i] if i < len(header) else f"col{i}"
                    row[key] = val
                rows.append(row)
        return rows

    @staticmethod
    def _count_keyword(output: str, keyword: str) -> int:
        return len(re.findall(re.escape(keyword), output, re.IGNORECASE))

    # ==================================================================
    # SECTION 1: FW Validations
    # ==================================================================
    def test_fw_versions(self):
        """Verify firmware versions via show sys version detail."""
        print("\n" + "=" * 60)
        print("SECTION 1: FW Validations")
        print("=" * 60)

        raw = self.run_show("show sys version detail", timeout=30)
        self.saved_outputs["version_detail"] = raw

        if "error" in raw.lower()[:200] and "version" not in raw.lower():
            self._record("FW version output", False, "Command returned error")
            return

        self._record("FW version output", True, "Command executed successfully")

        # Check if expected version is present
        if self.expected_version:
            if self.expected_version.lower() in raw.lower():
                self._record(
                    "Expected FW version",
                    True,
                    f"Found expected version: {self.expected_version}",
                )
            else:
                self._record(
                    "Expected FW version",
                    False,
                    f"Expected '{self.expected_version}' not found in output",
                )

        # Extract and display version info
        version_lines = []
        for line in raw.splitlines():
            stripped = line.strip()
            lower = stripped.lower()
            if any(
                kw in lower
                for kw in ["version", "build", "image", "firmware", "bios", "cpld", "fpga", "onie"]
            ):
                if stripped and not stripped.startswith("show "):
                    version_lines.append(stripped)
        if version_lines:
            detail = "; ".join(version_lines[:10])
            self._record("FW version details", True, detail[:300])
        else:
            self._record(
                "FW version details",
                False,
                "Could not parse version lines from output",
            )

    # ==================================================================
    # SECTION 2: Install Verification
    # ==================================================================
    def test_install_verification(self):
        """Verify system install tasks and patches."""
        print("\n" + "=" * 60)
        print("SECTION 2: Install Verification")
        print("=" * 60)

        # show system install
        raw_install = self.run_show("show system install", timeout=30)
        self.saved_outputs["system_install"] = raw_install

        if "error" in raw_install.lower()[:200]:
            self._record("show system install", False, "Command returned error")
        else:
            self._record("show system install", True, "Command executed")

            # Check for expected installation task types
            expected_tasks = [
                ("GI installation", "gi"),
                ("FW installation", "fw"),
                ("BaseOS installation", "baseos"),
                ("ONIE installation", "onie"),
                ("DNOS installation", "dnos"),
            ]
            for task_name, keyword in expected_tasks:
                if keyword in raw_install.lower():
                    self._record(f"Install task: {task_name}", True, "Found in install history")
                else:
                    self._record(
                        f"Install task: {task_name}",
                        False,
                        "Not found in install history (may be expected)",
                    )

            # Check for failed tasks
            fail_count = self._count_keyword(raw_install, "failed")
            error_count = self._count_keyword(raw_install, "error")
            if fail_count == 0 and error_count == 0:
                self._record("No failed install tasks", True, "No failures detected")
            else:
                self._record(
                    "No failed install tasks",
                    False,
                    f"Found {fail_count} 'failed' and {error_count} 'error' mentions",
                )

        # show system patches
        raw_patches = self.run_show("show system patches", timeout=30)
        self.saved_outputs["system_patches"] = raw_patches

        if "error" in raw_patches.lower()[:200] and "patch" not in raw_patches.lower():
            self._record("show system patches", False, "Command returned error")
        else:
            self._record("show system patches", True, "Command executed")

    # ==================================================================
    # SECTION 3: Show Commands
    # ==================================================================
    def test_show_commands(self):
        """Run the set of show commands and validate basic health."""
        print("\n" + "=" * 60)
        print("SECTION 3: Show Commands Validation")
        print("=" * 60)

        # --- 3.1 show system ---
        self._test_show_system()
        # --- 3.2 show file all core list ---
        self._test_show_core_files()
        # --- 3.3-3.6 show interfaces (various) ---
        self._test_show_interfaces()
        # --- 3.7-3.8 show interface counters ---
        self._test_show_interface_counters()
        # --- 3.9 show bfd sessions ---
        self._test_show_bfd_sessions()
        # --- 3.10 show qos summary ---
        self._test_show_qos_summary()
        # --- 3.11-3.12 show route summary / forwarding ---
        self._test_show_route()
        # --- 3.13 show sys stack detail ---
        self._test_show_stack_detail()
        # --- 3.14 show system details ---
        self._test_show_system_details()
        # --- 3.15 show system hardware ---
        self._test_show_system_hardware()

    def _test_show_system(self):
        """3.1: show system - verify all NCPs/components are up."""
        print("\n  -- show system --")
        raw = self.run_show("show system", timeout=30)
        self.saved_outputs["show_system"] = raw

        if "error" in raw.lower()[:200]:
            self._record("show system", False, "Command returned error")
            return

        # Count UP vs DOWN components
        up_count = self._count_keyword(raw, "| UP")
        down_count = self._count_keyword(raw, "| DOWN")
        not_present = self._count_keyword(raw, "Not Present")

        self._record(
            "show system",
            True,
            f"UP: {up_count}, DOWN: {down_count}, Not Present: {not_present}",
        )

        if down_count > 0:
            self._record(
                "All components UP",
                False,
                f"{down_count} component(s) in DOWN state",
            )
        else:
            self._record("All components UP", True, "No DOWN components")

    def _test_show_core_files(self):
        """3.2: show file all core list - check for crash core dumps."""
        print("\n  -- show file all core list --")
        raw = self.run_show("show file all core list", timeout=30)
        self.saved_outputs["core_list"] = raw

        if "error" in raw.lower()[:200] and "core" not in raw.lower():
            self._record("show file all core list", False, "Command returned error")
            return

        # Check if any core files exist
        core_lines = []
        for line in raw.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("show ") and ".core" in stripped.lower():
                core_lines.append(stripped)

        if not core_lines:
            self._record("Core files check", True, "No core dump files found")
        else:
            self._record(
                "Core files check",
                False,
                f"Found {len(core_lines)} core file(s) - investigate crashes",
            )

    def _test_show_interfaces(self):
        """3.3-3.6: show interfaces, fabric, control, backplane."""
        interface_cmds = [
            ("show interfaces", "interfaces"),
            ("show interfaces fabric", "interfaces_fabric"),
            ("show interfaces control", "interfaces_control"),
            ("show system backplane", "system_backplane"),
        ]
        for cmd, key in interface_cmds:
            print(f"\n  -- {cmd} --")
            raw = self.run_show(cmd, timeout=30)
            self.saved_outputs[key] = raw

            if "error" in raw.lower()[:200] and "interface" not in raw.lower() and "backplane" not in raw.lower():
                self._record(cmd, False, "Command returned error")
                continue

            self._record(cmd, True, "Command executed")

            # Check for DOWN interfaces (informational)
            down_count = self._count_keyword(raw, "Down")
            admin_down = self._count_keyword(raw, "Admin Down")
            up_count = self._count_keyword(raw, "| Up")
            if "interface" in key:
                self._record(
                    f"{cmd} status",
                    True,
                    f"Up: {up_count}, Down: {down_count}, Admin Down: {admin_down}",
                )

    def _test_show_interface_counters(self):
        """3.7-3.8: show interface counters, fabric counters."""
        counter_cmds = [
            "show interface counters",
            "show interface fabric counters",
        ]
        for cmd in counter_cmds:
            print(f"\n  -- {cmd} --")
            raw = self.run_show(cmd, timeout=30)
            self.saved_outputs[cmd.replace(" ", "_")] = raw

            if "error" in raw.lower()[:200]:
                self._record(cmd, False, "Command returned error")
                continue

            # Check for significant error counters
            error_fields = ["CRC", "Errors", "Drops", "Discards"]
            has_errors = False
            error_detail = []
            for line in raw.splitlines():
                for field in error_fields:
                    if field.lower() in line.lower():
                        # Try to extract numeric values
                        nums = re.findall(r"\d+", line)
                        non_zero = [n for n in nums if int(n) > 0]
                        if non_zero and field.lower() in line.lower():
                            has_errors = True
                            error_detail.append(line.strip()[:100])

            self._record(cmd, True, "Command executed")
            if has_errors:
                self._record(
                    f"{cmd} errors",
                    False,
                    f"Non-zero error counters detected: {'; '.join(error_detail[:3])}",
                )

    def _test_show_bfd_sessions(self):
        """3.9: show bfd sessions."""
        print("\n  -- show bfd sessions --")
        raw = self.run_show("show bfd sessions", timeout=30)
        self.saved_outputs["bfd_sessions"] = raw

        if "error" in raw.lower()[:200]:
            self._record("show bfd sessions", False, "Command returned error")
            return

        # Count sessions by state
        up_count = self._count_keyword(raw, "| Up")
        down_count = self._count_keyword(raw, "| Down")
        init_count = self._count_keyword(raw, "| Init")

        self._record(
            "show bfd sessions",
            True,
            f"Up: {up_count}, Down: {down_count}, Init: {init_count}",
        )

        if down_count > 0:
            self._record(
                "BFD sessions healthy",
                False,
                f"{down_count} BFD session(s) in Down state",
            )

    def _test_show_qos_summary(self):
        """3.10: show qos summary."""
        print("\n  -- show qos summary --")
        raw = self.run_show("show qos summary", timeout=30)
        self.saved_outputs["qos_summary"] = raw

        if "error" in raw.lower()[:200]:
            self._record("show qos summary", False, "Command returned error")
            return

        self._record("show qos summary", True, "Command executed")

    def _test_show_route(self):
        """3.11-3.12: show route summary and forwarding."""
        print("\n  -- show route summary --")
        raw = self.run_show("show route summary", timeout=30)
        self.saved_outputs["route_summary"] = raw

        if "error" in raw.lower()[:200]:
            self._record("show route summary", False, "Command returned error")
        else:
            self._record("show route summary", True, "Command executed")

            # Extract total route count if possible
            m = re.search(r"total.*?(\d+)", raw, re.IGNORECASE)
            if m:
                self._record(
                    "Route count",
                    True,
                    f"Total routes: {m.group(1)}",
                )

        # show route forwarding ncp <x> - try ncp 0
        print("\n  -- show route forwarding ncp 0 --")
        raw_fwd = self.run_show("show route forwarding ncp 0", timeout=30)
        self.saved_outputs["route_forwarding_ncp0"] = raw_fwd

        if "error" in raw_fwd.lower()[:200]:
            self._record("show route forwarding ncp 0", False, "Command returned error")
        else:
            self._record("show route forwarding ncp 0", True, "Command executed")

    def _test_show_stack_detail(self):
        """3.13: show sys stack detail - verify current, revert and target stacks."""
        print("\n  -- show sys stack detail --")
        raw = self.run_show("show sys stack detail", timeout=30)
        self.saved_outputs["stack_detail"] = raw

        if "error" in raw.lower()[:200] and "stack" not in raw.lower():
            self._record("show sys stack detail", False, "Command returned error")
            return

        self._record("show sys stack detail", True, "Command executed")

        # Check for current, revert, and target stacks
        has_current = bool(re.search(r"current", raw, re.IGNORECASE))
        has_revert = bool(re.search(r"revert", raw, re.IGNORECASE))
        has_target = bool(re.search(r"target", raw, re.IGNORECASE))

        if has_current:
            self._record("Stack: current present", True, "Current stack found")
        else:
            self._record("Stack: current present", False, "Current stack not found")

        if has_revert:
            self._record("Stack: revert present", True, "Revert stack found")
        else:
            self._record("Stack: revert present", False, "Revert stack not found")

        if has_target:
            self._record("Stack: target present", True, "Target stack found")
        else:
            self._record("Stack: target present", False, "Target stack not found (may be expected)")

    def _test_show_system_details(self):
        """3.14: show system details - verify all processes up, no HA failures."""
        print("\n  -- show system details --")
        raw = self.run_show("show system details", timeout=45)
        self.saved_outputs["system_details"] = raw

        if "error" in raw.lower()[:200]:
            self._record("show system details", False, "Command returned error")
            return

        self._record("show system details", True, "Command executed")

        # Check for HA failures
        ha_fail_count = self._count_keyword(raw, "HA failure")
        ha_fail_count += self._count_keyword(raw, "ha-failure")
        if ha_fail_count == 0:
            self._record("No HA failures", True, "No HA failures detected")
        else:
            self._record(
                "No HA failures",
                False,
                f"{ha_fail_count} HA failure(s) detected",
            )

        # Check for processes not running
        not_running = self._count_keyword(raw, "Not Running")
        not_running += self._count_keyword(raw, "not-running")
        if not_running == 0:
            self._record("All processes running", True, "No stopped processes detected")
        else:
            self._record(
                "All processes running",
                False,
                f"{not_running} process(es) not running",
            )

    def _test_show_system_hardware(self):
        """3.15: show system hardware."""
        print("\n  -- show system hardware --")
        raw = self.run_show("show system hardware", timeout=30)
        self.saved_outputs["system_hardware"] = raw

        if "error" in raw.lower()[:200]:
            self._record("show system hardware", False, "Command returned error")
            return

        self._record("show system hardware", True, "Command executed")

    # ==================================================================
    # SECTION 4: HA Validations (opt-in)
    # ==================================================================
    def test_ha_validations(self):
        """HA validations: SO/FO recovery, cold and warm restarts."""
        print("\n" + "=" * 60)
        print("SECTION 4: HA Validations")
        print("=" * 60)

        if not self.include_ha:
            print("  [SKIP] HA validations skipped (use --include-ha to enable)")
            print("  [SKIP] These are destructive tests: SO/FO, cold restart, warm restart")
            return

        self._test_ha_switchover()
        self._test_ha_cold_restart()
        self._test_ha_warm_restart()

    def _test_ha_switchover(self):
        """Perform SO (switchover) and verify system recovers."""
        print("\n  -- HA Switchover --")

        # Capture pre-switchover state
        pre_state = self.run_show("show system", timeout=30)
        pre_up = self._count_keyword(pre_state, "| UP")

        print("  [INFO] Initiating switchover...")
        self.run_operational_with_confirm(
            "request system switchover", confirm="yes", timeout=30
        )

        # Wait and reconnect
        ok = self.reconnect()
        if not ok:
            self._record("HA Switchover recovery", False, "Device did not come back within timeout")
            return

        # Verify post-switchover state
        time.sleep(10)
        post_state = self.run_show("show system", timeout=30)
        post_up = self._count_keyword(post_state, "| UP")

        if post_up >= pre_up - 1:
            self._record(
                "HA Switchover recovery",
                True,
                f"Pre-UP: {pre_up}, Post-UP: {post_up}",
            )
        else:
            self._record(
                "HA Switchover recovery",
                False,
                f"Pre-UP: {pre_up}, Post-UP: {post_up} (significant drop)",
            )

    def _test_ha_cold_restart(self):
        """Perform cold restart and verify system recovers."""
        print("\n  -- Cold Restart --")

        pre_state = self.run_show("show system", timeout=30)
        pre_up = self._count_keyword(pre_state, "| UP")

        print("  [INFO] Initiating cold restart...")
        self.run_operational_with_confirm(
            "request system restart", confirm="yes", timeout=30
        )

        ok = self.reconnect()
        if not ok:
            self._record("Cold restart recovery", False, "Device did not come back within timeout")
            return

        time.sleep(15)
        post_state = self.run_show("show system", timeout=30)
        post_up = self._count_keyword(post_state, "| UP")

        if post_up >= pre_up - 1:
            self._record(
                "Cold restart recovery",
                True,
                f"Pre-UP: {pre_up}, Post-UP: {post_up}",
            )
        else:
            self._record(
                "Cold restart recovery",
                False,
                f"Pre-UP: {pre_up}, Post-UP: {post_up} (significant drop)",
            )

    def _test_ha_warm_restart(self):
        """Perform warm restart and verify system recovers."""
        print("\n  -- Warm Restart --")

        pre_state = self.run_show("show system", timeout=30)
        pre_up = self._count_keyword(pre_state, "| UP")

        print("  [INFO] Initiating warm restart...")
        self.run_operational_with_confirm(
            "request system restart warm", confirm="yes", timeout=30
        )

        ok = self.reconnect()
        if not ok:
            self._record("Warm restart recovery", False, "Device did not come back within timeout")
            return

        time.sleep(15)
        post_state = self.run_show("show system", timeout=30)
        post_up = self._count_keyword(post_state, "| UP")

        if post_up >= pre_up - 1:
            self._record(
                "Warm restart recovery",
                True,
                f"Pre-UP: {pre_up}, Post-UP: {post_up}",
            )
        else:
            self._record(
                "Warm restart recovery",
                False,
                f"Pre-UP: {pre_up}, Post-UP: {post_up} (significant drop)",
            )

    # ==================================================================
    # SECTION 5: Config Validations
    # ==================================================================
    def test_config_validations(self):
        """Config validations: diff check, load override + rollback, commit test."""
        print("\n" + "=" * 60)
        print("SECTION 5: Config Validations")
        print("=" * 60)

        if self.skip_config_test:
            print("  [SKIP] Config validations skipped (--skip-config-test)")
            return

        self._test_config_diff()
        self._test_load_override_rollback()
        self._test_commit_pass()

    def _test_config_diff(self):
        """Verify no unexpected config diff (candidate vs running)."""
        print("\n  -- Config diff check --")
        raw = self.run_show("show config diff", timeout=30)
        self.saved_outputs["config_diff"] = raw

        # A clean diff should be empty or just show the prompt
        diff_lines = []
        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("show "):
                continue
            if stripped.endswith("#") or stripped.endswith(">"):
                continue
            if stripped in ("---", "+++", "@@"):
                diff_lines.append(stripped)
                continue
            if stripped.startswith("+") or stripped.startswith("-") or stripped.startswith("!"):
                diff_lines.append(stripped)

        if not diff_lines:
            self._record("Config diff clean", True, "No unexpected config differences")
        else:
            preview = "; ".join(diff_lines[:5])
            self._record(
                "Config diff clean",
                False,
                f"{len(diff_lines)} diff line(s): {preview[:200]}",
            )

    def _test_load_override_rollback(self):
        """Perform load override factory default + rollback 1."""
        print("\n  -- Load override factory default + rollback --")

        # Enter config mode and load override factory-default
        self._send("configure")
        self._read_until_prompt(timeout=10)

        self._send("load override factory-default")
        load_out = self._read_until_prompt(timeout=30)

        if self._has_error(load_out):
            self._record(
                "Load override factory-default",
                False,
                f"Error: {load_out[:200]}",
            )
            # Abort and exit
            self._send("rollback 0")
            self._read_until_prompt(timeout=15)
            self._send("exit")
            self._read_until_prompt(timeout=5)
            return

        self._record("Load override factory-default", True, "Loaded successfully")

        # Now rollback to restore the previous config
        self._send("rollback 1")
        rollback_out = self._read_until_prompt(timeout=30)

        if self._has_error(rollback_out):
            self._record(
                "Rollback 1",
                False,
                f"Error: {rollback_out[:200]}",
            )
            # Try rollback 0 to be safe
            self._send("rollback 0")
            self._read_until_prompt(timeout=15)
        else:
            self._record("Rollback 1", True, "Rolled back successfully")

        # Commit the rollback to confirm it
        self._send("commit")
        commit_out = self._read_until_prompt(timeout=60)

        if self._has_error(commit_out):
            self._record(
                "Commit after rollback",
                False,
                f"Error: {commit_out[:200]}",
            )
            # Emergency: rollback 0 and exit
            self._send("rollback 0")
            self._read_until_prompt(timeout=15)
        else:
            self._record("Commit after rollback", True, "Commit passed after rollback")

        self._send("exit")
        self._read_until_prompt(timeout=5)

    def _test_commit_pass(self):
        """Perform a no-op config change to verify commit works."""
        print("\n  -- Commit pass test --")

        # Enter config mode, change nothing, commit
        self._send("configure")
        self._read_until_prompt(timeout=10)

        self._send("commit")
        commit_out = self._read_until_prompt(timeout=30)

        self._send("exit")
        self._read_until_prompt(timeout=5)

        # A no-change commit should succeed (or say "no changes")
        if self._has_error(commit_out) and "no change" not in commit_out.lower():
            self._record(
                "Empty commit test",
                False,
                f"Commit failed: {commit_out[:200]}",
            )
        else:
            self._record("Empty commit test", True, "Commit passes (no-op)")

    # ==================================================================
    # SECTION 6: System Resources
    # ==================================================================
    def test_system_resources(self):
        """Check CPU and memory usage are within acceptable bounds."""
        print("\n" + "=" * 60)
        print("SECTION 6: System Resources")
        print("=" * 60)

        self._test_cpu_memory()
        self._test_docker_state()

    def _test_cpu_memory(self):
        """Check CPU and memory from show system hardware."""
        print("\n  -- System resources (CPU/Memory) --")

        raw = self.run_show("show system hardware", timeout=30)
        self.saved_outputs["system_hardware_resources"] = raw

        if "error" in raw.lower()[:200]:
            self._record("System resources", False, "Command returned error")
            return

        self._record("System resources output", True, "Command executed")

        # Parse memory usage
        mem_match = re.search(
            r"memory.*?(\d+)\s*(?:MB|GB).*?used.*?(\d+)\s*(?:MB|GB)",
            raw,
            re.IGNORECASE,
        )
        if mem_match:
            self._record(
                "Memory info",
                True,
                f"Total: {mem_match.group(1)}, Used: {mem_match.group(2)}",
            )

        # Parse CPU usage
        cpu_match = re.search(r"cpu.*?(\d+(?:\.\d+)?)\s*%", raw, re.IGNORECASE)
        if cpu_match:
            cpu_pct = float(cpu_match.group(1))
            cpu_ok = cpu_pct < 90.0
            self._record(
                "CPU usage",
                cpu_ok,
                f"CPU: {cpu_pct:.1f}%" + ("" if cpu_ok else " (HIGH - above 90%)"),
            )

    def _test_docker_state(self):
        """Check docker state if available."""
        print("\n  -- Docker state --")
        raw = self.run_show("show system docker state", timeout=30)
        self.saved_outputs["docker_state"] = raw

        if "error" in raw.lower()[:200] or "unknown" in raw.lower()[:200]:
            # Docker state command may not exist on all versions
            self._record(
                "Docker state",
                True,
                "Command not available (may be expected)",
            )
            return

        # Check for unhealthy containers
        unhealthy_count = self._count_keyword(raw, "unhealthy")
        exited_count = self._count_keyword(raw, "Exited")

        if unhealthy_count == 0 and exited_count == 0:
            self._record("Docker containers healthy", True, "No unhealthy or exited containers")
        else:
            self._record(
                "Docker containers healthy",
                False,
                f"Unhealthy: {unhealthy_count}, Exited: {exited_count}",
            )

    # ==================================================================
    # SECTION 7: AI / J2C+ Validations
    # ==================================================================
    def test_j2c_validations(self):
        """J2C+ validations: ICE interface, BGP, syncer."""
        print("\n" + "=" * 60)
        print("SECTION 7: AI / J2C+ Validations")
        print("=" * 60)

        self._test_ice_interface()
        self._test_ice_bgp()
        self._test_syncer_transactions()
        self._test_syncer_reachability()

    def _test_ice_interface(self):
        """Verify ICE interface is up."""
        print("\n  -- ICE interface --")
        raw = self.run_show("show interfaces", timeout=30)

        # Look for ICE interfaces
        ice_lines = []
        for line in raw.splitlines():
            if "ice" in line.lower():
                ice_lines.append(line.strip())

        if not ice_lines:
            self._record(
                "ICE interface present",
                False,
                "No ICE interfaces found (may not be a J2C+ setup)",
            )
            return

        self._record(
            "ICE interface present",
            True,
            f"Found {len(ice_lines)} ICE interface line(s)",
        )

        # Check if ICE interfaces are Up
        ice_up = sum(1 for line in ice_lines if re.search(r"\bUp\b", line, re.IGNORECASE))
        ice_down = sum(1 for line in ice_lines if re.search(r"\bDown\b", line, re.IGNORECASE))

        if ice_up > 0:
            self._record(
                "ICE interface Up",
                True,
                f"Up: {ice_up}, Down: {ice_down}",
            )
        else:
            self._record(
                "ICE interface Up",
                False,
                f"Up: {ice_up}, Down: {ice_down} - no ICE interfaces are up",
            )

    def _test_ice_bgp(self):
        """Verify BGP neighborship between ICE of cluster."""
        print("\n  -- ICE BGP neighborship --")
        raw = self.run_show("show bgp summary", timeout=30)
        self.saved_outputs["bgp_summary"] = raw

        if "error" in raw.lower()[:200]:
            self._record("BGP summary", False, "Command returned error")
            return

        # Count established vs non-established neighbors
        established = 0
        other_states = 0
        for line in raw.splitlines():
            stripped = line.strip()
            # Match neighbor rows: IP address followed by data columns
            if re.match(r"\d+\.\d+\.\d+\.\d+", stripped) or ("|" in stripped and re.search(r"\d+\.\d+\.\d+\.\d+", stripped)):
                if "established" in stripped.lower():
                    established += 1
                else:
                    # Check if last column is a number (means Established in standard format)
                    parts = stripped.split()
                    if parts:
                        last = parts[-1].strip("|").strip()
                        try:
                            int(last)
                            established += 1
                        except ValueError:
                            other_states += 1

        if established > 0:
            self._record(
                "BGP neighbors Established",
                True,
                f"Established: {established}, Other: {other_states}",
            )
        else:
            self._record(
                "BGP neighbors Established",
                False,
                f"No Established BGP neighbors (Other: {other_states})",
            )

    def _test_syncer_transactions(self):
        """Check syncer remote-transaction status and view."""
        print("\n  -- Syncer transactions --")

        # show system syncer remote-transaction status
        raw_status = self.run_show(
            "show system syncer remote-transaction status", timeout=30
        )
        self.saved_outputs["syncer_tx_status"] = raw_status

        if "error" in raw_status.lower()[:200] or "unknown" in raw_status.lower()[:200]:
            self._record(
                "Syncer remote-transaction status",
                True,
                "Command not available (may not be a J2C+ setup)",
            )
        else:
            self._record(
                "Syncer remote-transaction status",
                True,
                "Command executed",
            )
            # Check for failures
            if "fail" in raw_status.lower() or "error" in raw_status.lower():
                self._record(
                    "Syncer transactions healthy",
                    False,
                    "Failures detected in transaction status",
                )
            else:
                self._record(
                    "Syncer transactions healthy",
                    True,
                    "No failures in transaction status",
                )

        # show system syncer remote-transactions view
        raw_view = self.run_show(
            "show system syncer remote-transactions view", timeout=30
        )
        self.saved_outputs["syncer_tx_view"] = raw_view

        if "error" in raw_view.lower()[:200] or "unknown" in raw_view.lower()[:200]:
            self._record(
                "Syncer remote-transactions view",
                True,
                "Command not available (may not be a J2C+ setup)",
            )
        else:
            self._record("Syncer remote-transactions view", True, "Command executed")

    def _test_syncer_reachability(self):
        """Check syncer connectivity between setups."""
        print("\n  -- Syncer reachability --")

        raw = self.run_show(
            "show system syncer reachability status", timeout=30
        )
        self.saved_outputs["syncer_reachability"] = raw

        if "error" in raw.lower()[:200] or "unknown" in raw.lower()[:200]:
            self._record(
                "Syncer reachability",
                True,
                "Command not available (may not be a J2C+ setup)",
            )
            return

        self._record("Syncer reachability output", True, "Command executed")

        # Check for reachable/unreachable
        reachable_count = self._count_keyword(raw, "reachable")
        unreachable_count = self._count_keyword(raw, "unreachable")

        if unreachable_count > 0:
            self._record(
                "Syncer all reachable",
                False,
                f"Reachable: {reachable_count}, Unreachable: {unreachable_count}",
            )
        elif reachable_count > 0:
            self._record(
                "Syncer all reachable",
                True,
                f"Reachable: {reachable_count}",
            )
        else:
            self._record(
                "Syncer all reachable",
                True,
                "No reachability entries (may not be a multi-setup cluster)",
            )

    # ==================================================================
    # Save Raw Outputs
    # ==================================================================
    def save_outputs(self, output_file: Optional[str] = None):
        """Save all raw command outputs to a file for post-analysis."""
        if not output_file:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_file = f"gi_day_outputs_{self.host}_{ts}.txt"
        try:
            with open(output_file, "w") as f:
                for key, value in self.saved_outputs.items():
                    f.write(f"{'=' * 60}\n")
                    f.write(f"COMMAND: {key}\n")
                    f.write(f"{'=' * 60}\n")
                    f.write(value)
                    f.write("\n\n")
            print(f"[+] Raw outputs saved to: {output_file}")
        except OSError as e:
            print(f"[!] Could not save outputs: {e}")

    # ==================================================================
    # Orchestrator
    # ==================================================================
    def run_all(self) -> bool:
        """Run all validation sections and print summary."""
        start = datetime.now()
        print("=" * 60)
        print("  GI DAY VALIDATION TEST")
        print(f"  Device : {self.host}")
        print(f"  Started: {start.strftime('%Y-%m-%d %H:%M:%S')}")
        if self.expected_version:
            print(f"  Expected Version: {self.expected_version}")
        print(f"  HA Tests: {'ENABLED' if self.include_ha else 'DISABLED'}")
        print(f"  Config Tests: {'DISABLED' if self.skip_config_test else 'ENABLED'}")
        print("=" * 60)

        try:
            self.connect()

            # Section 1: FW Validations
            self.test_fw_versions()

            # Section 2: Install Verification
            self.test_install_verification()

            # Section 3: Show Commands
            self.test_show_commands()

            # Section 4: HA Validations (opt-in)
            self.test_ha_validations()

            # Section 5: Config Validations
            self.test_config_validations()

            # Section 6: System Resources
            self.test_system_resources()

            # Section 7: AI / J2C+ Validations
            self.test_j2c_validations()

        except Exception as exc:
            print(f"\n[ERROR] Unexpected exception: {exc}")
            self._record("Unexpected error", False, str(exc))
        finally:
            # Save raw outputs before disconnecting
            self.save_outputs()
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
        description="GI Day post-deployment validation test for DNOS devices. "
        "Ref: https://drivenets.atlassian.net/wiki/spaces/QA/pages/4115303055"
    )
    parser.add_argument(
        "--host",
        required=True,
        help="Device hostname or IP",
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
    parser.add_argument(
        "--expected-version",
        default=None,
        help="Expected DNOS/FW version string to verify (substring match)",
    )
    parser.add_argument(
        "--include-ha",
        action="store_true",
        default=False,
        help="Include HA validations (SO/FO, cold/warm restart) - DESTRUCTIVE",
    )
    parser.add_argument(
        "--skip-config-test",
        action="store_true",
        default=False,
        help="Skip config validations (load override + rollback, commit test)",
    )
    parser.add_argument(
        "--save-output",
        default=None,
        help="Custom path for saving raw command outputs (default: auto-generated)",
    )
    args = parser.parse_args()

    tester = GIDayValidationTest(
        host=args.host,
        username=args.user,
        password=args.password,
        include_ha=args.include_ha,
        skip_config_test=args.skip_config_test,
        expected_version=args.expected_version,
        timeout=args.timeout,
    )
    ok = tester.run_all()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
