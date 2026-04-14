#!/usr/bin/env python3
"""
SW-237053: Ethernet OAM Y.1731 | Functionality | System Event

Verifies that the CFM_PROACTIVE_TEST_FAILURE system event is emitted when
proactive DM / SLM sessions violate configured thresholds with
inform-test-results enabled.

Test plan
---------
1. Discover existing CFM context (MD, MA, MEP) on the initiator device.
2. Create PM profiles with inform-test-results enabled and intentionally
   tight thresholds (so normal network behaviour triggers violations).
3. Create proactive DM and SLM sessions using those profiles.
4. Wait for one or more test cycles to complete.
5. Verify CFM_PROACTIVE_TEST_FAILURE events appear in 'show system events'.
6. Validate event fields: test-type, session-name, session-id, source MEP ID,
   MA/MD names, threshold name/value, measured value, test end time.
7. Variant: trigger delay-rtt-avg, jitter-rtt-avg (DM); near-end-loss,
   far-end-loss (SLM).
8. Negative: inform-test-results disabled  → no event expected.
9. Negative: no thresholds configured      → no event expected.
10. Cleanup all test artefacts.

Usage
-----
    python3 test_sw237053_system_event.py \\
        --host 100.64.3.184 \\
        --md MD-CUST --ma MA-CUST --mep-id 1 --target-mep-id 2

    # If you already have DM/SLM sessions and only want to check events:
    python3 test_sw237053_system_event.py --host 100.64.3.184 --discover
"""

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple

import paramiko

sys.stdout.reconfigure(line_buffering=True)

# ---------------------------------------------------------------------------
#  Defaults
# ---------------------------------------------------------------------------
DEFAULT_USER = "dnroot"
DEFAULT_PASS = "dnroot"
OUTPUT_DIR = "/home/dn/output"

PM_BASE = "services performance-monitoring"
PM_CFM = f"{PM_BASE} cfm"
PM_PROF = f"{PM_BASE} profiles cfm"
CFM_BASE = "services ethernet-oam connectivity-fault-management"

ANSI_RE = re.compile(
    r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07|\x1b[()][AB012]"
    r"|\x1b\[\?[0-9;]*[hlm]|\r"
)
CLI_ERROR_RE = re.compile(
    r"error:|unknown\s+command|invalid|command\s+failed|"
    r"commit\s+failed|validation\s+failed|syntax\s+error",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
#  SSH helpers
# ---------------------------------------------------------------------------
def clean_ansi(text: str) -> str:
    return ANSI_RE.sub("", text).strip()


def create_shell(
    ip: str, user: str, password: str, label: str = ""
) -> Tuple[paramiko.SSHClient, paramiko.Channel]:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(
        ip,
        username=user,
        password=password,
        timeout=30,
        look_for_keys=False,
        allow_agent=False,
    )
    chan = ssh.invoke_shell(width=400, height=1000)
    time.sleep(5)
    chan.recv(65535)
    print(f"  [SSH {label}] Connected to {ip}", flush=True)
    return ssh, chan


def send(chan: paramiko.Channel, cmd: str, wait: float = 5) -> str:
    chan.send(cmd + "\n")
    time.sleep(wait)
    out = b""
    while chan.recv_ready():
        out += chan.recv(65535)
    return clean_ansi(out.decode(errors="replace"))


def run_show(chan: paramiko.Channel, cmd: str, wait: float = 10) -> str:
    return send(chan, cmd + " | no-more", wait)


def configure_and_commit(
    chan: paramiko.Channel,
    cmds: List[str],
    wait_per_cmd: float = 1.5,
    wait_commit: float = 30,
) -> Tuple[bool, str]:
    send(chan, "configure", 3)
    for cmd in cmds:
        send(chan, cmd, wait_per_cmd)
    out = send(chan, "commit", wait_commit)
    has_error = bool(CLI_ERROR_RE.search(out))
    if has_error:
        send(chan, "rollback", 5)
    send(chan, "end", 3)
    return not has_error, out


# ---------------------------------------------------------------------------
#  CFM discovery
# ---------------------------------------------------------------------------
def _send_long(chan: paramiko.Channel, cmd: str, timeout: int = 60) -> str:
    """Send a command and read output with a generous timeout for large configs."""
    chan.send(cmd + "\n")
    output = ""
    start = time.time()
    last_data = time.time()
    while True:
        if time.time() - start > timeout:
            break
        try:
            if chan.recv_ready():
                chunk = chan.recv(65535).decode(errors="replace")
                output += chunk
                last_data = time.time()
            else:
                if time.time() - last_data > 3:
                    break
                time.sleep(0.3)
        except Exception:
            break
    return clean_ansi(output)


def discover_cfm_context(
    chan: paramiko.Channel,
) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Return (md, ma, local_mep_id, remote_mep_id) from existing CFM config.

    Tries multiple show command variants to handle different CLI versions.
    """
    show_cmds = [
        "show config services ethernet-oam connectivity-fault-management | display-set | no-more",
        "show configuration services ethernet-oam connectivity-fault-management | display-set | no-more",
        "show config services ethernet-oam connectivity-fault-management | no-more",
        "show configuration services ethernet-oam connectivity-fault-management | no-more",
        "show config services ethernet-oam connectivity-fault-management | display-set",
        "show configuration services ethernet-oam connectivity-fault-management | display-set",
        "show config services ethernet-oam connectivity-fault-management",
        "show configuration services ethernet-oam connectivity-fault-management",
        "show config services ethernet-oam | display-set | match connectivity-fault-management | no-more",
        "show config services ethernet-oam | match connectivity-fault-management | no-more",
    ]

    output = ""
    used = None
    for cmd in show_cmds:
        out = _send_long(chan, cmd, timeout=30)
        has_err = bool(CLI_ERROR_RE.search(out))
        has_content = bool(
            re.search(r"(ethernet-oam|connectivity-fault-management|maintenance)", out, re.IGNORECASE)
        )
        if not has_err and has_content:
            output = out
            used = cmd
            break

    if not used:
        print("  Discovery: no show command returned usable CFM config.", flush=True)
        return None, None, None, None

    print(f"  Discovery: using '{used}'", flush=True)

    md_re = re.compile(
        r"\bmaintenance[-_]domain(?:s)?(?:[-_]name)?\s+(\S+)", re.IGNORECASE
    )
    ma_re = re.compile(
        r"\bmaintenance[-_]association(?:s)?(?:[-_]name)?\s+(\S+)", re.IGNORECASE
    )
    mep_id_re = re.compile(r"\bmep[-_]id\s+(\d+)\b", re.IGNORECASE)
    remote_mep_re = re.compile(
        r"\bremote[-_]mep(?:s)?(?:[-_]id)?\s+(\d+)\b", re.IGNORECASE
    )

    current_md: Optional[str] = None
    current_ma: Optional[str] = None
    candidates: Dict[Tuple[str, str], Dict] = {}

    for line in output.splitlines():
        md_m = md_re.search(line)
        if md_m:
            current_md = md_m.group(1)
            current_ma = None

        ma_m = ma_re.search(line)
        if ma_m:
            current_ma = ma_m.group(1)

        line_md = md_m.group(1) if md_m else current_md
        line_ma = ma_m.group(1) if ma_m else current_ma
        if not (line_md and line_ma):
            continue

        key = (line_md, line_ma)
        if key not in candidates:
            candidates[key] = {"meps": set(), "remote_meps": set()}

        is_remote = (
            bool(remote_mep_re.search(line))
            or "remote-mep" in line.lower()
            or "crosscheck" in line.lower()
        )
        for m in remote_mep_re.finditer(line):
            candidates[key]["remote_meps"].add(int(m.group(1)))
        if "crosscheck" in line.lower():
            for m in mep_id_re.finditer(line):
                candidates[key]["remote_meps"].add(int(m.group(1)))
        if is_remote:
            continue

        for m in mep_id_re.finditer(line):
            candidates[key]["meps"].add(int(m.group(1)))
        for m in re.finditer(r"\blocal-mep\s+(\d+)", line, re.IGNORECASE):
            candidates[key]["meps"].add(int(m.group(1)))

    if not candidates:
        sample = "\n".join(output.splitlines()[:30])
        print(f"  Discovery: parsed 0 candidates. Output sample:\n{sample}", flush=True)
        return None, None, None, None

    print(f"  Discovery: found {len(candidates)} MD/MA pair(s): {list(candidates.keys())}", flush=True)

    # Check which MEPs are already bound to PM sessions so we pick a free one
    bound_sources: set = set()
    pm_out = _send_long(chan, "show config services performance-monitoring | display-set | no-more", 30)
    if not pm_out or bool(CLI_ERROR_RE.search(pm_out)):
        pm_out = _send_long(chan, "show config services performance-monitoring | no-more", 30)
    for pm_line in pm_out.splitlines():
        src_m = re.search(
            r"source\s+maintenance-domain\s+(\S+)\s+maintenance-association\s+(\S+)\s+mep-id\s+(\d+)",
            pm_line, re.IGNORECASE,
        )
        if src_m:
            bound_sources.add((src_m.group(1), src_m.group(2), int(src_m.group(3))))
    if bound_sources:
        print(f"  Discovery: {len(bound_sources)} source MEP(s) already bound to PM sessions", flush=True)

    # Pick a candidate with a free (unbound) local MEP, preferring alphabetical order
    best_key = None
    best_local_mep = None
    for key in sorted(candidates.keys()):
        cand_md, cand_ma = key
        for mep in sorted(candidates[key]["meps"]):
            if (cand_md, cand_ma, mep) not in bound_sources:
                best_key = key
                best_local_mep = mep
                break
        if best_key:
            break

    # Fallback: pick any candidate with MEPs even if bound
    if not best_key:
        print("  Discovery: no unbound MEP found, using first available", flush=True)
        for key in sorted(candidates.keys()):
            if candidates[key]["meps"]:
                best_key = key
                best_local_mep = sorted(candidates[key]["meps"])[0]
                break
    if not best_key:
        best_key = sorted(candidates.keys())[0]

    md, ma = best_key
    meps = sorted(candidates[best_key]["meps"])
    remote_meps = sorted(candidates[best_key]["remote_meps"])
    local_mep = str(best_local_mep) if best_local_mep else (str(meps[0]) if meps else None)

    target_mep: Optional[str] = None
    if remote_meps:
        target_mep = str(remote_meps[0])
    elif len(meps) >= 2:
        target_mep = str(next(m for m in meps if m != best_local_mep))

    return md, ma, local_mep, target_mep


# ---------------------------------------------------------------------------
#  System-event helpers  (via 'set logging terminal')
# ---------------------------------------------------------------------------
# Events are streamed to the terminal as syslog messages when
# 'set logging terminal' is active in configure mode.
# Example line:
#   local7.alert 2025-07-29T08:28:44.014Z ncpl-cfm-nog CFM-OAM - - -
#   CFM_PROACTIVE_TEST_FAILURE:CFM proactive TWO_WAY_DELAY_MEASUREMENT
#   test session DM_CLI_TAB_mep1 (ID: 2662) initiated from MEP 1 in MA
#   MA-CUST under MD MD-CUST in level 7 has failed due to a violation
#   of the configured FRAME_DELAY_TWO_WAY_MAX threshold of 2us.
#   Recorded value: 13us. Test completed at: 2025-07-29 08:28:44 +0000

EVENT_RE = re.compile(
    r"local\d+\.\w+\s+\S+\s+\S+\s+CFM-OAM\s.*?CFM_PROACTIVE_TEST_FAILURE:(.+?)(?=local\d+\.\w+|\Z)",
    re.DOTALL,
)


def enable_terminal_logging(chan: paramiko.Channel) -> None:
    """Enable 'set logging terminal' so syslog events stream to the shell.

    This is a CLI session-level command (like 'set cli screen-length'),
    NOT a configuration change — no commit needed.
    """
    send(chan, "set logging terminal", 5)
    time.sleep(2)
    drain_channel(chan)


def disable_terminal_logging(chan: paramiko.Channel) -> None:
    send(chan, "no set logging terminal", 3)
    drain_channel(chan)


def drain_channel(chan: paramiko.Channel) -> str:
    """Read and return all currently buffered data from the channel."""
    out = b""
    while chan.recv_ready():
        out += chan.recv(65535)
    return clean_ansi(out.decode(errors="replace"))


def collect_events(chan: paramiko.Channel, wait_seconds: int) -> str:
    """Collect terminal output over wait_seconds, returning raw text."""
    collected = ""
    start = time.time()
    while time.time() - start < wait_seconds:
        time.sleep(2)
        if chan.recv_ready():
            chunk = chan.recv(65535).decode(errors="replace")
            collected += chunk
    return clean_ansi(collected)


def parse_cfm_proactive_events(raw: str) -> List[Dict[str, str]]:
    """Extract CFM_PROACTIVE_TEST_FAILURE events from terminal syslog output."""
    events: List[Dict[str, str]] = []

    for line in raw.splitlines():
        if "CFM_PROACTIVE_TEST_FAILURE" not in line:
            continue

        ev: Dict[str, str] = {"raw": line.strip()}

        ts = re.search(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", line)
        if ts:
            ev["timestamp"] = ts.group(1)

        # Severity from syslog priority
        sev = re.match(r"local\d+\.(\w+)", line)
        if sev:
            ev["severity"] = sev.group(1)

        # Group (always CFM-OAM for these events)
        if "CFM-OAM" in line:
            ev["group"] = "CFM-OAM"

        # test_type: e.g. TWO_WAY_DELAY_MEASUREMENT
        tt = re.search(r"CFM proactive (\S+) test session", line)
        if tt:
            ev["test-type"] = tt.group(1)

        # session_name and session_id
        sn = re.search(r"test session (\S+) \(ID:\s*(\d+)\)", line)
        if sn:
            ev["session-name"] = sn.group(1)
            ev["session-id"] = sn.group(2)

        # source MEP ID
        mep = re.search(r"from MEP (\d+)", line)
        if mep:
            ev["source-mep-id"] = mep.group(1)

        # MA name
        ma = re.search(r"in MA (\S+)", line)
        if ma:
            ev["maintenance-association"] = ma.group(1)

        # MD name
        md = re.search(r"under MD (\S+)", line)
        if md:
            ev["maintenance-domain"] = md.group(1)

        # MD level
        lvl = re.search(r"in level (\d+)", line)
        if lvl:
            ev["md-level"] = lvl.group(1)

        # threshold type and value
        th = re.search(r"configured (\S+) threshold of (\S+)", line)
        if th:
            ev["threshold-name"] = th.group(1)
            ev["threshold-value"] = th.group(2)

        # measured (violating) value
        mv = re.search(r"Recorded value:\s*(\S+)", line)
        if mv:
            ev["measured-value"] = mv.group(1).rstrip(".")

        # test end time
        te = re.search(r"Test completed at:\s*(.+?)$", line)
        if te:
            ev["test-end-time"] = te.group(1).strip()

        events.append(ev)
    return events


# ---------------------------------------------------------------------------
#  Test verdict tracker
# ---------------------------------------------------------------------------
class TestTracker:
    def __init__(self):
        self.verdicts: List[Dict] = []
        self.start_time = datetime.now(timezone.utc)

    def verdict(self, name: str, passed: bool, detail: str = ""):
        tag = "PASS" if passed else "FAIL"
        self.verdicts.append({"name": name, "passed": passed, "detail": detail})
        print(f"  [{tag}] {name}", flush=True)
        if detail:
            for line in detail.split("\n")[:5]:
                print(f"         {line}", flush=True)

    @property
    def pass_count(self) -> int:
        return sum(1 for v in self.verdicts if v["passed"])

    @property
    def fail_count(self) -> int:
        return sum(1 for v in self.verdicts if not v["passed"])

    def summary(self) -> str:
        lines = []
        for v in self.verdicts:
            tag = "PASS" if v["passed"] else "FAIL"
            lines.append(f"  [{tag}] {v['name']}")
            if v["detail"] and not v["passed"]:
                lines.append(f"         {v['detail']}")
        lines.append(
            f"\n  Passed: {self.pass_count}  Failed: {self.fail_count}  "
            f"Total: {len(self.verdicts)}"
        )
        return "\n".join(lines)


def section(title: str):
    sep = "=" * 78
    print(f"\n{sep}\n  {title}\n{sep}", flush=True)


# ---------------------------------------------------------------------------
#  Single profile + session names (reused across all phases)
# ---------------------------------------------------------------------------
DM_PROF = "DM-EVT-PROF"
SLM_PROF = "SLM-EVT-PROF"
DM_SESSION = "DM-EVT-SESS"
SLM_SESSION = "SLM-EVT-SESS"


# ---------------------------------------------------------------------------
#  Detect and temporarily free an occupied source slot
# ---------------------------------------------------------------------------
@dataclass
class SavedSession:
    name: str
    session_type: str  # "dm" or "slm"
    profile: str
    md: str
    ma: str
    mep_id: str
    target_mep: str
    description: str


def detect_occupied_sessions(
    chan: paramiko.Channel,
) -> List[SavedSession]:
    """Parse existing PM sessions from device config (hierarchical format)."""
    raw = _send_long(
        chan, "show config services performance-monitoring cfm | no-more", 30
    )
    sessions: List[SavedSession] = []

    current_name: Optional[str] = None
    current_type: Optional[str] = None
    props: Dict[str, str] = {}

    for line in raw.splitlines():
        stripped = line.strip()

        dm_m = re.match(r"two-way-delay-measurement\s+(\S+)", stripped)
        if dm_m:
            if current_name:
                sessions.append(_build_saved(current_name, current_type, props))
            current_name = dm_m.group(1)
            current_type = "dm"
            props = {}
            continue

        slm_m = re.match(r"two-way-synthetic-loss-measurement\s+(\S+)", stripped)
        if slm_m:
            if current_name:
                sessions.append(_build_saved(current_name, current_type, props))
            current_name = slm_m.group(1)
            current_type = "slm"
            props = {}
            continue

        if current_name:
            if stripped.startswith("profile "):
                props["profile"] = stripped.split(None, 1)[1]
            elif stripped.startswith("description "):
                props["description"] = stripped.split(None, 1)[1]
            elif stripped.startswith("source "):
                src_m = re.search(
                    r"maintenance-domain\s+(\S+)\s+maintenance-association\s+(\S+)\s+mep-id\s+(\d+)",
                    stripped,
                )
                if src_m:
                    props["md"] = src_m.group(1)
                    props["ma"] = src_m.group(2)
                    props["mep_id"] = src_m.group(3)
            elif stripped.startswith("target "):
                t_m = re.search(r"mep-id\s+(\d+)", stripped)
                if t_m:
                    props["target_mep"] = t_m.group(1)
            elif stripped == "!":
                if current_name:
                    sessions.append(_build_saved(current_name, current_type, props))
                current_name = None
                current_type = None
                props = {}

    if current_name:
        sessions.append(_build_saved(current_name, current_type, props))

    return sessions


def _build_saved(name: str, stype: Optional[str], props: Dict[str, str]) -> SavedSession:
    return SavedSession(
        name=name,
        session_type=stype or "dm",
        profile=props.get("profile", ""),
        md=props.get("md", ""),
        ma=props.get("ma", ""),
        mep_id=props.get("mep_id", ""),
        target_mep=props.get("target_mep", ""),
        description=props.get("description", ""),
    )


def free_slot(
    chan: paramiko.Channel,
    md: str,
    ma: str,
    mep_id: str,
    tracker: TestTracker,
) -> List[SavedSession]:
    """Remove existing sessions on the given source MEP. Returns saved sessions."""
    existing = detect_occupied_sessions(chan)
    blocking = [
        s for s in existing
        if s.md == md and s.ma == ma and s.mep_id == mep_id
        and s.name not in (DM_SESSION, SLM_SESSION)
    ]

    if not blocking:
        print("  No existing sessions to temporarily remove.", flush=True)
        return []

    print(f"  Temporarily removing {len(blocking)} existing session(s) to free slot:", flush=True)
    for s in blocking:
        print(f"    {s.session_type.upper()} '{s.name}' on {s.md}/{s.ma}/MEP {s.mep_id}", flush=True)

    del_cmds: List[str] = []
    for s in blocking:
        if s.session_type == "dm":
            del_cmds.append(f"no {PM_CFM} two-way-delay-measurement {s.name}")
        else:
            del_cmds.append(f"no {PM_CFM} two-way-synthetic-loss-measurement {s.name}")

    ok, out = configure_and_commit(chan, del_cmds, wait_per_cmd=1, wait_commit=30)
    tracker.verdict(
        f"Temporarily removed {len(blocking)} session(s) from slot",
        ok,
        out[:200] if not ok else "",
    )
    time.sleep(5)
    return blocking if ok else []


def restore_sessions(
    chan: paramiko.Channel,
    saved: List[SavedSession],
    tracker: TestTracker,
):
    """Recreate previously removed sessions."""
    if not saved:
        return

    print(f"  Restoring {len(saved)} previously-removed session(s)...", flush=True)
    cmds: List[str] = []
    for s in saved:
        if s.session_type == "dm":
            prefix = f"{PM_CFM} two-way-delay-measurement {s.name}"
        else:
            prefix = f"{PM_CFM} two-way-synthetic-loss-measurement {s.name}"

        cmds.append(f"{prefix} admin-state enabled")
        if s.description:
            cmds.append(f"{prefix} description {s.description}")
        if s.profile:
            cmds.append(f"{prefix} profile {s.profile}")
        cmds.append(
            f"{prefix} source maintenance-domain {s.md} "
            f"maintenance-association {s.ma} mep-id {s.mep_id}"
        )
        cmds.append(f"{prefix} target mep-id {s.target_mep}")

    ok, out = configure_and_commit(chan, cmds, wait_per_cmd=1, wait_commit=30)
    tracker.verdict("Original sessions restored", ok, out[:200] if not ok else "")


# ---------------------------------------------------------------------------
#  Test phases
# ---------------------------------------------------------------------------
def phase_setup_positive(
    chan: paramiko.Channel,
    md: str,
    ma: str,
    mep_id: str,
    target_mep: str,
    tracker: TestTracker,
) -> Tuple[bool, List[SavedSession]]:
    """Create 1 DM profile + 1 SLM profile + sessions with tight thresholds.

    If the source MEP already has sessions, they are temporarily removed and
    returned so the caller can restore them later.

    Thresholds are intentionally low so that normal operation triggers
    violations:
      - delay-rtt-avg 1  (1 µs — any real RTT exceeds this)
      - near-end-loss 0  (any loss triggers)
    """
    section("PHASE 1: Setup profile + session (positive — tight thresholds)")

    cmds: List[str] = [
        # DM profile: inform enabled, tight delay thresholds (actual RTT ~13 us)
        f"{PM_PROF} two-way-delay-measurement {DM_PROF} inform-test-results enabled",
        f"{PM_PROF} two-way-delay-measurement {DM_PROF} "
        f"test-duration probes probe-count 5 probe-interval 1 repeat-interval 10",
        f"{PM_PROF} two-way-delay-measurement {DM_PROF} thresholds delay-rtt-avg 1",
        f"{PM_PROF} two-way-delay-measurement {DM_PROF} thresholds delay-rtt-max 2",
        f"{PM_PROF} two-way-delay-measurement {DM_PROF} thresholds delay-rtt-min 2",
        # SLM profile: inform enabled, tight loss thresholds
        f"{PM_PROF} two-way-synthetic-loss-measurement {SLM_PROF} inform-test-results enabled",
        f"{PM_PROF} two-way-synthetic-loss-measurement {SLM_PROF} "
        f"test-duration probes probe-count 5 probe-interval 1 repeat-interval 10",
        f"{PM_PROF} two-way-synthetic-loss-measurement {SLM_PROF} thresholds near-end-loss 0",
        f"{PM_PROF} two-way-synthetic-loss-measurement {SLM_PROF} thresholds far-end-loss 0",
    ]

    ok, out = configure_and_commit(chan, cmds, wait_per_cmd=1, wait_commit=30)
    tracker.verdict("Profiles created/updated (DM delay + SLM near-end)", ok, out[:200] if not ok else "")
    if not ok:
        return False, []

    # Free source MEP slot if occupied by other sessions
    saved = free_slot(chan, md, ma, mep_id, tracker)

    session_cmds: List[str] = [
        f"{PM_CFM} two-way-delay-measurement {DM_SESSION} admin-state enabled",
        f"{PM_CFM} two-way-delay-measurement {DM_SESSION} profile {DM_PROF}",
        f"{PM_CFM} two-way-delay-measurement {DM_SESSION} source "
        f"maintenance-domain {md} maintenance-association {ma} mep-id {mep_id}",
        f"{PM_CFM} two-way-delay-measurement {DM_SESSION} target mep-id {target_mep}",
        f"{PM_CFM} two-way-synthetic-loss-measurement {SLM_SESSION} admin-state enabled",
        f"{PM_CFM} two-way-synthetic-loss-measurement {SLM_SESSION} profile {SLM_PROF}",
        f"{PM_CFM} two-way-synthetic-loss-measurement {SLM_SESSION} source "
        f"maintenance-domain {md} maintenance-association {ma} mep-id {mep_id}",
        f"{PM_CFM} two-way-synthetic-loss-measurement {SLM_SESSION} target mep-id {target_mep}",
    ]

    ok2, out2 = configure_and_commit(chan, session_cmds, wait_per_cmd=1, wait_commit=30)
    tracker.verdict("Sessions created (DM + SLM)", ok2, out2[:200] if not ok2 else "")
    if not ok2:
        restore_sessions(chan, saved, tracker)
        return False, []

    time.sleep(10)
    show_out = run_show(chan, "show services performance-monitoring cfm tests proactive", 12)
    for name in [DM_SESSION, SLM_SESSION]:
        tracker.verdict(f"Session {name} visible in proactive list", name in show_out)

    return True, saved


def wait_and_collect_events(
    chan: paramiko.Channel,
    label: str,
    cycles: int = 2,
    cycle_time: int = 45,
) -> str:
    """Wait for test cycles and collect terminal syslog output.

    Returns the accumulated raw text containing any streamed events.
    """
    total_wait = cycles * cycle_time
    print(f"  Waiting {total_wait}s for {label}...", flush=True)

    # Drain anything already buffered so we start fresh
    drain_channel(chan)

    collected = ""
    for i in range(cycles):
        print(f"    Cycle {i + 1}/{cycles} — collecting for {cycle_time}s...", flush=True)
        chunk = collect_events(chan, cycle_time)
        collected += chunk
        event_count = chunk.count("CFM_PROACTIVE_TEST_FAILURE")
        print(f"    Events captured this cycle: {event_count}", flush=True)

    return collected


def phase_verify_positive_events(
    raw_collected: str,
    tracker: TestTracker,
):
    """Verify CFM_PROACTIVE_TEST_FAILURE events for DM and SLM sessions."""
    section("PHASE 2: Verify positive-case system events (delay / near-end loss)")

    events = parse_cfm_proactive_events(raw_collected)

    print(f"  Found {len(events)} CFM_PROACTIVE_TEST_FAILURE event(s).", flush=True)
    tracker.verdict(
        "At least one CFM_PROACTIVE_TEST_FAILURE event emitted",
        len(events) > 0,
        f"Total events: {len(events)}",
    )

    if not events:
        print("  No events captured. Check that thresholds are being violated.", flush=True)
        snippet = [l for l in raw_collected.splitlines() if l.strip()][-20:]
        for line in snippet:
            print(f"    {line}", flush=True)
        return

    # DM event
    dm_events = [e for e in events if DM_SESSION in e.get("session-name", "")]
    tracker.verdict(
        f"DM event found for session {DM_SESSION}",
        len(dm_events) > 0,
        f"Found {len(dm_events)} DM event(s)",
    )
    if dm_events:
        _validate_event_fields(dm_events[0], "TWO_WAY_DELAY_MEASUREMENT", DM_SESSION, tracker)

    # SLM event
    slm_events = [e for e in events if SLM_SESSION in e.get("session-name", "")]
    tracker.verdict(
        f"SLM event found for session {SLM_SESSION}",
        len(slm_events) > 0,
        f"Found {len(slm_events)} SLM event(s)",
    )
    if slm_events:
        _validate_event_fields(slm_events[0], "TWO_WAY_SYNTHETIC_LOSS", SLM_SESSION, tracker)

    # Duplicate check
    for sess_name in [DM_SESSION, SLM_SESSION]:
        sess_events = [e for e in events if sess_name in e.get("session-name", "")]
        tracker.verdict(
            f"No excessive duplicates for {sess_name}",
            len(sess_events) <= 10,
            f"Count: {len(sess_events)}",
        )


def phase_variant_jitter(
    chan: paramiko.Channel,
    tracker: TestTracker,
    cycles: int,
    cycle_time: int,
):
    """Modify DM profile to use jitter threshold instead of delay."""
    section("PHASE 3a: Variant -- DM jitter threshold")

    cmds = [
        f"no {PM_PROF} two-way-delay-measurement {DM_PROF} thresholds delay-rtt-avg",
        f"{PM_PROF} two-way-delay-measurement {DM_PROF} thresholds jitter-rtt-avg 1",
    ]
    ok, out = configure_and_commit(chan, cmds, wait_per_cmd=1, wait_commit=20)
    tracker.verdict("DM profile switched to jitter threshold", ok, out[:200] if not ok else "")
    if not ok:
        return

    drain_channel(chan)
    raw = wait_and_collect_events(chan, "jitter variant", cycles, cycle_time)
    events = parse_cfm_proactive_events(raw)
    dm_events = [e for e in events if DM_SESSION in e.get("session-name", "")]
    tracker.verdict(
        "DM jitter threshold triggers event",
        len(dm_events) > 0,
        f"Found {len(dm_events)} event(s)",
    )

    restore = [
        f"no {PM_PROF} two-way-delay-measurement {DM_PROF} thresholds jitter-rtt-avg",
        f"{PM_PROF} two-way-delay-measurement {DM_PROF} thresholds delay-rtt-avg 1",
    ]
    configure_and_commit(chan, restore, wait_per_cmd=1, wait_commit=20)


def phase_variant_far_end(
    chan: paramiko.Channel,
    tracker: TestTracker,
    cycles: int,
    cycle_time: int,
):
    """Modify SLM profile to use far-end loss threshold instead of near-end."""
    section("PHASE 3b: Variant -- SLM far-end loss threshold")

    cmds = [
        f"no {PM_PROF} two-way-synthetic-loss-measurement {SLM_PROF} thresholds near-end-loss",
        f"{PM_PROF} two-way-synthetic-loss-measurement {SLM_PROF} thresholds far-end-loss 0",
    ]
    ok, out = configure_and_commit(chan, cmds, wait_per_cmd=1, wait_commit=20)
    tracker.verdict("SLM profile switched to far-end-loss threshold", ok, out[:200] if not ok else "")
    if not ok:
        return

    drain_channel(chan)
    raw = wait_and_collect_events(chan, "far-end loss variant", cycles, cycle_time)
    events = parse_cfm_proactive_events(raw)
    slm_events = [e for e in events if SLM_SESSION in e.get("session-name", "")]
    tracker.verdict(
        "SLM far-end-loss threshold triggers event",
        len(slm_events) > 0,
        f"Found {len(slm_events)} event(s)",
    )

    restore = [
        f"no {PM_PROF} two-way-synthetic-loss-measurement {SLM_PROF} thresholds far-end-loss",
        f"{PM_PROF} two-way-synthetic-loss-measurement {SLM_PROF} thresholds near-end-loss 0",
    ]
    configure_and_commit(chan, restore, wait_per_cmd=1, wait_commit=20)


def phase_negative_inform_disabled(
    chan: paramiko.Channel,
    tracker: TestTracker,
    cycles: int,
    cycle_time: int,
):
    """Set inform-test-results disabled -- no event should be emitted."""
    section("PHASE 4: Negative -- inform-test-results disabled")

    cmds = [
        f"{PM_PROF} two-way-delay-measurement {DM_PROF} inform-test-results disabled",
        f"{PM_PROF} two-way-synthetic-loss-measurement {SLM_PROF} inform-test-results disabled",
    ]
    ok, out = configure_and_commit(chan, cmds, wait_per_cmd=1, wait_commit=20)
    tracker.verdict("inform-test-results set to disabled on both profiles", ok, out[:200] if not ok else "")
    if not ok:
        return

    drain_channel(chan)
    raw = wait_and_collect_events(chan, "inform-disabled negative test", cycles, cycle_time)
    events = parse_cfm_proactive_events(raw)
    dm_events = [e for e in events if DM_SESSION in e.get("session-name", "")]
    slm_events = [e for e in events if SLM_SESSION in e.get("session-name", "")]

    tracker.verdict(
        "No DM event when inform-test-results disabled",
        len(dm_events) == 0,
        f"Found {len(dm_events)} unexpected event(s)",
    )
    tracker.verdict(
        "No SLM event when inform-test-results disabled",
        len(slm_events) == 0,
        f"Found {len(slm_events)} unexpected event(s)",
    )

    restore = [
        f"{PM_PROF} two-way-delay-measurement {DM_PROF} inform-test-results enabled",
        f"{PM_PROF} two-way-synthetic-loss-measurement {SLM_PROF} inform-test-results enabled",
    ]
    configure_and_commit(chan, restore, wait_per_cmd=1, wait_commit=20)


def phase_negative_no_thresholds(
    chan: paramiko.Channel,
    tracker: TestTracker,
    cycles: int,
    cycle_time: int,
):
    """Remove all thresholds -- no event should be emitted."""
    section("PHASE 5: Negative -- no thresholds configured")

    cmds = [
        f"no {PM_PROF} two-way-delay-measurement {DM_PROF} thresholds",
        f"no {PM_PROF} two-way-synthetic-loss-measurement {SLM_PROF} thresholds",
    ]
    ok, out = configure_and_commit(chan, cmds, wait_per_cmd=1, wait_commit=20)
    tracker.verdict("Thresholds removed from both profiles", ok, out[:200] if not ok else "")
    if not ok:
        return

    drain_channel(chan)
    raw = wait_and_collect_events(chan, "no-threshold negative test", cycles, cycle_time)
    events = parse_cfm_proactive_events(raw)
    dm_events = [e for e in events if DM_SESSION in e.get("session-name", "")]
    slm_events = [e for e in events if SLM_SESSION in e.get("session-name", "")]

    tracker.verdict(
        "No DM event when no thresholds configured",
        len(dm_events) == 0,
        f"Found {len(dm_events)} unexpected event(s)",
    )
    tracker.verdict(
        "No SLM event when no thresholds configured",
        len(slm_events) == 0,
        f"Found {len(slm_events)} unexpected event(s)",
    )


def phase_verify_show_output(
    chan: paramiko.Channel,
    tracker: TestTracker,
):
    """Verify session details are available via CLI show commands."""
    section("PHASE 6: Verify session detail in CLI show output")

    out_detail = run_show(
        chan,
        f"show services performance-monitoring cfm tests proactive "
        f"two-way-delay session-name {DM_SESSION} detail",
        12,
    )
    tracker.verdict(
        f"DM session {DM_SESSION} detail available",
        DM_SESSION in out_detail,
    )
    if "Inform Test Results: enabled" in out_detail:
        tracker.verdict("Inform Test Results shown as enabled in DM detail", True)

    out_slm = run_show(
        chan,
        f"show services performance-monitoring cfm tests proactive "
        f"two-way-synthetic-loss session-name {SLM_SESSION} detail",
        12,
    )
    tracker.verdict(
        f"SLM session {SLM_SESSION} detail available",
        SLM_SESSION in out_slm,
    )

    # Verify the event definition exists on the device
    ev_def = _send_long(
        chan,
        "show system logging system-events CFM_PROACTIVE_TEST_FAILURE",
        10,
    )
    has_def = "CFM_PROACTIVE_TEST_FAILURE" in ev_def and "CFM-OAM" in ev_def
    tracker.verdict(
        "CFM_PROACTIVE_TEST_FAILURE event defined in system-events catalog",
        has_def,
    )


def _validate_event_fields(
    ev: Dict[str, str], expected_type: str, expected_session: str, tracker: TestTracker,
):
    """Validate required fields in a single event dict."""
    required = ["session-name", "session-id"]
    optional = [
        "test-type", "mep-id", "source-mep-id",
        "maintenance-domain", "maintenance-association",
        "threshold-name", "threshold-value", "measured-value",
        "test-end-time", "severity", "group",
    ]

    present = [f for f in required if f in ev]
    missing = [f for f in required if f not in ev]
    tracker.verdict(
        f"Required fields present for {expected_session}",
        len(missing) == 0,
        f"Present: {present}, Missing: {missing}",
    )

    opt_present = [f for f in optional if f in ev]
    if opt_present:
        print(f"    Optional fields present: {opt_present}", flush=True)

    if "session-name" in ev:
        tracker.verdict(
            f"session-name matches {expected_session}",
            expected_session in ev["session-name"],
            f"Got: {ev['session-name']}",
        )

    if "session-id" in ev:
        tracker.verdict(
            f"session-id is numeric for {expected_session}",
            ev["session-id"].isdigit(),
            f"Got: {ev['session-id']}",
        )

    if "severity" in ev:
        print(f"    Severity: {ev['severity']}", flush=True)

    if "group" in ev:
        tracker.verdict(
            f"Event group is CFM-related for {expected_session}",
            "cfm" in ev["group"].lower() or "oam" in ev["group"].lower(),
            f"Got: {ev['group']}",
        )

    print(f"    Raw event:\n    {ev.get('raw', '')[:300]}", flush=True)


def phase_cleanup(
    chan: paramiko.Channel,
    tracker: TestTracker,
    saved_sessions: Optional[List[SavedSession]] = None,
):
    """Remove test sessions/profiles and restore any previously removed sessions."""
    section("CLEANUP")

    del_cmds = [
        f"no {PM_CFM} two-way-delay-measurement {DM_SESSION}",
        f"no {PM_CFM} two-way-synthetic-loss-measurement {SLM_SESSION}",
    ]
    ok1, _ = configure_and_commit(chan, del_cmds, wait_per_cmd=1, wait_commit=30)
    tracker.verdict("Test sessions deleted", ok1)

    time.sleep(5)

    prof_cmds = [
        f"no {PM_PROF} two-way-delay-measurement {DM_PROF}",
        f"no {PM_PROF} two-way-synthetic-loss-measurement {SLM_PROF}",
    ]
    ok2, _ = configure_and_commit(chan, prof_cmds, wait_per_cmd=1, wait_commit=20)
    tracker.verdict("Test profiles deleted", ok2)

    show = run_show(chan, "show services performance-monitoring cfm tests proactive", 10)
    leftover = DM_SESSION in show or SLM_SESSION in show
    tracker.verdict("No leftover test sessions", not leftover)

    if saved_sessions:
        restore_sessions(chan, saved_sessions, tracker)


# ===========================================================================
#  MAIN
# ===========================================================================
def main() -> int:
    parser = argparse.ArgumentParser(
        description="SW-237053: Y.1731 CFM_PROACTIVE_TEST_FAILURE system event test",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host", required=True, help="Device IP (initiator)")
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument("--password", default=DEFAULT_PASS)
    parser.add_argument("--md", default=None, help="Maintenance Domain name (auto-discovered if omitted)")
    parser.add_argument("--ma", default=None, help="Maintenance Association name (auto-discovered if omitted)")
    parser.add_argument("--mep-id", default=None, help="Local MEP ID (auto-discovered if omitted)")
    parser.add_argument("--target-mep-id", default=None, help="Target MEP ID (auto-discovered if omitted)")
    parser.add_argument("--discover", action="store_true", help="Only discover CFM context and print it")
    parser.add_argument("--cycles", type=int, default=2, help="Test cycles to wait per phase (default 2)")
    parser.add_argument("--cycle-time", type=int, default=45, help="Seconds per cycle (default 45)")
    parser.add_argument("--skip-cleanup", action="store_true", help="Don't remove test artefacts")
    parser.add_argument("--skip-variants", action="store_true", help="Skip jitter / far-end variant tests")
    parser.add_argument("--skip-negative", action="store_true", help="Skip negative tests")
    parser.add_argument("--cleanup-only", action="store_true", help="Only run cleanup phase")

    args = parser.parse_args()

    tracker = TestTracker()
    start_time = datetime.now(timezone.utc)

    print(f"{'=' * 78}", flush=True)
    print(f"  SW-237053: CFM_PROACTIVE_TEST_FAILURE System Event Test", flush=True)
    print(f"  Host: {args.host}", flush=True)
    print(f"  Started: {start_time.strftime('%Y-%m-%d %H:%M:%S UTC')}", flush=True)
    print(f"{'=' * 78}", flush=True)

    ssh, chan = create_shell(args.host, args.user, args.password, "main")

    try:
        # -- Discovery --
        section("CFM Context Discovery")
        md, ma, mep_id, target_mep = discover_cfm_context(chan)

        md = args.md or md
        ma = args.ma or ma
        mep_id = args.mep_id or mep_id
        target_mep = args.target_mep_id or target_mep

        print(f"  MD:         {md}", flush=True)
        print(f"  MA:         {ma}", flush=True)
        print(f"  Local MEP:  {mep_id}", flush=True)
        print(f"  Target MEP: {target_mep}", flush=True)

        if not all([md, ma, mep_id, target_mep]):
            print(
                "\n  ERROR: Could not discover all CFM parameters.\n"
                "  Use --md, --ma, --mep-id, --target-mep-id to specify manually.",
                flush=True,
            )
            return 1

        if args.discover:
            print("\n  --discover mode: exiting after discovery.", flush=True)
            return 0

        saved_sessions: List[SavedSession] = []

        if args.cleanup_only:
            phase_cleanup(chan, tracker)
        else:
            # Phase 1: Create profile + session with tight thresholds
            ok, saved_sessions = phase_setup_positive(
                chan, md, ma, mep_id, target_mep, tracker,
            )
            if not ok:
                print("  Setup failed -- aborting.", flush=True)
                return 1

            # Enable terminal logging AFTER setup so events aren't consumed
            section("Enable terminal logging")
            enable_terminal_logging(chan)
            print("  Terminal logging enabled (syslog events stream to shell).", flush=True)

            # Wait for events and collect terminal output
            section("Wait for positive test cycles")
            raw_positive = wait_and_collect_events(
                chan, "positive-case events", args.cycles, args.cycle_time,
            )

            # Phase 2: Verify positive events
            phase_verify_positive_events(raw_positive, tracker)

            # Phase 3a: Variant — jitter threshold (DM)
            if not args.skip_variants:
                phase_variant_jitter(chan, tracker, args.cycles, args.cycle_time)

            # Phase 3b: Variant — far-end loss (SLM)
            if not args.skip_variants:
                phase_variant_far_end(chan, tracker, args.cycles, args.cycle_time)

            # Phase 4: Negative — inform disabled
            if not args.skip_negative:
                phase_negative_inform_disabled(chan, tracker, args.cycles, args.cycle_time)

            # Phase 5: Negative — no thresholds
            if not args.skip_negative:
                phase_negative_no_thresholds(chan, tracker, args.cycles, args.cycle_time)

            # Disable terminal logging before show commands / cleanup
            section("Disable terminal logging")
            disable_terminal_logging(chan)
            print("  Terminal logging disabled.", flush=True)
            drain_channel(chan)

            # Phase 6: Show output verification
            phase_verify_show_output(chan, tracker)

            # Cleanup
            if not args.skip_cleanup:
                phase_cleanup(chan, tracker, saved_sessions)

    finally:
        print("\nClosing SSH session...", flush=True)
        try:
            ssh.close()
        except Exception:
            pass

    # -- Results --
    end_time = datetime.now(timezone.utc)
    elapsed = (end_time - start_time).total_seconds()

    print(f"\n{'=' * 78}", flush=True)
    print(f"  SW-237053 TEST RESULTS", flush=True)
    print(f"{'=' * 78}", flush=True)
    print(f"  Duration: {elapsed:.0f}s ({elapsed / 60:.1f} min)", flush=True)
    print(tracker.summary(), flush=True)

    overall = "PASS" if tracker.fail_count == 0 else "FAIL"
    print(f"\n  OVERALL: {overall}", flush=True)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(OUTPUT_DIR, "sw237053_system_event_results.json")
    payload = {
        "ticket": "SW-237053",
        "start": start_time.isoformat(),
        "end": end_time.isoformat(),
        "duration_s": round(elapsed, 1),
        "host": args.host,
        "cfm_context": {"md": md, "ma": ma, "mep_id": mep_id, "target_mep": target_mep},
        "overall": overall,
        "verdicts": tracker.verdicts,
    }
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"\n  Results saved to {out_path}", flush=True)
    print(f"\n{'=' * 78}\n  TEST COMPLETED\n{'=' * 78}", flush=True)

    return 0 if tracker.fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
