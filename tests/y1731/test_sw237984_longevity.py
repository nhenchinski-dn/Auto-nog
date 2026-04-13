#!/usr/bin/env python3
"""
SW-237984: Stop command longevity / endurance test.

Repeatedly starts on-demand Y.1731 sessions (DM, SLM, LB, LT) and stops
them using rotating stop-command variants for ~500 cycles.  Monitors for
crashes, session leaks, memory growth, and latency degradation.

Usage:
    python3 test_sw237984_longevity.py
"""
import paramiko
import time
import re
import json
import sys
import os
from datetime import datetime, timezone

sys.stdout.reconfigure(line_buffering=True)

DEVICE_IP = "100.64.3.48"
USERNAME = "dnroot"
PASSWORD = "dnroot"
TOTAL_CYCLES = 500
SETTLE_TIME = 3
HEALTH_CHECK_INTERVAL = 25
STOP_LATENCY_THRESHOLD = 30
MEMORY_GROWTH_WARN_PCT = 20

# -- On-demand run commands (persistent types preferred for reliable stop) --
RUN_CMDS = {
    "DM":  "run ethernet-oam cfm on-demand delay-measurement two-way "
           "maintenance-domain MD-CUST maintenance-association MA-CUST target mep-id 2",
    "SLM": "run ethernet-oam cfm on-demand synthetic-loss-measurement two-way "
           "maintenance-domain MD-CUST maintenance-association MA-CUST target mep-id 2",
    "LB":  "run ethernet-oam cfm on-demand loopback "
           "maintenance-domain MD-CUST maintenance-association MA-CUST target mep-id 2 count 50",
    "LT":  "run ethernet-oam cfm on-demand linktrace "
           "maintenance-domain MD-CUST maintenance-association MA-CUST target mep-id 2",
}

SHOW_OD = "show services performance-monitoring cfm tests on-demand | no-more"

STOP_VARIANTS = [
    ("stop_all",     "request ethernet-oam cfm on-demand stop all"),
    ("stop_dm_md",   "request ethernet-oam cfm on-demand stop maintenance-domain MD-CUST "
                     "maintenance-association MA-CUST test-type two-way-delay-measurement"),
    ("stop_slm_md",  "request ethernet-oam cfm on-demand stop maintenance-domain MD-CUST "
                     "maintenance-association MA-CUST test-type two-way-synthetic-loss-measurement"),
    ("stop_lb_md",   "request ethernet-oam cfm on-demand stop maintenance-domain MD-CUST "
                     "maintenance-association MA-CUST test-type loopback"),
    ("stop_lt_md",   "request ethernet-oam cfm on-demand stop maintenance-domain MD-CUST "
                     "maintenance-association MA-CUST test-type linktrace"),
    ("stop_dm_type", "request ethernet-oam cfm on-demand stop test-type two-way-delay-measurement"),
    ("stop_slm_type","request ethernet-oam cfm on-demand stop test-type two-way-synthetic-loss-measurement"),
    ("stop_lb_type", "request ethernet-oam cfm on-demand stop test-type loopback"),
    ("stop_lt_type", "request ethernet-oam cfm on-demand stop test-type linktrace"),
]

# Map test types to their matching stop variants (type-specific ones + stop_all)
TYPE_STOP_MAP = {
    "DM":  [0, 1, 5],   # stop_all, stop_dm_md, stop_dm_type
    "SLM": [0, 2, 6],   # stop_all, stop_slm_md, stop_slm_type
    "LB":  [0, 3, 7],   # stop_all, stop_lb_md, stop_lb_type
    "LT":  [0, 4, 8],   # stop_all, stop_lt_md, stop_lt_type
}

ANSI_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07|\x1b[()][AB012]|\x1b\[\?[0-9;]*[hlm]|\r')
CLI_ERROR_RE = re.compile(
    r'(Error:|ERROR:|Unknown command|Invalid command|Command failed|rpc-error)',
    re.IGNORECASE,
)


def clean_ansi(text):
    return ANSI_RE.sub('', text).strip()


def create_shell(ip, username, password, label=""):
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(ip, username=username, password=password, timeout=30,
                look_for_keys=False, allow_agent=False)
    chan = ssh.invoke_shell(width=300, height=1000)
    time.sleep(5)
    chan.recv(65535)
    print(f"  [SSH {label}] Connected", flush=True)
    return ssh, chan


def run_cmd(chan, cmd, wait=10):
    chan.send(cmd + '\n')
    time.sleep(wait)
    out = b''
    while chan.recv_ready():
        out += chan.recv(65535)
    return clean_ansi(out.decode(errors='replace'))


def run_cmd_timed(chan, cmd, wait=10):
    """Run a command and return (output, elapsed_seconds)."""
    t0 = time.time()
    out = run_cmd(chan, cmd, wait=wait)
    return out, time.time() - t0


def run_cmd_async(chan, cmd):
    chan.send(cmd + '\n')


def drain_channel(chan, wait=2):
    time.sleep(wait)
    out = b''
    while chan.recv_ready():
        out += chan.recv(65535)
    return clean_ansi(out.decode(errors='replace'))


def has_cli_error(text):
    for line in text.splitlines():
        if CLI_ERROR_RE.search(line):
            return True, line.strip()
    return False, ""


def has_stale_sessions(show_output):
    """Return True if show on-demand output indicates active/stale sessions."""
    clean = clean_ansi(show_output).lower()
    if "no ongoing" in clean or "no on-demand" in clean:
        return False
    active_indicators = ["ongoing", "running", "in-progress", "delay-measurement",
                         "synthetic-loss", "loopback", "linktrace"]
    for indicator in active_indicators:
        if indicator in clean:
            lines = [l for l in clean.splitlines()
                     if indicator in l and "stopped" not in l and "invalid" not in l]
            if lines:
                return True
    return False


def get_cfm_mgr_memory(chan):
    """Return cfm_mgr VmRSS in kB, or None if unavailable."""
    out = run_cmd(chan,
                  "run bash cat /proc/$(pgrep -f cfm_mgr | head -1)/status 2>/dev/null "
                  "| grep VmRSS || echo NO_PROC",
                  wait=3)
    m = re.search(r'VmRSS:\s+(\d+)\s+kB', out)
    return int(m.group(1)) if m else None


def get_core_dumps(chan):
    """Return set of cfm-related core dump filenames."""
    out = run_cmd(chan, "run bash ls /var/core/core-cfm* 2>/dev/null || echo NONE", wait=3)
    return set(re.findall(r'core-cfm\S+', out))


def section(title):
    sep = '=' * 70
    print(f"\n{sep}\n  {title}\n{sep}", flush=True)


def percentile(sorted_list, p):
    if not sorted_list:
        return 0.0
    k = (len(sorted_list) - 1) * (p / 100.0)
    f = int(k)
    c = f + 1
    if c >= len(sorted_list):
        return sorted_list[-1]
    return sorted_list[f] + (k - f) * (sorted_list[c] - sorted_list[f])


# ======================================================================
#  MAIN
# ======================================================================
def main():
    start_ts = datetime.now(timezone.utc)
    print(f"=== SW-237984 Stop Command Longevity Test ===", flush=True)
    print(f"Started: {start_ts.strftime('%Y-%m-%d %H:%M:%S UTC')}", flush=True)
    print(f"Device:  {DEVICE_IP}", flush=True)
    print(f"Cycles:  {TOTAL_CYCLES}", flush=True)
    print(f"Health check every {HEALTH_CHECK_INTERVAL} cycles", flush=True)
    print()

    results = []          # list of per-cycle dicts
    verdicts = []         # (name, passed, detail)
    stop_latencies = []
    memory_snapshots = [] # (cycle, rss_kb)
    errors = []           # (cycle, description)

    def verdict(name, passed, detail=""):
        tag = "PASS" if passed else "FAIL"
        verdicts.append((name, passed, detail))
        print(f"  [{tag}] {name}", flush=True)
        if detail:
            print(f"         {detail}", flush=True)

    # -- Connect --
    section("Establishing SSH sessions")
    ssh1, chan1 = create_shell(DEVICE_IP, USERNAME, PASSWORD, "S1-start")
    ssh2, chan2 = create_shell(DEVICE_IP, USERNAME, PASSWORD, "S2-stop")
    ssh3, chan3 = create_shell(DEVICE_IP, USERNAME, PASSWORD, "S3-ctrl")

    try:
        # ==============================================================
        #  PRE-CHECKS
        # ==============================================================
        section("PRE-CHECK 1: Software version")
        ver = run_cmd(chan3, "show system version | no-more", wait=5)
        print(ver[:600], flush=True)

        section("PRE-CHECK 2: Baseline on-demand sessions")
        pre_show = run_cmd(chan3, SHOW_OD, wait=5)
        print(pre_show, flush=True)
        pre_clean = not has_stale_sessions(pre_show)
        if not pre_clean:
            print("  Cleaning stale sessions before starting...", flush=True)
            run_cmd(chan2, "request ethernet-oam cfm on-demand stop all", wait=5)
            time.sleep(3)
            pre_show = run_cmd(chan3, SHOW_OD, wait=5)
            pre_clean = not has_stale_sessions(pre_show)
        verdict("Baseline clean (no stale sessions)", pre_clean)

        section("PRE-CHECK 3: cfm_mgr baseline memory")
        baseline_mem = get_cfm_mgr_memory(chan3)
        if baseline_mem:
            print(f"  cfm_mgr VmRSS: {baseline_mem} kB", flush=True)
            memory_snapshots.append((0, baseline_mem))
        else:
            print("  cfm_mgr memory not available (process not found or no access)", flush=True)
        verdict("cfm_mgr baseline memory captured", baseline_mem is not None,
                f"{baseline_mem} kB" if baseline_mem else "unavailable")

        section("PRE-CHECK 4: Baseline core dumps")
        cores_before = get_core_dumps(chan3)
        print(f"  Existing cfm cores: {len(cores_before)}", flush=True)

        # ==============================================================
        #  LONGEVITY LOOP
        # ==============================================================
        section(f"LONGEVITY: {TOTAL_CYCLES} start/stop cycles")

        test_types = list(RUN_CMDS.keys())  # DM, SLM, LB, LT
        cycle_errors = 0
        cycle_stale = 0

        for cycle in range(1, TOTAL_CYCLES + 1):
            tt_idx = (cycle - 1) % len(test_types)
            test_type = test_types[tt_idx]
            run_cmd_str = RUN_CMDS[test_type]

            matching_stops = TYPE_STOP_MAP[test_type]
            stop_idx = matching_stops[(cycle - 1) // len(test_types) % len(matching_stops)]
            stop_label, stop_cmd_str = STOP_VARIANTS[stop_idx]

            # 1) Start on-demand session
            run_cmd_async(chan1, run_cmd_str)
            time.sleep(SETTLE_TIME)

            # 2) Stop and measure latency
            stop_out, elapsed = run_cmd_timed(chan2, stop_cmd_str, wait=8)

            stop_latencies.append(elapsed)

            # 3) Check for CLI errors in stop output
            err_found, err_line = has_cli_error(stop_out)
            status = "OK"
            if err_found:
                status = "ERR"
                cycle_errors += 1
                errors.append((cycle, f"CLI error on {stop_label}: {err_line}"))
            elif elapsed > STOP_LATENCY_THRESHOLD:
                status = "SLOW"
                errors.append((cycle, f"Stop latency {elapsed:.1f}s > {STOP_LATENCY_THRESHOLD}s"))

            # 4) Drain the start channel
            drain_channel(chan1, wait=1)

            # Progress line
            print(f"  Cycle {cycle:>4}/{TOTAL_CYCLES} [{test_type:>3}, {stop_label:<14}] "
                  f"{elapsed:5.1f}s {status}", flush=True)

            # 5) Record
            results.append({
                "cycle": cycle,
                "test_type": test_type,
                "stop_variant": stop_label,
                "latency_s": round(elapsed, 2),
                "cli_error": err_found,
                "status": status,
            })

            # ===========================================================
            #  PERIODIC HEALTH CHECK
            # ===========================================================
            if cycle % HEALTH_CHECK_INTERVAL == 0:
                print(f"\n  --- Health check at cycle {cycle} ---", flush=True)

                # Check for stale sessions
                show_out = run_cmd(chan3, SHOW_OD, wait=5)
                stale = has_stale_sessions(show_out)
                if stale:
                    cycle_stale += 1
                    errors.append((cycle, "Stale sessions detected after stop"))
                    print(f"    STALE SESSIONS: {show_out[:200]}", flush=True)
                    # Attempt cleanup
                    run_cmd(chan2, "request ethernet-oam cfm on-demand stop all", wait=5)
                    time.sleep(2)
                    drain_channel(chan1, wait=1)
                else:
                    print(f"    Sessions: clean", flush=True)

                # Memory check
                mem = get_cfm_mgr_memory(chan3)
                if mem:
                    memory_snapshots.append((cycle, mem))
                    growth = ""
                    if baseline_mem and baseline_mem > 0:
                        pct = ((mem - baseline_mem) / baseline_mem) * 100
                        growth = f" ({pct:+.1f}% vs baseline)"
                    print(f"    cfm_mgr VmRSS: {mem} kB{growth}", flush=True)

                # Latency trend
                recent = stop_latencies[-HEALTH_CHECK_INTERVAL:]
                avg_recent = sum(recent) / len(recent)
                max_recent = max(recent)
                print(f"    Last {HEALTH_CHECK_INTERVAL} cycles latency: "
                      f"avg={avg_recent:.1f}s max={max_recent:.1f}s", flush=True)

                print(f"    Errors so far: {cycle_errors}, Stale detections: {cycle_stale}\n",
                      flush=True)

        # ==============================================================
        #  POST-CHECKS
        # ==============================================================
        section("POST-CHECK 1: Final on-demand session state")
        # Make sure everything is stopped
        run_cmd(chan2, "request ethernet-oam cfm on-demand stop all", wait=5)
        time.sleep(3)
        final_show = run_cmd(chan3, SHOW_OD, wait=5)
        print(final_show, flush=True)
        final_clean = not has_stale_sessions(final_show)
        verdict("No stale sessions after longevity", final_clean)

        section("POST-CHECK 2: Feature still works (DM start + stop)")
        run_cmd_async(chan1, RUN_CMDS["DM"])
        time.sleep(SETTLE_TIME)
        verify_show = run_cmd(chan3, SHOW_OD, wait=5)
        dm_visible = "delay-measurement" in verify_show.lower() or "dm" in verify_show.lower()
        verdict("DM session starts after longevity", dm_visible)
        run_cmd(chan2, "request ethernet-oam cfm on-demand stop all", wait=5)
        time.sleep(3)
        drain_channel(chan1, wait=1)
        post_stop_show = run_cmd(chan3, SHOW_OD, wait=5)
        post_stop_clean = not has_stale_sessions(post_stop_show)
        verdict("DM session stops cleanly after longevity", post_stop_clean)

        section("POST-CHECK 3: cfm_mgr final memory")
        final_mem = get_cfm_mgr_memory(chan3)
        if final_mem:
            memory_snapshots.append((TOTAL_CYCLES, final_mem))
            print(f"  cfm_mgr VmRSS: {final_mem} kB", flush=True)
            if baseline_mem and baseline_mem > 0:
                growth_pct = ((final_mem - baseline_mem) / baseline_mem) * 100
                print(f"  Growth from baseline: {growth_pct:+.1f}%", flush=True)
                if growth_pct > MEMORY_GROWTH_WARN_PCT:
                    verdict("cfm_mgr memory growth", False,
                            f"{growth_pct:+.1f}% exceeds {MEMORY_GROWTH_WARN_PCT}% threshold "
                            f"({baseline_mem} -> {final_mem} kB)")
                else:
                    verdict("cfm_mgr memory growth within limits", True,
                            f"{growth_pct:+.1f}% ({baseline_mem} -> {final_mem} kB)")
            else:
                verdict("cfm_mgr memory (no baseline to compare)", True, f"{final_mem} kB")
        else:
            verdict("cfm_mgr final memory", False, "process not found — may have crashed")

        section("POST-CHECK 4: Core dumps")
        cores_after = get_core_dumps(chan3)
        new_cores = cores_after - cores_before
        if new_cores:
            print(f"  NEW cfm core dumps: {new_cores}", flush=True)
        else:
            print(f"  No new cfm core dumps.", flush=True)
        verdict("No new cfm core dumps", len(new_cores) == 0,
                f"new: {new_cores}" if new_cores else "")

        section("POST-CHECK 5: Stop latency statistics")
        if stop_latencies:
            sorted_lat = sorted(stop_latencies)
            lat_min = sorted_lat[0]
            lat_max = sorted_lat[-1]
            lat_avg = sum(sorted_lat) / len(sorted_lat)
            lat_p95 = percentile(sorted_lat, 95)
            lat_p99 = percentile(sorted_lat, 99)
            print(f"  Min:  {lat_min:.2f}s", flush=True)
            print(f"  Avg:  {lat_avg:.2f}s", flush=True)
            print(f"  P95:  {lat_p95:.2f}s", flush=True)
            print(f"  P99:  {lat_p99:.2f}s", flush=True)
            print(f"  Max:  {lat_max:.2f}s", flush=True)
            verdict("Stop latency within threshold",
                    lat_max < STOP_LATENCY_THRESHOLD,
                    f"max={lat_max:.2f}s (threshold={STOP_LATENCY_THRESHOLD}s)")

        # Aggregate verdicts
        verdict("No CLI errors across all cycles",
                cycle_errors == 0,
                f"{cycle_errors} errors in {TOTAL_CYCLES} cycles" if cycle_errors else "")
        verdict("No stale session accumulation",
                cycle_stale == 0,
                f"{cycle_stale} stale detections" if cycle_stale else "")

    finally:
        print("\nClosing SSH sessions...", flush=True)
        for s in [ssh1, ssh2, ssh3]:
            try:
                s.close()
            except Exception:
                pass

    # ==================================================================
    #  SUMMARY
    # ==================================================================
    end_ts = datetime.now(timezone.utc)
    elapsed_total = (end_ts - start_ts).total_seconds()

    print(f"\n{'='*70}", flush=True)
    print(f"  SW-237984 LONGEVITY TEST RESULTS", flush=True)
    print(f"{'='*70}", flush=True)
    print(f"  Duration:  {elapsed_total:.0f}s ({elapsed_total/60:.1f} min)", flush=True)
    print(f"  Cycles:    {TOTAL_CYCLES}", flush=True)
    print(f"  Device:    {DEVICE_IP}", flush=True)
    print(f"{'='*70}", flush=True)
    pass_count = sum(1 for _, p, _ in verdicts if p)
    fail_count = sum(1 for _, p, _ in verdicts if not p)
    for name, passed, detail in verdicts:
        tag = "PASS" if passed else "FAIL"
        print(f"  [{tag}] {name}", flush=True)
        if detail and not passed:
            print(f"         {detail}", flush=True)
    print(f"\n  Passed: {pass_count}, Failed: {fail_count}, Total: {len(verdicts)}", flush=True)

    if fail_count == 0:
        print(f"\n  CONCLUSION: Stop command survived {TOTAL_CYCLES} cycles with no issues.",
              flush=True)
    else:
        print(f"\n  CONCLUSION: Stop command longevity test found {fail_count} issue(s).",
              flush=True)

    if errors:
        print(f"\n  First 20 errors:", flush=True)
        for cyc, desc in errors[:20]:
            print(f"    Cycle {cyc}: {desc}", flush=True)

    # Save results
    out_dir = "/home/dn/output"
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "sw237984_longevity_results.json")
    payload = {
        "start": start_ts.isoformat(),
        "end": end_ts.isoformat(),
        "duration_s": round(elapsed_total, 1),
        "device": DEVICE_IP,
        "total_cycles": TOTAL_CYCLES,
        "verdicts": [{"name": n, "passed": p, "detail": d} for n, p, d in verdicts],
        "memory_snapshots": [{"cycle": c, "rss_kb": m} for c, m in memory_snapshots],
        "latency_stats": {
            "min": round(sorted_lat[0], 3) if stop_latencies else None,
            "avg": round(lat_avg, 3) if stop_latencies else None,
            "p95": round(lat_p95, 3) if stop_latencies else None,
            "p99": round(lat_p99, 3) if stop_latencies else None,
            "max": round(sorted_lat[-1], 3) if stop_latencies else None,
        } if stop_latencies else {},
        "errors": [{"cycle": c, "desc": d} for c, d in errors],
        "cycles": results,
    }
    with open(out_path, 'w') as f:
        json.dump(payload, f, indent=2)
    print(f"\n  Results saved to {out_path}", flush=True)
    print(f"\n{'='*70}\n  TEST COMPLETED\n{'='*70}", flush=True)

    return fail_count == 0


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)
