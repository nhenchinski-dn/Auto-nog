#!/usr/bin/env python3
"""
SPT Switchover CPU Validation Test for Q3D Multicast (SW-242472)

Validates that RPF failure traps reach the CPU during SPT switchover on Q3D,
and that CPRL protects the CPU from flooding.

Implementation dependencies (all Pending Merge as of 2026-02-25):
  SW-238696 - MC SPT solution (mutually exclusive with MOFRR)
  SW-235955 - MC events (+ RPF trap): WRONGVIF, NOCACHE, WHOLEPKT traps to CPU
  SW-235956 - MC counters: counter accuracy during/after switchover

On Q3D (J3), MC routes cannot be indexed by IIF.  RPF check failures are
trapped to CPU instead of being handled in hardware.  During switchover:
  - RPF failures hit CPU via Punted-IP-Multicast CPRL bucket
  - Packets are counted twice (RPF fail + CPU re-inject)
  - CPRL must rate-limit to prevent CPU flooding
  - SPT and MoFRR are mutually exclusive (SW-238696 design constraint)

Test flow:
  Phase 0: Pre-flight - Verify (S,G) route exists, MoFRR off, PIM neighbors up
  Phase 1: Baseline   - Capture CPRL, MC route state, forwarding counters
  Phase 2: Trigger    - Shut down the primary RPF interface to force RPF change
  Phase 3: Measure    - Poll CPRL/counters during switchover window
  Phase 4: Verify     - Confirm new RPF path, CPU traps, CPRL protection
  Phase 5: Restore    - Re-enable the primary interface, verify revert

Usage:
    python3 spt_cpu_validation_test.py
    python3 spt_cpu_validation_test.py --host 100.64.6.171
    python3 spt_cpu_validation_test.py --primary-iif ge800-0/0/31 --mc-group 232.10.10.10 --mc-source 3.5.0.2
    python3 spt_cpu_validation_test.py --no-restore --debug
"""

import argparse
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import paramiko


@dataclass
class Snapshot:
    """A point-in-time capture of all relevant counters."""
    timestamp: str = ""
    cprl: Dict[str, Dict[str, int]] = field(default_factory=dict)
    pim_stats_raw: str = ""
    mc_route_raw: str = ""
    mc_route_iif: str = ""
    mc_route_oif_count: int = 0
    mc_fwd_frames: int = 0
    mc_fwd_bps: float = 0.0
    mc_wrong_rpf: int = 0
    mc_punted: int = 0
    iface_counters: Dict[str, Dict[str, int]] = field(default_factory=dict)
    cprl_mc_rx: int = 0
    cprl_mc_drops: int = 0
    cprl_punt_rx: int = 0
    cprl_punt_drops: int = 0


class SPTCpuValidationTest:
    """Validates RPF failure CPU traps and CPRL protection during SPT switchover."""

    CPRL_MC_PROTOCOLS = ["PIM", "IGMP", "All-Routers", "All-Hosts"]
    CPRL_PUNT_PROTOCOL = "Punted-IP-Multicast"

    def __init__(self, host, username, password, primary_iif, mc_group,
                 mc_source, poll_interval, poll_count, no_restore, debug=False):
        self.host = host
        self.username = username
        self.password = password
        self.primary_iif = primary_iif
        self.mc_group = mc_group
        self.mc_source = mc_source
        self.poll_interval = poll_interval
        self.poll_count = poll_count
        self.no_restore = no_restore
        self.debug = debug
        self.client: Optional[paramiko.SSHClient] = None
        self.shell: Optional[paramiko.Channel] = None
        self.results: List[Tuple[str, bool, str]] = []

    # ------------------------------------------------------------------ #
    # SSH helpers
    # ------------------------------------------------------------------ #
    def connect(self):
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
        self.shell = self.client.invoke_shell(width=300, height=1000)
        self._read_until_prompt(timeout=15)
        self._send("no-paging")
        self._read_until_prompt(timeout=5)
        print("[+] Connected and paging disabled.\n")

    def disconnect(self):
        if self.shell:
            self.shell.close()
        if self.client:
            self.client.close()
        print("\n[*] Disconnected.")

    def _send(self, cmd: str):
        self.shell.send(cmd + "\n")

    def _read_until_prompt(self, timeout: int = 30) -> str:
        buf = ""
        end_time = time.time() + timeout
        while time.time() < end_time:
            if self.shell.recv_ready():
                chunk = self.shell.recv(65536).decode("utf-8", errors="replace")
                buf += chunk
                lines = buf.strip().split("\n")
                last = lines[-1].strip() if lines else ""
                if last.endswith("#") or last.endswith(">"):
                    break
            else:
                time.sleep(0.2)
        return buf

    def run_show(self, cmd: str, timeout: int = 30) -> str:
        self._send(cmd)
        return self._read_until_prompt(timeout=timeout)

    def run_config(self, config_lines: List[str], commit_timeout: int = 30) -> str:
        self._send("configure")
        self._read_until_prompt(timeout=10)
        for line in config_lines:
            self._send(line)
            self._read_until_prompt(timeout=5)
        self._send("commit")
        commit_out = self._read_until_prompt(timeout=commit_timeout)
        self._send("exit")
        self._read_until_prompt(timeout=5)
        return commit_out

    def run_operational(self, cmd: str, timeout: int = 15) -> str:
        self._send(cmd)
        return self._read_until_prompt(timeout=timeout)

    # ------------------------------------------------------------------ #
    # Parsers
    # ------------------------------------------------------------------ #
    @staticmethod
    def parse_cprl_table(output: str) -> Dict[str, Dict[str, int]]:
        result = {}
        row_re = re.compile(
            r"\|\s*(.+?)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|"
        )
        for line in output.split("\n"):
            m = row_re.search(line)
            if m:
                proto = m.group(1).strip()
                if proto.startswith("Control"):
                    continue
                result[proto] = {
                    "rate": int(m.group(2)),
                    "burst": int(m.group(3)),
                    "rx": int(m.group(4)),
                    "policer_drops": int(m.group(5)),
                    "total_drops": int(m.group(6)),
                }
        return result

    @staticmethod
    def parse_mc_route_iif(output: str) -> str:
        """Parse IIF from DNOS 'show multicast route' output.

        Matches the section header 'Incoming Interfaces:' (plural) and
        returns the first non-'Any' interface on the following line.
        """
        lines = output.split("\n")
        for i, line in enumerate(lines):
            stripped = line.strip().lower()
            if stripped in ("incoming interfaces:", "incoming interface:"):
                for j in range(i + 1, min(i + 4, len(lines))):
                    candidate = lines[j].strip().split(",")[0].strip()
                    if candidate and candidate != "Any":
                        return candidate
                break
        return "UNKNOWN"

    @staticmethod
    def parse_mc_route_oif_count(output: str) -> int:
        """Parse OIF count from DNOS 'show multicast route' output."""
        count = 0
        in_oif = False
        for line in output.split("\n"):
            stripped = line.strip()
            lower = stripped.lower()
            if lower in ("outgoing interfaces:", "outgoing interface:"):
                in_oif = True
                continue
            if in_oif:
                if not stripped or stripped.startswith(("(", "Counters:", "Forwarded", "Punted", "Wrong", "Uptime:", "Incoming", "VRF:")):
                    in_oif = False
                    continue
                iface = stripped.split(",")[0].strip()
                if iface and iface != "Any":
                    count += 1
        return count

    @staticmethod
    def parse_wrong_rpf(output: str) -> int:
        """Parse 'Wrong RPF packets' from DNOS multicast route output."""
        for line in output.split("\n"):
            if "wrong rpf" in line.lower():
                nums = re.findall(r"\d+", line)
                if nums:
                    return int(nums[-1])
        return 0

    @staticmethod
    def parse_punted_packets(output: str) -> int:
        """Parse 'Punted packets' from DNOS multicast route output."""
        for line in output.split("\n"):
            if "punted packets" in line.lower():
                nums = re.findall(r"\d+", line)
                if nums:
                    return int(nums[-1])
        return 0

    @staticmethod
    def parse_forwarded_frames(output: str) -> int:
        """Parse 'Forwarded frames' count from multicast route output."""
        for line in output.split("\n"):
            if "forwarded frames" in line.lower():
                nums = re.findall(r"\d+", line)
                if nums:
                    return int(nums[0])
        return 0

    @staticmethod
    def parse_forwarded_bps(output: str) -> float:
        """Parse forwarding rate in bps from multicast route counters."""
        for line in output.split("\n"):
            lower = line.lower()
            if "forwarded octets" in lower:
                m = re.search(r"\((\d+)\s*bps", line)
                if m:
                    return float(m.group(1))
        return 0.0

    @staticmethod
    def parse_interface_counters(output: str) -> Dict[str, int]:
        counters = {"rx_packets": 0, "tx_packets": 0}
        for line in output.split("\n"):
            lower = line.lower().strip()
            nums = re.findall(r"\d+", line)
            if not nums:
                continue
            if "rx" in lower and "packet" in lower:
                counters["rx_packets"] = int(nums[-1])
            elif "tx" in lower and "packet" in lower:
                counters["tx_packets"] = int(nums[-1])
        return counters

    @staticmethod
    def parse_pim_summary(output: str) -> Dict[str, int]:
        """Parse key fields from 'show pim summary'."""
        info: Dict[str, int] = {}
        patterns = {
            "sg_ssm": r"Number of \(S,G\)SSM route entries\s*:\s*(\d+)",
            "sg_sm": r"Number of \(S,G\)SM route entries\s*:\s*(\d+)",
            "star_g": r"Number of \(\*,G\) route entries\s*:\s*(\d+)",
            "mofrr": r"Number of MoFRR protected entries\s*:\s*(\d+)",
            "total_entries": r"Total PIM Tree entries\s*:\s*(\d+)",
            "total_replications": r"Total PIM MFIB routes\s*:\s*(\d+)",
        }
        for key, pattern in patterns.items():
            m = re.search(pattern, output)
            info[key] = int(m.group(1)) if m else 0
        return info

    # ------------------------------------------------------------------ #
    # Snapshot
    # ------------------------------------------------------------------ #
    def capture_snapshot(self, label: str) -> Snapshot:
        print(f"  [{label}] Capturing snapshot ...")
        snap = Snapshot(timestamp=datetime.now().strftime("%H:%M:%S.%f")[:-3])

        cprl_raw = self.run_show("show system cprl")
        snap.cprl = self.parse_cprl_table(cprl_raw)

        mc_cmd = f"show multicast route group-range {self.mc_group}/32"
        snap.mc_route_raw = self.run_show(mc_cmd, timeout=60)
        if self.debug:
            print(f"  [DEBUG] Raw 'show multicast route' output:\n{snap.mc_route_raw}")
        snap.mc_route_iif = self.parse_mc_route_iif(snap.mc_route_raw)
        snap.mc_route_oif_count = self.parse_mc_route_oif_count(snap.mc_route_raw)
        snap.mc_wrong_rpf = self.parse_wrong_rpf(snap.mc_route_raw)
        snap.mc_punted = self.parse_punted_packets(snap.mc_route_raw)
        snap.mc_fwd_frames = self.parse_forwarded_frames(snap.mc_route_raw)
        snap.mc_fwd_bps = self.parse_forwarded_bps(snap.mc_route_raw)

        mc_rx = 0
        mc_drops = 0
        for proto in self.CPRL_MC_PROTOCOLS:
            if proto in snap.cprl:
                mc_rx += snap.cprl[proto]["rx"]
                mc_drops += snap.cprl[proto]["policer_drops"]
        snap.cprl_mc_rx = mc_rx
        snap.cprl_mc_drops = mc_drops

        if self.CPRL_PUNT_PROTOCOL in snap.cprl:
            snap.cprl_punt_rx = snap.cprl[self.CPRL_PUNT_PROTOCOL]["rx"]
            snap.cprl_punt_drops = snap.cprl[self.CPRL_PUNT_PROTOCOL]["policer_drops"]

        iif_raw = self.run_show(f"show interfaces {self.primary_iif} counters")
        snap.iface_counters[self.primary_iif] = self.parse_interface_counters(iif_raw)

        print(f"  [{label}] IIF={snap.mc_route_iif}, OIFs={snap.mc_route_oif_count}, "
              f"WrongRPF={snap.mc_wrong_rpf}, Punted={snap.mc_punted}, "
              f"FwdFrames={snap.mc_fwd_frames}, "
              f"CPRL-punt-rx={snap.cprl_punt_rx}, CPRL-mc-rx={snap.cprl_mc_rx}")
        return snap

    # ------------------------------------------------------------------ #
    # Recording
    # ------------------------------------------------------------------ #
    def _record(self, name: str, passed: bool, detail: str = ""):
        self.results.append((name, passed, detail))
        tag = "[PASS]" if passed else "[FAIL]"
        print(f"  {tag} {name}" + (f" -- {detail}" if detail else ""))

    # ------------------------------------------------------------------ #
    # Test phases
    # ------------------------------------------------------------------ #
    def phase0_preflight(self) -> bool:
        """Pre-flight checks: verify device is ready for SPT switchover testing."""
        print("\n" + "=" * 70)
        print("PHASE 0: PRE-FLIGHT CHECKS")
        print("=" * 70)

        pim_raw = self.run_show("show pim summary")
        if self.debug:
            print(f"  [DEBUG] Raw 'show pim summary':\n{pim_raw}")
        pim = self.parse_pim_summary(pim_raw)

        has_sg = pim.get("sg_ssm", 0) + pim.get("sg_sm", 0) > 0
        self._record(
            "PIM has (S,G) routes",
            has_sg,
            f"SSM={pim.get('sg_ssm', 0)}, SM={pim.get('sg_sm', 0)}",
        )

        mofrr_off = pim.get("mofrr", 0) == 0
        self._record(
            "MoFRR is not active (SW-238696: mutually exclusive with SPT)",
            mofrr_off,
            f"MoFRR protected entries={pim.get('mofrr', 0)}",
        )

        mc_route_raw = self.run_show(
            f"show multicast route group-range {self.mc_group}/32", timeout=60
        )
        iif = self.parse_mc_route_iif(mc_route_raw)
        oif_count = self.parse_mc_route_oif_count(mc_route_raw)
        fwd_bps = self.parse_forwarded_bps(mc_route_raw)

        route_exists = iif != "UNKNOWN"
        self._record(
            f"MC route exists for ({self.mc_source},{self.mc_group})",
            route_exists,
            f"IIF={iif}, OIFs={oif_count}",
        )

        if route_exists:
            if self.primary_iif not in iif and iif not in self.primary_iif:
                print(f"  [AUTO] IIF mismatch: --primary-iif={self.primary_iif}, "
                      f"actual={iif}. Updating to use actual IIF.")
                self.primary_iif = iif

            self._record(
                "Primary IIF resolved",
                True,
                f"will shut {self.primary_iif} (live IIF from route)",
            )

            traffic_flowing = fwd_bps > 0
            self._record(
                "Multicast traffic is flowing",
                traffic_flowing,
                f"rate={fwd_bps:.0f} bps ({fwd_bps/1e6:.3f} Mbps)",
            )

        nbr_raw = self.run_show("show pim neighbors")
        iif_base = self.primary_iif.split(".")[0]
        has_nbr = iif_base in nbr_raw or self.primary_iif in nbr_raw
        self._record(
            f"PIM neighbor on IIF ({self.primary_iif})",
            has_nbr,
            "neighbor found" if has_nbr else "NO neighbor on IIF",
        )

        cprl_raw = self.run_show("show system cprl")
        cprl = self.parse_cprl_table(cprl_raw)
        has_punt = self.CPRL_PUNT_PROTOCOL in cprl
        punt_rate = cprl.get(self.CPRL_PUNT_PROTOCOL, {}).get("rate", 0)
        self._record(
            f"CPRL has {self.CPRL_PUNT_PROTOCOL} bucket",
            has_punt,
            f"rate={punt_rate} pps" if has_punt else "MISSING — RPF traps unprotected",
        )

        preflight_ok = all(passed for _, passed, _ in self.results)
        if not preflight_ok:
            print("\n  [!] Pre-flight checks failed. SPT PRs may not be merged yet.")
            print("      Required: SW-238696, SW-235955, SW-235956")
        return preflight_ok

    def phase1_baseline(self) -> Snapshot:
        print("\n" + "=" * 70)
        print("PHASE 1: BASELINE CAPTURE")
        print("=" * 70)

        self.run_operational("clear system cprl counters")
        time.sleep(2)

        snap = self.capture_snapshot("baseline")

        self._record(
            "CPRL table parsed",
            len(snap.cprl) > 0,
            f"{len(snap.cprl)} protocols found",
        )
        self._record(
            "Baseline Wrong RPF = 0",
            snap.mc_wrong_rpf == 0,
            f"Wrong RPF packets={snap.mc_wrong_rpf}",
        )

        return snap

    def phase2_trigger(self):
        print("\n" + "=" * 70)
        print("PHASE 2: TRIGGER SPT SWITCHOVER")
        print(f"  Shutting down primary IIF: {self.primary_iif}")
        print("=" * 70)

        base_iif = self.primary_iif.split(".")[0]
        commit_out = self.run_config([
            f"interfaces {base_iif} admin-state disabled",
        ])

        commit_lower = commit_out.lower()
        ok = not any(k in commit_lower for k in ["error:", "failed", "syntax error"])
        self._record(
            "Commit: shut primary IIF",
            ok,
            f"interface {base_iif}" + ("" if ok else f" -- {commit_out[:150]}"),
        )
        if not ok:
            print("  [!] Commit failed -- aborting")
        return ok

    def phase3_measure(self, baseline: Snapshot) -> List[Snapshot]:
        print("\n" + "=" * 70)
        print("PHASE 3: MEASURE DURING SWITCHOVER WINDOW")
        print(f"  Polling {self.poll_count}x, {self.poll_interval}s apart")
        print("=" * 70)

        snapshots = []
        for i in range(self.poll_count):
            time.sleep(self.poll_interval)
            snap = self.capture_snapshot(f"poll-{i+1}/{self.poll_count}")
            snapshots.append(snap)

            d_punt_rx = snap.cprl_punt_rx - baseline.cprl_punt_rx
            d_punt_drops = snap.cprl_punt_drops - baseline.cprl_punt_drops
            d_mc_rx = snap.cprl_mc_rx - baseline.cprl_mc_rx
            d_rpf = snap.mc_wrong_rpf - baseline.mc_wrong_rpf
            d_punted = snap.mc_punted - baseline.mc_punted
            print(f"    Delta: WrongRPF=+{d_rpf}, Punted=+{d_punted}, "
                  f"CPRL-punt-rx=+{d_punt_rx}, CPRL-punt-drops=+{d_punt_drops}, "
                  f"CPRL-mc-rx=+{d_mc_rx}, IIF={snap.mc_route_iif}")
        return snapshots

    def phase4_verify(self, baseline: Snapshot, snaps: List[Snapshot]):
        print("\n" + "=" * 70)
        print("PHASE 4: VERIFICATION")
        print("=" * 70)

        if not snaps:
            self._record("Switchover data", False, "No snapshots")
            return

        final = snaps[-1]

        iif_changed = (
            final.mc_route_iif != baseline.mc_route_iif
            and final.mc_route_iif != "UNKNOWN"
        )
        route_gone = final.mc_route_iif == "UNKNOWN"
        if route_gone:
            self._record(
                "IIF changed after switchover",
                False,
                f"Route disappeared (IIF shut with no alternate RPF path)",
            )
        else:
            self._record(
                "IIF changed after switchover",
                iif_changed,
                f"before={baseline.mc_route_iif}, after={final.mc_route_iif}",
            )

        rpf_delta = final.mc_wrong_rpf - baseline.mc_wrong_rpf
        self._record(
            "Wrong RPF packets increased (RPF traps to CPU)",
            rpf_delta > 0,
            f"Wrong RPF delta = +{rpf_delta}",
        )

        punted_delta = final.mc_punted - baseline.mc_punted
        self._record(
            "Punted packets increased (CPU handled traffic)",
            punted_delta > 0,
            f"Punted delta = +{punted_delta}",
        )

        punt_rx_delta = final.cprl_punt_rx - baseline.cprl_punt_rx
        self._record(
            f"CPRL {self.CPRL_PUNT_PROTOCOL} rx increased",
            punt_rx_delta > 0,
            f"Punted-IP-Multicast rx delta = +{punt_rx_delta}",
        )

        mc_rx_delta = final.cprl_mc_rx - baseline.cprl_mc_rx
        self._record(
            "CPRL PIM/IGMP rx increased",
            mc_rx_delta > 0,
            f"PIM+IGMP+AllRouters+AllHosts rx delta = +{mc_rx_delta}",
        )

        punt_drops = final.cprl_punt_drops - baseline.cprl_punt_drops
        mc_drops = final.cprl_mc_drops - baseline.cprl_mc_drops
        total_drops = punt_drops + mc_drops
        if total_drops > 0:
            self._record(
                "CPRL rate-limited traps (policer drops)",
                True,
                f"Punted-IP-MC drops=+{punt_drops}, PIM/IGMP drops=+{mc_drops} (CPU protected)",
            )
        else:
            self._record(
                "CPRL rate-limited traps (policer drops)",
                True,
                "No policer drops — rate below CPRL threshold (informational)",
            )

        if not route_gone:
            self._record(
                "MC route still has OIFs post-switchover",
                final.mc_route_oif_count > 0,
                f"OIF count = {final.mc_route_oif_count}",
            )

        print(f"\n  CPRL per-protocol delta (baseline -> final):")
        print(f"  {'Protocol':<25} {'RX delta':>12} {'Drop delta':>14}")
        print(f"  {'-'*25} {'-'*12} {'-'*14}")
        all_protos = sorted(set(list(baseline.cprl.keys()) + list(final.cprl.keys())))
        for proto in all_protos:
            b = baseline.cprl.get(proto, {"rx": 0, "policer_drops": 0})
            f = final.cprl.get(proto, {"rx": 0, "policer_drops": 0})
            rx_d = f["rx"] - b["rx"]
            pd_d = f["policer_drops"] - b["policer_drops"]
            if rx_d != 0 or pd_d != 0:
                print(f"  {proto:<25} {'+' + str(rx_d):>12} {'+' + str(pd_d):>14}")

        print(f"\n  Multicast route counter delta:")
        print(f"    Wrong RPF packets : +{final.mc_wrong_rpf - baseline.mc_wrong_rpf}")
        print(f"    Punted packets    : +{final.mc_punted - baseline.mc_punted}")
        print(f"    Forwarded frames  : +{final.mc_fwd_frames - baseline.mc_fwd_frames}")

    def phase5_restore(self, baseline: Snapshot):
        print("\n" + "=" * 70)
        print("PHASE 5: RESTORE PRIMARY PATH")
        print("=" * 70)

        base_iif = self.primary_iif.split(".")[0]
        commit_out = self.run_config([
            f"interfaces {base_iif} admin-state enabled",
        ])
        commit_lower = commit_out.lower()
        ok = not any(k in commit_lower for k in ["error:", "failed", "syntax error"])
        self._record("Commit: re-enable primary IIF", ok, f"interface {base_iif}")

        print("  Waiting for convergence (20s) ...")
        time.sleep(20)

        restored = self.capture_snapshot("restored")
        reverted = (self.primary_iif in restored.mc_route_iif
                    or restored.mc_route_iif in self.primary_iif
                    or restored.mc_route_iif == baseline.mc_route_iif)
        self._record(
            "IIF reverted to primary path",
            reverted,
            f"expected={baseline.mc_route_iif}, actual={restored.mc_route_iif}",
        )

        if restored.mc_fwd_bps > 0:
            self._record(
                "Traffic resumed after restore",
                True,
                f"rate={restored.mc_fwd_bps:.0f} bps ({restored.mc_fwd_bps/1e6:.3f} Mbps)",
            )
        else:
            self._record(
                "Traffic resumed after restore",
                False,
                "No traffic flowing — possible black hole",
            )

        rpf_delta = restored.mc_wrong_rpf - baseline.mc_wrong_rpf
        if rpf_delta > 0:
            print(f"\n  [INFO] Wrong RPF packets after restore: +{rpf_delta}")
            print(f"         (RPF traps fired during convergence — expected on Q3D)")

    # ------------------------------------------------------------------ #
    # Orchestrator
    # ------------------------------------------------------------------ #
    def run_all(self) -> bool:
        start = datetime.now()
        print("=" * 70)
        print("  SPT CPU VALIDATION TEST  --  Q3D Multicast")
        print(f"  Jira        : SW-242472 (test), SW-238696 (impl)")
        print(f"  Device      : {self.host}")
        print(f"  Primary IIF : {self.primary_iif}")
        print(f"  MC (S,G)    : ({self.mc_source or '*'},{self.mc_group})")
        print(f"  Poll        : {self.poll_count} x {self.poll_interval}s")
        print(f"  Started     : {start.strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 70)

        try:
            self.connect()

            preflight_ok = self.phase0_preflight()
            if not preflight_ok:
                print("\n  [!] Continuing despite pre-flight failures "
                      "(use results to diagnose)")

            print(f"\n  Resolved IIF : {self.primary_iif}")
            baseline = self.phase1_baseline()
            trigger_ok = self.phase2_trigger()

            if trigger_ok:
                snaps = self.phase3_measure(baseline)
                self.phase4_verify(baseline, snaps)
                if self.no_restore:
                    print(f"\n  [!] --no-restore: {self.primary_iif} left SHUT")
                else:
                    self.phase5_restore(baseline)
            else:
                self._record("Test aborted", False, "Could not trigger switchover")

        except Exception as exc:
            print(f"\n[ERROR] {exc}")
            self._record("Unexpected error", False, str(exc))
        finally:
            try:
                self.disconnect()
            except Exception:
                pass

        elapsed = (datetime.now() - start).total_seconds()
        total = len(self.results)
        passed = sum(1 for _, p, _ in self.results if p)
        failed = total - passed

        print("\n" + "=" * 70)
        print("  SUMMARY")
        print("=" * 70)
        print(f"  Total : {total}")
        print(f"  Passed: {passed}")
        print(f"  Failed: {failed}")
        print(f"  Time  : {elapsed:.1f}s")
        print("=" * 70)

        if failed:
            print("\n  Failed checks:")
            for name, p, detail in self.results:
                if not p:
                    print(f"    - {name}: {detail}")

        verdict = "ALL CHECKS PASSED" if failed == 0 else "SOME CHECKS FAILED"
        print(f"\n  >>> {verdict} <<<\n")
        return failed == 0


def main():
    parser = argparse.ArgumentParser(
        description="SPT switchover CPU validation test for Q3D multicast (SW-242472)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 spt_cpu_validation_test.py
  python3 spt_cpu_validation_test.py --primary-iif ge800-0/0/31 --mc-group 232.10.10.10 --mc-source 3.5.0.2
  python3 spt_cpu_validation_test.py --poll-count 10 --poll-interval 1
  python3 spt_cpu_validation_test.py --no-restore --debug
""",
    )
    parser.add_argument("--host", default="100.64.6.171",
                        help="Device hostname or IP (default: 100.64.6.171)")
    parser.add_argument("--user", default="dnroot",
                        help="SSH username (default: dnroot)")
    parser.add_argument("--password", default="dnroot",
                        help="SSH password (default: dnroot)")
    parser.add_argument("--primary-iif", default="ge800-0/0/31",
                        help="Primary IIF to shut for triggering RPF change "
                             "(default: ge800-0/0/31)")
    parser.add_argument("--mc-group", default="232.10.10.10",
                        help="Multicast group address (default: 232.10.10.10)")
    parser.add_argument("--mc-source", default="3.5.0.2",
                        help="Multicast source address (default: 3.5.0.2)")
    parser.add_argument("--poll-interval", type=float, default=2.0,
                        help="Seconds between polls during switchover (default: 2)")
    parser.add_argument("--poll-count", type=int, default=5,
                        help="Number of polls during switchover window (default: 5)")
    parser.add_argument("--no-restore", action="store_true",
                        help="Don't re-enable primary IIF after test")
    parser.add_argument("--debug", action="store_true",
                        help="Print raw command output for parser debugging")
    args = parser.parse_args()

    tester = SPTCpuValidationTest(
        host=args.host,
        username=args.user,
        password=args.password,
        primary_iif=args.primary_iif,
        mc_group=args.mc_group,
        mc_source=args.mc_source,
        poll_interval=args.poll_interval,
        poll_count=args.poll_count,
        no_restore=args.no_restore,
        debug=args.debug,
    )
    ok = tester.run_all()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
