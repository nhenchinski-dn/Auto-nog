#!/usr/bin/env python3
"""
SW-239791: Ethernet OAM Y.1731 | SNMP Scale — Happy Flow

Validates DRIVENETS-CFM-MIB SNMP tables work correctly under scale.
Walks all 4 proactive tables, verifies all sessions are present,
spot-checks SNMP values against CLI, and checks for duplicate OIDs.

Expects proactive DM + SLM sessions already configured on the device.

Usage:
    python3 test_sw239791_snmp_scale.py --host 100.64.7.110 --community noga
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import paramiko

sys.stdout.reconfigure(line_buffering=True)

DEFAULT_USER = "dnroot"
DEFAULT_PASS = "dnroot"
OUTPUT_DIR = "/home/dn/output"

DN_CFM_OID = "1.3.6.1.4.1.49739.2.15"
DN_CFM_OBJECTS = f"{DN_CFM_OID}.1"

TABLES = {
    "proactive_dm_session":  {"oid": f"{DN_CFM_OBJECTS}.15", "label": "Proactive DM Session"},
    "proactive_dm_results":  {"oid": f"{DN_CFM_OBJECTS}.16", "label": "Proactive DM Results"},
    "proactive_slm_session": {"oid": f"{DN_CFM_OBJECTS}.17", "label": "Proactive SLM Session"},
    "proactive_slm_results": {"oid": f"{DN_CFM_OBJECTS}.18", "label": "Proactive SLM Results"},
}

ANSI_RE = re.compile(
    r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07|\x1b[()][AB012]"
    r"|\x1b\[\?[0-9;]*[hlm]|\r"
)
SNMP_NUMERIC_RE = re.compile(
    r"\.1\.3\.6\.1\.4\.1\.49739\.2\.15\.1\.(\d+)\.1\.(\d+)\.([\d.]+)\s+=\s+(.+)"
)

PM_BASE = "services performance-monitoring"


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
        ip, username=user, password=password,
        timeout=30, look_for_keys=False, allow_agent=False,
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


# ---------------------------------------------------------------------------
#  SNMP helpers
# ---------------------------------------------------------------------------
def snmpwalk(
    host: str, oid: str, community: str,
    per_oid_timeout: int = 5, total_timeout: int = 600,
) -> Tuple[str, float]:
    cmd = [
        "snmpwalk", "-v2c", "-c", community,
        "-On", "-t", str(per_oid_timeout), "-r", "1",
        host, oid,
    ]
    t0 = time.monotonic()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=total_timeout)
        elapsed = time.monotonic() - t0
        output = result.stdout
        if result.returncode != 0 and not output:
            output = result.stderr
        return output, elapsed
    except FileNotFoundError:
        return "ERROR: snmpwalk not found", time.monotonic() - t0
    except subprocess.TimeoutExpired:
        return "ERROR: snmpwalk timed out", time.monotonic() - t0


def parse_walk(raw: str) -> Dict[str, Dict[int, str]]:
    rows: Dict[str, Dict[int, str]] = {}
    for line in raw.splitlines():
        m = SNMP_NUMERIC_RE.search(line)
        if not m:
            continue
        key = f"{m.group(1)}.{m.group(3)}"
        if key not in rows:
            rows[key] = {}
        rows[key][int(m.group(2))] = m.group(4).strip()
    return rows


def extract_val(raw_val: str) -> str:
    raw_val = raw_val.strip()
    for prefix in ["STRING:", "INTEGER:", "Gauge32:", "Hex-STRING:", "OID:", "Timeticks:"]:
        if raw_val.startswith(prefix):
            val = raw_val[len(prefix):].strip().strip('"')
            if prefix == "Gauge32:":
                m = re.match(r"^(\d+)", val)
                if m:
                    return m.group(1)
            return val
    return raw_val


def count_sessions(raw: str, table_suffix: int) -> int:
    indices = set()
    for line in raw.splitlines():
        m = SNMP_NUMERIC_RE.search(line)
        if m and int(m.group(1)) == table_suffix:
            indices.add(m.group(3))
    return len(indices)


def find_duplicates(raw: str) -> Tuple[int, List[str]]:
    oid_re = re.compile(r"(\.1\.3\.6\.1\.4\.1\.49739\.2\.15[\d.]+)\s*=")
    seen: Dict[str, int] = {}
    dups = []
    for line in raw.splitlines():
        m = oid_re.search(line)
        if m:
            oid = m.group(1)
            seen[oid] = seen.get(oid, 0) + 1
            if seen[oid] == 2:
                dups.append(oid)
    return len(seen), dups


# ---------------------------------------------------------------------------
#  CLI helpers
# ---------------------------------------------------------------------------
def parse_cli_detail(output: str) -> Dict[str, str]:
    info: Dict[str, str] = {}
    for key, pat in {
        "md_name": r"Maintenance Domain:\s*(\S+)",
        "ma_name": r"Maintenance Association:\s*(\S+)",
        "source_interface": r"Source Interface:\s*(\S+)",
        "count": r"Count:\s*(\d+)\s*probes",
        "interval": r"Interval:\s*(\d+)\s*second",
    }.items():
        m = re.search(pat, output, re.IGNORECASE)
        if m:
            info[key] = m.group(1).rstrip(",")
    return info


def get_proactive_sessions(chan: paramiko.Channel) -> Tuple[List[str], List[str]]:
    out = run_show(chan, f"show {PM_BASE} cfm tests proactive", 15)
    dm, slm = [], []
    for line in out.splitlines():
        if "|" not in line or line.strip().startswith("+"):
            continue
        parts = [p.strip() for p in line.split("|") if p.strip()]
        if len(parts) >= 2:
            if "two-way-delay" in parts[1]:
                dm.append(parts[0])
            elif "two-way-synthetic-loss" in parts[1]:
                slm.append(parts[0])
    return dm, slm


# ---------------------------------------------------------------------------
#  Verdicts
# ---------------------------------------------------------------------------
class Tracker:
    def __init__(self):
        self.verdicts: List[Dict] = []

    def v(self, name: str, passed: bool, detail: str = ""):
        self.verdicts.append({"name": name, "passed": passed, "detail": detail})
        tag = "PASS" if passed else "FAIL"
        print(f"  [{tag}] {name}", flush=True)
        if detail and not passed:
            print(f"         {detail}", flush=True)

    @property
    def passed(self) -> int:
        return sum(1 for v in self.verdicts if v["passed"])

    @property
    def failed(self) -> int:
        return sum(1 for v in self.verdicts if not v["passed"])

    def summary(self) -> str:
        lines = []
        for v in self.verdicts:
            tag = "PASS" if v["passed"] else "FAIL"
            lines.append(f"  [{tag}] {v['name']}")
            if v["detail"] and not v["passed"]:
                lines.append(f"         {v['detail']}")
        lines.append(f"\n  Passed: {self.passed}  Failed: {self.failed}  Total: {len(self.verdicts)}")
        return "\n".join(lines)


def banner(title: str):
    sep = "=" * 78
    print(f"\n{sep}\n  {title}\n{sep}", flush=True)


# ---------------------------------------------------------------------------
#  Main
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="SW-239791: Y.1731 SNMP Scale — Happy Flow")
    parser.add_argument("--host", required=True, help="DUT IP address")
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument("--password", default=DEFAULT_PASS)
    parser.add_argument("--community", default="public", help="SNMP community string")
    args = parser.parse_args()

    start = datetime.now(timezone.utc)
    t = Tracker()

    print(f"\n{'='*78}")
    print(f"  SW-239791: Ethernet OAM Y.1731 | SNMP Scale")
    print(f"  DUT: {args.host}  community: {args.community}")
    print(f"  Started: {start.isoformat()}")
    print(f"{'='*78}", flush=True)

    ssh = None

    try:
        # ---- Connect ----
        banner("Connect & Discover")
        ssh, chan = create_shell(args.host, args.user, args.password, "DUT")

        ver_out = run_show(chan, "show system version")
        ver_m = (
            re.search(r"DNOS.*?\[([\d.]+[^\]]*)\]", ver_out)
            or re.search(r"Software Version:\s*(\S+)", ver_out)
            or re.search(r"[Vv]ersion\s*[:\s]*([\d.]+\S*)", ver_out)
        )
        sw_version = ver_m.group(1) if ver_m else "unknown"
        print(f"  Software version: {sw_version}", flush=True)

        dm_sessions, slm_sessions = get_proactive_sessions(chan)
        n_dm, n_slm = len(dm_sessions), len(slm_sessions)
        print(f"  Found {n_dm} DM + {n_slm} SLM proactive sessions", flush=True)
        t.v(f"Sessions discovered: {n_dm} DM + {n_slm} SLM", n_dm > 0 and n_slm > 0)

        if n_dm == 0 and n_slm == 0:
            print("  FATAL: No sessions. Exiting.", flush=True)
            return 1

        # ---- Step 1: Walk all tables, verify completeness ----
        banner("Step 1: Walk All Proactive Tables")
        raw_outputs: Dict[str, Tuple[str, float]] = {}

        for key, info in TABLES.items():
            print(f"  Walking {info['label']}...", flush=True)
            raw, elapsed = snmpwalk(args.host, info["oid"], args.community)
            raw_outputs[key] = (raw, elapsed)
            suffix = int(info["oid"].split(".")[-1])

            if "session" in key:
                expected = n_dm if "dm" in key else n_slm
                found = count_sessions(raw, suffix)
                t.v(f"{info['label']}: {found}/{expected} sessions ({elapsed:.1f}s)", found >= expected)
            else:
                rows = parse_walk(raw)
                t.v(f"{info['label']}: {len(rows)} result rows ({elapsed:.1f}s)", len(rows) > 0)

        # ---- Step 2: Spot-check SNMP vs CLI ----
        banner("Step 2: Spot-check SNMP vs CLI")

        def pick_indices(n: int, count: int = 3) -> List[int]:
            if n <= count:
                return list(range(n))
            step = (n - 1) / (count - 1)
            return [round(i * step) for i in range(count)]

        def find_row(parsed: Dict, md: str, ma: str, prefix: str) -> Optional[Dict]:
            for key, row in parsed.items():
                if not key.startswith(f"{prefix}."):
                    continue
                if (5 in row and extract_val(row[5]) == md and
                        6 in row and extract_val(row[6]) == ma):
                    return row
            return None

        dm_parsed = parse_walk(raw_outputs["proactive_dm_session"][0])
        for idx in pick_indices(n_dm):
            name = dm_sessions[idx]
            cli_out = run_show(chan, f"show {PM_BASE} cfm tests proactive two-way-delay session-name {name} detail", 12)
            cli = parse_cli_detail(cli_out)
            if not cli:
                t.v(f"DM '{name}' CLI parse", False, "No fields")
                continue
            row = find_row(dm_parsed, cli.get("md_name", ""), cli.get("ma_name", ""), "15")
            if not row:
                t.v(f"DM '{name}' SNMP row found", False, f"No row for MA={cli.get('ma_name')}")
                continue
            ok = 0
            total = 0
            misses = []
            for col, field, label in [(6, "ma_name", "MA"), (10, "source_interface", "Iface"), (13, "count", "Count"), (14, "interval", "Interval")]:
                if col in row:
                    total += 1
                    if extract_val(row[col]) == cli.get(field, ""):
                        ok += 1
                    else:
                        misses.append(f"{label}: snmp={extract_val(row[col])} cli={cli.get(field)}")
            t.v(f"DM '{name}': {ok}/{total} fields match", ok == total, "; ".join(misses))

        slm_parsed = parse_walk(raw_outputs["proactive_slm_session"][0])
        for idx in pick_indices(n_slm):
            name = slm_sessions[idx]
            cli_out = run_show(chan, f"show {PM_BASE} cfm tests proactive two-way-synthetic-loss session-name {name} detail", 12)
            cli = parse_cli_detail(cli_out)
            if not cli:
                t.v(f"SLM '{name}' CLI parse", False, "No fields")
                continue
            row = find_row(slm_parsed, cli.get("md_name", ""), cli.get("ma_name", ""), "17")
            if not row:
                t.v(f"SLM '{name}' SNMP row found", False, f"No row for MA={cli.get('ma_name')}")
                continue
            ok = 0
            total = 0
            misses = []
            for col, field, label in [(6, "ma_name", "MA"), (10, "source_interface", "Iface")]:
                if col in row:
                    total += 1
                    if extract_val(row[col]) == cli.get(field, ""):
                        ok += 1
                    else:
                        misses.append(f"{label}: snmp={extract_val(row[col])} cli={cli.get(field)}")
            t.v(f"SLM '{name}': {ok}/{total} fields match", ok == total, "; ".join(misses))

        # ---- Step 3: Duplicate OID check ----
        banner("Step 3: Duplicate OID Check")
        for key, (raw, _) in raw_outputs.items():
            total, dups = find_duplicates(raw)
            t.v(
                f"{TABLES[key]['label']}: {total} OIDs, {len(dups)} duplicates",
                len(dups) == 0,
                f"Duplicates: {', '.join(dups[:3])}" if dups else "",
            )

        # ---- Step 4: Verify agent + sessions still healthy ----
        banner("Step 4: Post-test Health")
        sysname_raw, sysname_t = snmpwalk(args.host, "1.3.6.1.2.1.1.5", args.community, per_oid_timeout=5, total_timeout=15)
        t.v(f"SNMP agent responsive ({sysname_t:.1f}s)", "STRING:" in sysname_raw)

        dm_post, slm_post = get_proactive_sessions(chan)
        t.v(f"Sessions intact: {len(dm_post)} DM + {len(slm_post)} SLM", len(dm_post) >= n_dm and len(slm_post) >= n_slm)

    finally:
        if ssh:
            print("\nClosing SSH...", flush=True)
            try:
                ssh.close()
            except Exception:
                pass

    end = datetime.now(timezone.utc)
    elapsed = (end - start).total_seconds()

    print(f"\n{'='*78}")
    print(f"  SW-239791 SNMP SCALE TEST RESULTS")
    print(f"{'='*78}")
    print(f"  Duration: {elapsed:.0f}s ({elapsed/60:.1f} min)")
    print(f"  DUT:      {args.host} (v{sw_version})")
    print(f"  Scale:    {n_dm} DM + {n_slm} SLM sessions")
    print(f"{'='*78}")
    print(t.summary(), flush=True)

    overall = "PASS" if t.failed == 0 else "FAIL"
    print(f"\n  OVERALL: {overall}", flush=True)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(OUTPUT_DIR, "sw239791_snmp_scale_results.json")
    with open(out_path, "w") as f:
        json.dump({
            "ticket": "SW-239791",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "duration_s": round(elapsed, 1),
            "host": args.host,
            "sw_version": sw_version,
            "community": args.community,
            "dm_sessions": n_dm,
            "slm_sessions": n_slm,
            "overall": overall,
            "pass_count": t.passed,
            "fail_count": t.failed,
            "verdicts": t.verdicts,
        }, f, indent=2)
    print(f"\n  Results saved to {out_path}")
    print(f"\n{'='*78}\n  TEST COMPLETED\n{'='*78}", flush=True)

    return 0 if t.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
