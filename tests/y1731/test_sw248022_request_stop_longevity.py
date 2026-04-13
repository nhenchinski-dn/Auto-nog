#!/usr/bin/env python3
"""
SW-248022: Ethernet OAM Y.1731 | Request Stop Longevity

Repeatedly starts on-demand Y.1731 sessions (DM, SLM, LB, LT) and stops
them using 'request ethernet-oam cfm on-demand stop all' for many cycles.
Monitors for crashes, session leaks, memory growth, and latency degradation.

Usage:
    python3 test_sw248022_request_stop_longevity.py --host 100.64.3.48

    # Quick sanity
    python3 test_sw248022_request_stop_longevity.py --host 100.64.3.48 --cycles 30

    # Overnight
    python3 test_sw248022_request_stop_longevity.py --host 100.64.3.48 --cycles 500
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import paramiko

sys.stdout.reconfigure(line_buffering=True)

DEFAULT_USER = "dnroot"
DEFAULT_PASS = "dnroot"
OUTPUT_DIR = "/home/dn/output"
MEMORY_GROWTH_WARN_PCT = 20

ANSI_RE = re.compile(
    r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07|\x1b[()][AB012]"
    r"|\x1b\[\?[0-9;]*[hlm]|\r"
)
CLI_ERROR_RE = re.compile(
    r"error:|unknown\s+command|invalid command|command\s+failed|syntax\s+error",
    re.IGNORECASE,
)

ON_DEMAND_TYPES = ["DM", "SLM", "LB", "LT"]
SHOW_OD = "show services performance-monitoring cfm tests on-demand | no-more"
STOP_ALL = "request ethernet-oam cfm on-demand stop all"


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
    transport = ssh.get_transport()
    if transport:
        transport.set_keepalive(30)
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


def send_async(chan: paramiko.Channel, cmd: str):
    chan.send(cmd + "\n")


def drain(chan: paramiko.Channel, wait: float = 2) -> str:
    time.sleep(wait)
    out = b""
    while chan.recv_ready():
        out += chan.recv(65535)
    return clean_ansi(out.decode(errors="replace"))


def build_run_cmds(md: str, ma: str, target: str) -> Dict[str, str]:
    base = f"maintenance-domain {md} maintenance-association {ma} target {target}"
    return {
        "DM":  f"run ethernet-oam cfm on-demand delay-measurement two-way {base}",
        "SLM": f"run ethernet-oam cfm on-demand synthetic-loss-measurement {base}",
        "LB":  f"run ethernet-oam cfm on-demand loopback {base} count 20",
        "LT":  f"run ethernet-oam cfm on-demand linktrace {base}",
    }


def discover_cfm_context(chan: paramiko.Channel) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    out = send(chan,
               "show config services ethernet-oam connectivity-fault-management "
               "| display-set | no-more", wait=15)
    if CLI_ERROR_RE.search(out) or "maintenance" not in out.lower():
        out = send(chan,
                   "show configuration services ethernet-oam connectivity-fault-management "
                   "| display-set | no-more", wait=15)
    if "maintenance" not in out.lower():
        return None, None, None

    md_re = re.compile(r"maintenance[-_]domain(?:s)?(?:[-_]name)?\s+(\S+)", re.IGNORECASE)
    ma_re = re.compile(r"maintenance[-_]association(?:s)?(?:[-_]name)?\s+(\S+)", re.IGNORECASE)
    remote_mep_re = re.compile(r"remote[-_]mep(?:s)?(?:[-_]id)?\s+(\d+)", re.IGNORECASE)
    mep_id_re = re.compile(r"\bmep[-_]id\s+(\d+)", re.IGNORECASE)

    mds, mas, meps, remote_meps = set(), set(), set(), set()
    for line in out.splitlines():
        for m in md_re.finditer(line):
            mds.add(m.group(1))
        for m in ma_re.finditer(line):
            mas.add(m.group(1))
        is_remote = "remote-mep" in line.lower() or "crosscheck" in line.lower()
        for m in remote_mep_re.finditer(line):
            remote_meps.add(m.group(1))
        if is_remote:
            for m in mep_id_re.finditer(line):
                remote_meps.add(m.group(1))
        else:
            for m in mep_id_re.finditer(line):
                meps.add(m.group(1))

    md = sorted(mds)[0] if mds else None
    ma = sorted(mas)[0] if mas else None
    target_mep = sorted(remote_meps)[0] if remote_meps else (
        sorted(meps)[1] if len(meps) >= 2 else None
    )
    return md, ma, target_mep


def count_ongoing_on_demand(show_output: str) -> int:
    """Count on-demand sessions with 'Ongoing' status (truly active, not stopped/invalid)."""
    return show_output.lower().count("ongoing")


def get_cfm_mgr_memory(chan: paramiko.Channel) -> Optional[int]:
    out = send(chan,
               "run bash cat /proc/$(pgrep -f cfm_mgr | head -1)/status 2>/dev/null "
               "| grep VmRSS || echo NO_PROC", wait=3)
    m = re.search(r"VmRSS:\s+(\d+)\s+kB", out)
    return int(m.group(1)) if m else None


def get_core_dumps(chan: paramiko.Channel) -> set:
    out = send(chan, "run bash ls /var/core/core-cfm* 2>/dev/null || echo NONE", wait=3)
    return set(re.findall(r"core-cfm\S+", out))


def section(title: str):
    sep = "=" * 78
    print(f"\n{sep}\n  {title}\n{sep}", flush=True)


def percentile(sorted_list: list, p: float) -> float:
    if not sorted_list:
        return 0.0
    k = (len(sorted_list) - 1) * (p / 100.0)
    f = int(k)
    c = f + 1
    if c >= len(sorted_list):
        return sorted_list[-1]
    return sorted_list[f] + (k - f) * (sorted_list[c] - sorted_list[f])


def main() -> int:
    parser = argparse.ArgumentParser(
        description="SW-248022: Y.1731 Request Stop Longevity",
    )
    parser.add_argument("--host", required=True, help="Device IP")
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument("--password", default=DEFAULT_PASS)
    parser.add_argument("--cycles", type=int, default=200,
                        help="Number of start/stop cycles (default 200)")
    parser.add_argument("--health-interval", type=int, default=25,
                        help="Health check every N cycles (default 25)")
    parser.add_argument("--settle-time", type=int, default=3,
                        help="Seconds to wait after start before stop (default 3)")
    parser.add_argument("--log-file", default=None,
                        help="File to write raw CLI output (default: output/sw248022_cli.log)")
    parser.add_argument("--md", default=None)
    parser.add_argument("--ma", default=None)
    parser.add_argument("--target", default=None, help="e.g. 'mep-id 2'")
    args = parser.parse_args()

    if args.log_file is None:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        args.log_file = os.path.join(OUTPUT_DIR, "sw248022_cli.log")

    start_time = datetime.now(timezone.utc)
    print(f"{'=' * 78}", flush=True)
    print(f"  SW-248022: Y.1731 Request Stop Longevity", flush=True)
    print(f"  Host:    {args.host}", flush=True)
    print(f"  Cycles:  {args.cycles}", flush=True)
    print(f"  Started: {start_time.strftime('%Y-%m-%d %H:%M:%S UTC')}", flush=True)
    print(f"{'=' * 78}", flush=True)

    log_fh = open(args.log_file, "w", encoding="utf-8")
    print(f"  CLI output logging to: {args.log_file}", flush=True)

    def log(label: str, output: str):
        log_fh.write(f"\n{'='*60}\n[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {label}\n{'='*60}\n")
        log_fh.write(output + "\n")
        log_fh.flush()

    ssh1, chan1 = create_shell(args.host, args.user, args.password, "S1-start")
    ssh2, chan2 = create_shell(args.host, args.user, args.password, "S2-stop")

    verdicts: List[Dict] = []
    cycle_results: List[Dict] = []
    memory_snapshots: List[Dict] = []
    errors: List[Dict] = []
    stop_latencies: List[float] = []
    ongoing_after_stop = 0
    cores_baseline: set = set()
    baseline_mem: Optional[int] = None

    def verdict(name: str, passed: bool, detail: str = ""):
        tag = "PASS" if passed else "FAIL"
        verdicts.append({"name": name, "passed": passed, "detail": detail})
        print(f"  [{tag}] {name}", flush=True)
        if detail:
            print(f"         {detail}", flush=True)

    try:
        # -- PRE-CHECKS --
        section("PRE-CHECK 1: Software version")
        ver = send(chan2, "show system version | no-more", wait=5)
        log("show system version", ver)
        print(ver[:600], flush=True)

        section("PRE-CHECK 2: Discover CFM context")
        if args.md and args.ma and args.target:
            md, ma, target = args.md, args.ma, args.target
        else:
            d_md, d_ma, d_target = discover_cfm_context(chan2)
            md = args.md or d_md or "MD-CUST"
            ma = args.ma or d_ma or "MA-CUST"
            target = args.target or (f"mep-id {d_target}" if d_target else "mep-id 2")
        print(f"  md={md}  ma={ma}  target={target}", flush=True)
        verdict("CFM context resolved", bool(md and ma and target))

        run_cmds = build_run_cmds(md, ma, target)

        section("PRE-CHECK 3: cfm_mgr baseline memory")
        baseline_mem = get_cfm_mgr_memory(chan2)
        if baseline_mem:
            memory_snapshots.append({"cycle": 0, "rss_kb": baseline_mem})
        print(f"  cfm_mgr VmRSS: {baseline_mem} kB" if baseline_mem else
              "  cfm_mgr memory: unavailable", flush=True)

        section("PRE-CHECK 4: Baseline core dumps")
        cores_baseline = get_core_dumps(chan2)
        print(f"  Existing cfm cores: {len(cores_baseline)}", flush=True)

        section("PRE-CHECK 5: Clean on-demand state")
        pre_od = send(chan2, SHOW_OD, wait=5)
        log("PRE-CHECK show on-demand", pre_od)
        if count_ongoing_on_demand(pre_od) > 0:
            print("  Cleaning active sessions...", flush=True)
            send(chan2, STOP_ALL, wait=5)
            time.sleep(3)
        print("  On-demand state: clean", flush=True)

        # -- LONGEVITY LOOP --
        section(f"LONGEVITY: {args.cycles} start/stop cycles")

        for cycle in range(1, args.cycles + 1):
            tt = ON_DEMAND_TYPES[(cycle - 1) % len(ON_DEMAND_TYPES)]

            send_async(chan1, run_cmds[tt])
            time.sleep(args.settle_time)

            t0 = time.time()
            stop_out = send(chan2, STOP_ALL, wait=8)
            elapsed = time.time() - t0
            stop_latencies.append(elapsed)

            err_found = bool(CLI_ERROR_RE.search(stop_out))
            status = "ERR" if err_found else "OK"
            if err_found:
                errors.append({"cycle": cycle, "desc": f"CLI error on stop ({tt})"})
                log(f"Cycle {cycle} CLI error ({tt})", stop_out)

            drain(chan1, wait=1)

            print(f"  Cycle {cycle:>4}/{args.cycles} [{tt:>3}] {elapsed:5.1f}s {status}",
                  flush=True)
            cycle_results.append({
                "cycle": cycle, "test_type": tt,
                "latency_s": round(elapsed, 2), "status": status,
            })

            # -- PERIODIC HEALTH CHECK --
            if cycle % args.health_interval == 0:
                print(f"\n    --- Health check at cycle {cycle} ---", flush=True)

                od_out = send(chan2, SHOW_OD, wait=5)
                log(f"Health check cycle {cycle} show on-demand", od_out)
                active = count_ongoing_on_demand(od_out)
                if active > 0:
                    ongoing_after_stop += 1
                    errors.append({"cycle": cycle,
                                   "desc": f"Active on-demand sessions after stop: {active}"})
                    send(chan2, STOP_ALL, wait=5)
                    print(f"    ACTIVE SESSIONS after stop: {active} — re-stopped", flush=True)
                else:
                    print(f"    On-demand: no active sessions (OK)", flush=True)

                mem = get_cfm_mgr_memory(chan2)
                if mem:
                    memory_snapshots.append({"cycle": cycle, "rss_kb": mem})
                    growth = ""
                    if baseline_mem and baseline_mem > 0:
                        pct = ((mem - baseline_mem) / baseline_mem) * 100
                        growth = f" ({pct:+.1f}%)"
                    print(f"    cfm_mgr VmRSS: {mem} kB{growth}", flush=True)
                else:
                    errors.append({"cycle": cycle, "desc": "cfm_mgr process not found"})
                    print(f"    cfm_mgr: PROCESS NOT FOUND", flush=True)

                recent = stop_latencies[-args.health_interval:]
                print(f"    Stop latency (last {len(recent)}): "
                      f"avg={sum(recent)/len(recent):.1f}s max={max(recent):.1f}s", flush=True)
                print(f"    Errors so far: {len(errors)}\n", flush=True)

        # -- POST-CHECKS --
        section("POST-CHECK 1: No active on-demand sessions")
        send(chan2, STOP_ALL, wait=5)
        time.sleep(3)
        final_od = send(chan2, SHOW_OD, wait=5)
        log("POST-CHECK show on-demand", final_od)
        final_active = count_ongoing_on_demand(final_od)
        verdict("No active on-demand sessions after final stop",
                final_active == 0, f"{final_active} ongoing" if final_active else "")

        section("POST-CHECK 2: On-demand still works")
        send_async(chan1, run_cmds["DM"])
        time.sleep(args.settle_time)
        verify_od = send(chan2, SHOW_OD, wait=5)
        log("POST-CHECK verify DM start", verify_od)
        dm_visible = "delay-measurement" in verify_od.lower()
        verdict("DM starts after longevity", dm_visible)
        send(chan2, STOP_ALL, wait=5)
        drain(chan1, wait=1)

        section("POST-CHECK 3: cfm_mgr memory")
        final_mem = get_cfm_mgr_memory(chan2)
        if final_mem and baseline_mem and baseline_mem > 0:
            memory_snapshots.append({"cycle": args.cycles, "rss_kb": final_mem})
            growth_pct = ((final_mem - baseline_mem) / baseline_mem) * 100
            print(f"  {baseline_mem} kB -> {final_mem} kB ({growth_pct:+.1f}%)", flush=True)
            verdict("Memory growth within limits", growth_pct <= MEMORY_GROWTH_WARN_PCT,
                    f"{growth_pct:+.1f}% (threshold {MEMORY_GROWTH_WARN_PCT}%)")
        elif final_mem:
            verdict("cfm_mgr memory (no baseline)", True, f"{final_mem} kB")
        else:
            verdict("cfm_mgr memory", False, "process not found")

        section("POST-CHECK 4: Core dumps")
        new_cores = get_core_dumps(chan2) - cores_baseline
        print(f"  New cfm cores: {len(new_cores)}", flush=True)
        verdict("No new cfm core dumps", len(new_cores) == 0,
                f"{new_cores}" if new_cores else "")

        section("POST-CHECK 5: Stop latency stats")
        if stop_latencies:
            s = sorted(stop_latencies)
            avg = sum(s) / len(s)
            p95 = percentile(s, 95)
            print(f"  Min: {s[0]:.2f}s  Avg: {avg:.2f}s  P95: {p95:.2f}s  Max: {s[-1]:.2f}s",
                  flush=True)

        verdict("No CLI errors across all cycles", len(errors) == 0,
                f"{len(errors)} error(s)" if errors else "")
        verdict("No sessions stuck in Ongoing state", ongoing_after_stop == 0,
                f"{ongoing_after_stop} detection(s)" if ongoing_after_stop else "")

    finally:
        for s in [ssh1, ssh2]:
            try:
                s.close()
            except Exception:
                pass
        log_fh.close()

    # -- SUMMARY --
    end_time = datetime.now(timezone.utc)
    elapsed_total = (end_time - start_time).total_seconds()
    pass_count = sum(1 for v in verdicts if v["passed"])
    fail_count = sum(1 for v in verdicts if not v["passed"])
    overall = "PASS" if fail_count == 0 else "FAIL"

    print(f"\n{'=' * 78}", flush=True)
    print(f"  SW-248022 REQUEST STOP LONGEVITY — {overall}", flush=True)
    print(f"{'=' * 78}", flush=True)
    print(f"  Duration: {elapsed_total:.0f}s ({elapsed_total/3600:.1f}h)", flush=True)
    print(f"  Cycles:   {len(cycle_results)}/{args.cycles}", flush=True)
    print(f"  Device:   {args.host}", flush=True)
    print(f"{'=' * 78}", flush=True)
    for v in verdicts:
        tag = "PASS" if v["passed"] else "FAIL"
        print(f"  [{tag}] {v['name']}", flush=True)
        if v["detail"] and not v["passed"]:
            print(f"         {v['detail']}", flush=True)
    print(f"\n  Passed: {pass_count}  Failed: {fail_count}", flush=True)

    if errors:
        print(f"\n  First 20 errors:", flush=True)
        for e in errors[:20]:
            print(f"    Cycle {e['cycle']}: {e['desc']}", flush=True)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(OUTPUT_DIR, "sw248022_request_stop_longevity_results.json")
    payload = {
        "ticket": "SW-248022",
        "start": start_time.isoformat(),
        "end": end_time.isoformat(),
        "duration_s": round(elapsed_total, 1),
        "host": args.host,
        "total_cycles": args.cycles,
        "completed_cycles": len(cycle_results),
        "overall": overall,
        "verdicts": verdicts,
        "memory_snapshots": memory_snapshots,
        "latency_stats": {
            "min": round(sorted(stop_latencies)[0], 3),
            "avg": round(sum(stop_latencies) / len(stop_latencies), 3),
            "p95": round(percentile(sorted(stop_latencies), 95), 3),
            "max": round(sorted(stop_latencies)[-1], 3),
        } if stop_latencies else {},
        "ongoing_after_stop_detections": ongoing_after_stop,
        "errors": errors,
        "cli_log": args.log_file,
        "cycles": cycle_results,
    }
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"\n  Results saved to {out_path}", flush=True)
    print(f"{'=' * 78}", flush=True)

    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
