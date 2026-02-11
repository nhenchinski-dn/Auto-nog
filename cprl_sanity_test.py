#!/usr/bin/env python3
"""
CPRL (Control Plane Rate Limiting) Sanity Test Script

Runs a happy-flow sanity test on a DNOS device to verify CPRL works correctly:
  1. Show defaults and verify expected values
  2. Modify ICMP rate/burst and verify
  3. Modify BGP rate/burst and verify
  4. Clear counters and verify
  5. Revert to defaults and verify
  6. Per-NCP view verification

Usage:
    python3 cprl_sanity_test.py
    python3 cprl_sanity_test.py --host xgu1f7v900009p2 --user dnroot --password dnroot
    python3 cprl_sanity_test.py --no-revert   # leave modified config in place
"""

import argparse
import re
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import paramiko


# Expected default values per protocol (from live device output)
EXPECTED_DEFAULTS = {
    "ICMP":          {"rate": 250,  "burst": 300},
    "ICMPv6":        {"rate": 250,  "burst": 300},
    "BGP":           {"rate": 5000, "burst": 1000},
    "ARP":           {"rate": 250,  "burst": 300},
    "LLDP":          {"rate": 300,  "burst": 300},
    "OSPF":          {"rate": 500,  "burst": 1000},
    "OSPFv3":        {"rate": 500,  "burst": 1000},
    "BFD":           {"rate": 4000, "burst": 2000},
    "LDP":           {"rate": 500,  "burst": 1000},
    "RSVP":          {"rate": 500,  "burst": 1000},
    "IS-IS":         {"rate": 500,  "burst": 1000},
    "LACP":          {"rate": 250,  "burst": 300},
    "NTP":           {"rate": 110,  "burst": 550},
    "SNMP":          {"rate": 2000, "burst": 4000},
    "SSH":           {"rate": 1000, "burst": 1000},
    "NDP":           {"rate": 250,  "burst": 300},
    "All-Hosts":     {"rate": 500,  "burst": 1000},
    "All-Routers":   {"rate": 500,  "burst": 1000},
}


class CPRLSanityTest:
    """CPRL happy-flow sanity tester for DNOS devices."""

    def __init__(self, host: str, username: str, password: str, no_revert: bool = False):
        self.host = host
        self.username = username
        self.password = password
        self.no_revert = no_revert
        self.client: Optional[paramiko.SSHClient] = None
        self.shell: Optional[paramiko.Channel] = None
        self.results: List[Tuple[str, bool, str]] = []  # (test_name, passed, detail)

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
            timeout=30,
        )
        self.shell = self.client.invoke_shell(width=250, height=1000)
        # Wait for initial prompt
        self._read_until_prompt(timeout=15)
        # Disable CLI paging so show output is not truncated
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
        """Read shell output until we see a DNOS prompt (ending with # or >)."""
        buf = ""
        end_time = time.time() + timeout
        while time.time() < end_time:
            if self.shell.recv_ready():
                chunk = self.shell.recv(65536).decode("utf-8", errors="replace")
                buf += chunk
                # DNOS prompts typically end with # or (cfg...)#
                lines = buf.strip().split("\n")
                last_line = lines[-1].strip() if lines else ""
                if last_line.endswith("#") or last_line.endswith(">"):
                    break
            else:
                time.sleep(0.2)
        return buf

    def run_show(self, cmd: str, timeout: int = 30) -> str:
        """Run a show / operational command and return its output."""
        self._send(cmd)
        output = self._read_until_prompt(timeout=timeout)
        return output

    def run_config(self, config_lines: List[str]) -> str:
        """Enter config mode, apply lines, commit, and exit config mode."""
        self._send("configure")
        self._read_until_prompt(timeout=10)

        for line in config_lines:
            self._send(line)
            self._read_until_prompt(timeout=5)

        self._send("commit")
        commit_output = self._read_until_prompt(timeout=30)

        self._send("exit")
        self._read_until_prompt(timeout=5)

        return commit_output

    def run_operational(self, cmd: str, timeout: int = 15) -> str:
        """Run an operational (non-show) command like clear."""
        self._send(cmd)
        output = self._read_until_prompt(timeout=timeout)
        return output

    # ------------------------------------------------------------------
    # Parser
    # ------------------------------------------------------------------
    @staticmethod
    def parse_cprl_table(output: str) -> Dict[str, Dict[str, int]]:
        """
        Parse the output of 'show system cprl' into a dict:
            { "ICMP": {"rate": 250, "burst": 300, "rx": 0,
                       "policer_drops": 0, "total_drops": 0}, ... }
        """
        result: Dict[str, Dict[str, int]] = {}
        # Each data row looks like:
        # | ICMP                  | 250                  | 300                      | 0 ...
        row_re = re.compile(
            r"\|\s*(.+?)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|"
        )
        for line in output.split("\n"):
            m = row_re.search(line)
            if m:
                proto = m.group(1).strip()
                # Skip header row
                if proto.startswith("Control"):
                    continue
                result[proto] = {
                    "rate":          int(m.group(2)),
                    "burst":         int(m.group(3)),
                    "rx":            int(m.group(4)),
                    "policer_drops": int(m.group(5)),
                    "total_drops":   int(m.group(6)),
                }
        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _record(self, name: str, passed: bool, detail: str = ""):
        status = "PASS" if passed else "FAIL"
        self.results.append((name, passed, detail))
        tag = "[PASS]" if passed else "[FAIL]"
        print(f"  {tag} {name}" + (f" -- {detail}" if detail else ""))

    def _get_cprl(self, ncp: Optional[int] = None) -> Dict[str, Dict[str, int]]:
        """Run show system cprl (optionally per-NCP) and parse."""
        cmd = "show system cprl"
        if ncp is not None:
            cmd += f" ncp {ncp}"
        raw = self.run_show(cmd, timeout=30)
        return self.parse_cprl_table(raw)

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------
    def test_show_defaults(self):
        """Step 1: Verify default CPRL values."""
        print("\n" + "=" * 60)
        print("TEST 1: Verify CPRL default values")
        print("=" * 60)

        table = self._get_cprl()

        if not table:
            self._record("Parse CPRL table", False, "Table is empty or unparseable")
            return

        self._record("Parse CPRL table", True, f"{len(table)} protocols found")

        for proto, expected in EXPECTED_DEFAULTS.items():
            row = table.get(proto)
            if row is None:
                self._record(f"Default {proto}", False, "Protocol not found in table")
                continue
            rate_ok = row["rate"] == expected["rate"]
            burst_ok = row["burst"] == expected["burst"]
            if rate_ok and burst_ok:
                self._record(
                    f"Default {proto}",
                    True,
                    f"rate={row['rate']}, burst={row['burst']}",
                )
            else:
                self._record(
                    f"Default {proto}",
                    False,
                    f"expected rate={expected['rate']}/burst={expected['burst']}, "
                    f"got rate={row['rate']}/burst={row['burst']}",
                )

    def test_modify_icmp(self):
        """Step 2: Modify ICMP rate/burst and verify."""
        print("\n" + "=" * 60)
        print("TEST 2: Modify ICMP rate=500, burst=600")
        print("=" * 60)

        config = [
            "system",
            "cprl",
            "icmp",
            "rate 500",
            "burst 600",
            "exit",  # exit icmp
            "exit",  # exit cprl
            "exit",  # exit system
        ]
        commit_out = self.run_config(config)
        if "error" in commit_out.lower() and "commit" not in commit_out.lower():
            self._record("Commit ICMP config", False, commit_out[:200])
            return

        self._record("Commit ICMP config", True)

        table = self._get_cprl()
        row = table.get("ICMP")
        if row is None:
            self._record("Verify ICMP change", False, "ICMP not found")
            return
        ok = row["rate"] == 500 and row["burst"] == 600
        self._record(
            "Verify ICMP change",
            ok,
            f"rate={row['rate']}, burst={row['burst']}",
        )

    def test_modify_bgp(self):
        """Step 3: Modify BGP rate/burst and verify."""
        print("\n" + "=" * 60)
        print("TEST 3: Modify BGP rate=3000, burst=2000")
        print("=" * 60)

        config = [
            "system",
            "cprl",
            "bgp",
            "rate 3000",
            "burst 2000",
            "exit",  # exit bgp
            "exit",  # exit cprl
            "exit",  # exit system
        ]
        commit_out = self.run_config(config)
        if "error" in commit_out.lower() and "commit" not in commit_out.lower():
            self._record("Commit BGP config", False, commit_out[:200])
            return

        self._record("Commit BGP config", True)

        table = self._get_cprl()
        row = table.get("BGP")
        if row is None:
            self._record("Verify BGP change", False, "BGP not found")
            return
        ok = row["rate"] == 3000 and row["burst"] == 2000
        self._record(
            "Verify BGP change",
            ok,
            f"rate={row['rate']}, burst={row['burst']}",
        )

    def test_clear_counters(self):
        """Step 4: Clear CPRL counters and verify."""
        print("\n" + "=" * 60)
        print("TEST 4: Clear CPRL counters")
        print("=" * 60)

        self.run_operational("clear system cprl counters")
        time.sleep(2)  # give a moment for counters to reset

        table = self._get_cprl()
        if not table:
            self._record("Clear counters", False, "Empty table")
            return

        all_zero = True
        nonzero_protos = []
        for proto, vals in table.items():
            if vals["policer_drops"] != 0 or vals["total_drops"] != 0:
                all_zero = False
                nonzero_protos.append(proto)

        if all_zero:
            self._record("Clear counters (drops)", True, "All drop counters are 0")
        else:
            self._record(
                "Clear counters (drops)",
                False,
                f"Non-zero drops in: {', '.join(nonzero_protos)}",
            )

    def test_revert_defaults(self):
        """Step 5: Revert ICMP and BGP to defaults and verify."""
        print("\n" + "=" * 60)
        print("TEST 5: Revert ICMP and BGP to defaults")
        print("=" * 60)

        config = [
            "system",
            "cprl",
            "icmp",
            "no rate",
            "no burst",
            "exit",  # exit icmp
            "bgp",
            "no rate",
            "no burst",
            "exit",  # exit bgp
            "exit",  # exit cprl
            "exit",  # exit system
        ]
        commit_out = self.run_config(config)
        if "error" in commit_out.lower() and "commit" not in commit_out.lower():
            self._record("Commit revert config", False, commit_out[:200])
            return

        self._record("Commit revert config", True)

        table = self._get_cprl()

        # Check ICMP back to defaults
        icmp = table.get("ICMP")
        if icmp:
            ok = icmp["rate"] == 250 and icmp["burst"] == 300
            self._record(
                "ICMP reverted to defaults",
                ok,
                f"rate={icmp['rate']}, burst={icmp['burst']}",
            )
        else:
            self._record("ICMP reverted to defaults", False, "ICMP not found")

        # Check BGP back to defaults
        bgp = table.get("BGP")
        if bgp:
            ok = bgp["rate"] == 5000 and bgp["burst"] == 1000
            self._record(
                "BGP reverted to defaults",
                ok,
                f"rate={bgp['rate']}, burst={bgp['burst']}",
            )
        else:
            self._record("BGP reverted to defaults", False, "BGP not found")

    def test_per_ncp_view(self):
        """Step 6: Verify per-NCP CPRL view matches global."""
        print("\n" + "=" * 60)
        print("TEST 6: Per-NCP (ncp 0) CPRL view")
        print("=" * 60)

        global_table = self._get_cprl()
        ncp_table = self._get_cprl(ncp=0)

        if not ncp_table:
            self._record("Per-NCP table parsed", False, "Empty table")
            return

        self._record("Per-NCP table parsed", True, f"{len(ncp_table)} protocols")

        # Verify rate/burst match between global and ncp-0 views
        mismatches = []
        for proto in global_table:
            if proto not in ncp_table:
                continue  # some aggregation-only rows may differ
            g = global_table[proto]
            n = ncp_table[proto]
            if g["rate"] != n["rate"] or g["burst"] != n["burst"]:
                mismatches.append(
                    f"{proto}: global rate/burst={g['rate']}/{g['burst']} "
                    f"vs ncp0={n['rate']}/{n['burst']}"
                )

        if not mismatches:
            self._record(
                "NCP-0 rate/burst match global",
                True,
                "All protocols match",
            )
        else:
            self._record(
                "NCP-0 rate/burst match global",
                False,
                "; ".join(mismatches),
            )

    # ------------------------------------------------------------------
    # Orchestrator
    # ------------------------------------------------------------------
    def run_all(self) -> bool:
        """Run all tests and print summary. Returns True if all passed."""
        start = datetime.now()
        print("=" * 60)
        print("  CPRL SANITY TEST  --  Happy Flow")
        print(f"  Device : {self.host}")
        print(f"  Started: {start.strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 60)

        try:
            self.connect()

            self.test_show_defaults()
            self.test_modify_icmp()
            self.test_modify_bgp()
            self.test_clear_counters()
            if self.no_revert:
                print("\n" + "=" * 60)
                print("SKIPPING TEST 5: Revert (--no-revert flag set)")
                print("  Config left as: ICMP rate=500/burst=600, BGP rate=3000/burst=2000")
                print("=" * 60)
            else:
                self.test_revert_defaults()
            self.test_per_ncp_view()

        except Exception as exc:
            print(f"\n[ERROR] Unexpected exception: {exc}")
            self._record("Unexpected error", False, str(exc))
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
        description="CPRL happy-flow sanity test for DNOS devices"
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
        "--no-revert",
        action="store_true",
        default=False,
        help="Skip the revert step -- leave modified CPRL config on the device",
    )
    args = parser.parse_args()

    tester = CPRLSanityTest(
        host=args.host,
        username=args.user,
        password=args.password,
        no_revert=args.no_revert,
    )
    ok = tester.run_all()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
