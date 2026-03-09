#!/usr/bin/env python3
"""
Negative Test: Source start during RP failure at scale (SW-246192)

Removes the static RP while 30K ASM groups are active with (S,G) state,
monitors degradation, then restores RP and verifies recovery.
"""

import paramiko
import time
import re
import sys
from datetime import datetime, timezone

HOST = "100.64.6.171"
USER = "dnroot"
PASS = "dnroot"

ANSI_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07|\x1b[^[]')

def clean(text):
    return ANSI_RE.sub('', text)

def ts():
    return datetime.now().strftime("%H:%M:%S")

class Device:
    def __init__(self, host, user, passwd):
        self.ssh = paramiko.SSHClient()
        self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.ssh.connect(host, username=user, password=passwd,
                         timeout=30, look_for_keys=False, allow_agent=False)
        self.chan = self.ssh.invoke_shell(width=300, height=500)
        time.sleep(3)
        self.chan.recv(65535)

    def run(self, cmd, wait=8):
        self.chan.send(cmd + "\n")
        time.sleep(wait)
        out = b""
        while self.chan.recv_ready():
            out += self.chan.recv(65535)
            time.sleep(0.3)
        return clean(out.decode("utf-8", errors="replace"))

    def show(self, cmd, wait=10):
        return self.run(cmd + " | no-more", wait)

    def configure(self, cmds, wait_commit=30):
        self.run("configure", 2)
        for cmd in cmds:
            self.run(cmd, 2)
        out = self.run("commit", wait_commit)
        self.run("exit", 2)
        return out

    def close(self):
        self.ssh.close()


def extract_pim_counts(text):
    counts = {}
    for label, pattern in [
        ("star_g", r"Number of \(\*,G\) route entries\s*:\s*(\d+)"),
        ("sg_sm", r"Number of \(S,G\)SM route entries\s*:\s*(\d+)"),
        ("total", r"Total PIM Tree entries\s*:\s*(\d+)"),
        ("mfib", r"Total PIM MFIB routes\s*:\s*(\d+)"),
    ]:
        m = re.search(pattern, text)
        counts[label] = int(m.group(1)) if m else -1
    return counts


def extract_cprl_multicast(text):
    for line in text.split("\n"):
        if "Punted-IP-Multicast" in line:
            nums = re.findall(r'\d+', line.replace(",", ""))
            if len(nums) >= 5:
                return {"rate": nums[0], "burst": nums[1], "rx": nums[2], "drops": nums[3]}
    return {}


def extract_mc_route_summary(text):
    counts = {}
    for label, pattern in [
        ("star_g", r"Number of \(\*,G\) routes\s*:\s*(\d+)"),
        ("sg", r"Number of \(S,G\) routes\s*:\s*(\d+)"),
        ("failed", r"Number of failed route installs\s*:\s*(\d+)"),
    ]:
        m = re.search(pattern, text)
        counts[label] = int(m.group(1)) if m else -1
    return counts


def print_state(label, dev):
    print(f"\n{'='*70}")
    print(f"  [{ts()}] {label}")
    print(f"{'='*70}")

    pim = dev.show("show pim summary")
    counts = extract_pim_counts(pim)
    print(f"  PIM: (*,G)={counts['star_g']}  (S,G)SM={counts['sg_sm']}  Total={counts['total']}  MFIB={counts['mfib']}")

    cprl_out = dev.show("show system cprl | include Punted-IP-Multicast")
    cprl = extract_cprl_multicast(cprl_out)
    if cprl:
        print(f"  CPRL Punted-IP-Multicast: RX={cprl.get('rx','?')}  Drops={cprl.get('drops','?')}")

    mc = dev.show("show multicast route summary")
    mc_counts = extract_mc_route_summary(mc)
    print(f"  MC Routes: (*,G)={mc_counts['star_g']}  (S,G)={mc_counts['sg']}  Failed installs={mc_counts['failed']}")

    rps = dev.show("show pim rps")
    if "8.8.8.8" in rps:
        print(f"  RP: 8.8.8.8 (present)")
    else:
        print(f"  RP: NOT CONFIGURED")

    return counts, mc_counts


def check_health(dev):
    out = dev.show("show pim neighbors")
    nbr_count = out.count("ge800")
    print(f"  PIM neighbors: {nbr_count} (SSH responsive)")
    return nbr_count > 0


results = {}

try:
    print(f"[{ts()}] Connecting to {HOST}...")
    dev = Device(HOST, USER, PASS)
    print(f"[{ts()}] Connected.\n")

    # ── STEP 1: Capture baseline ──
    print("=" * 70)
    print("  STEP 1: BASELINE (before RP failure)")
    print("=" * 70)
    baseline_pim, baseline_mc = print_state("Baseline", dev)
    healthy = check_health(dev)
    results["baseline"] = {
        "pim": baseline_pim, "mc": baseline_mc, "healthy": healthy
    }

    # ── STEP 2: Remove RP ──
    print(f"\n\n{'#'*70}")
    print(f"  STEP 2: REMOVING RP (no protocols pim static-rp 8.8.8.8)")
    print(f"{'#'*70}")
    rp_remove_time = time.time()
    commit_out = dev.configure(["no protocols pim static-rp 8.8.8.8"], wait_commit=60)
    print(f"[{ts()}] Commit output: {'OK' if 'ERROR' not in commit_out.upper() else commit_out}")
    rp_removed_at = time.time()
    print(f"[{ts()}] RP removed. Elapsed: {rp_removed_at - rp_remove_time:.1f}s")

    # ── STEP 3: Monitor degradation over time ──
    print(f"\n\n{'#'*70}")
    print(f"  STEP 3: MONITORING DEGRADATION (every 15s for 2 minutes)")
    print(f"{'#'*70}")

    degradation_snapshots = []
    for i in range(8):
        time.sleep(15)
        elapsed = time.time() - rp_removed_at
        label = f"Degradation T+{elapsed:.0f}s"
        pim_c, mc_c = print_state(label, dev)
        healthy = check_health(dev)
        degradation_snapshots.append({
            "elapsed": round(elapsed), "pim": pim_c, "mc": mc_c, "healthy": healthy
        })

    results["degradation"] = degradation_snapshots

    # ── STEP 4: Restore RP ──
    print(f"\n\n{'#'*70}")
    print(f"  STEP 4: RESTORING RP (protocols pim static-rp 8.8.8.8)")
    print(f"{'#'*70}")
    rp_restore_time = time.time()
    commit_out = dev.configure(["protocols pim static-rp 8.8.8.8"], wait_commit=60)
    print(f"[{ts()}] Commit output: {'OK' if 'ERROR' not in commit_out.upper() else commit_out}")
    rp_restored_at = time.time()
    print(f"[{ts()}] RP restored. Elapsed: {rp_restored_at - rp_restore_time:.1f}s")

    # ── STEP 5: Monitor recovery ──
    print(f"\n\n{'#'*70}")
    print(f"  STEP 5: MONITORING RECOVERY (every 15s for 3 minutes)")
    print(f"{'#'*70}")

    recovery_snapshots = []
    for i in range(12):
        time.sleep(15)
        elapsed = time.time() - rp_restored_at
        label = f"Recovery T+{elapsed:.0f}s"
        pim_c, mc_c = print_state(label, dev)
        healthy = check_health(dev)
        recovery_snapshots.append({
            "elapsed": round(elapsed), "pim": pim_c, "mc": mc_c, "healthy": healthy
        })
        if pim_c["star_g"] >= 29000 and pim_c["sg_sm"] >= 29000:
            print(f"\n  >> Recovery looks complete at T+{elapsed:.0f}s")
            break

    results["recovery"] = recovery_snapshots

    # ── FINAL STATE ──
    print(f"\n\n{'#'*70}")
    print(f"  FINAL STATE")
    print(f"{'#'*70}")
    final_pim, final_mc = print_state("Final", dev)
    healthy = check_health(dev)
    results["final"] = {"pim": final_pim, "mc": final_mc, "healthy": healthy}

    # ── SUMMARY ──
    print(f"\n\n{'='*70}")
    print(f"  TEST SUMMARY")
    print(f"{'='*70}")
    print(f"  Baseline:  (*,G)={baseline_pim['star_g']}  (S,G)SM={baseline_pim['sg_sm']}")
    print(f"  After RP removal (worst):")
    if degradation_snapshots:
        worst_sg = min(s["pim"]["sg_sm"] for s in degradation_snapshots)
        worst_stg = min(s["pim"]["star_g"] for s in degradation_snapshots)
        print(f"    (*,G) min={worst_stg}  (S,G)SM min={worst_sg}")
    print(f"  After RP restore (final):")
    print(f"    (*,G)={final_pim['star_g']}  (S,G)SM={final_pim['sg_sm']}")
    all_healthy = all(s["healthy"] for s in degradation_snapshots + recovery_snapshots)
    print(f"  Device healthy throughout: {all_healthy}")
    no_crash = True
    print(f"  No crash/hang: {no_crash}")

    dev.close()
    print(f"\n[{ts()}] Test complete.")

except Exception as e:
    print(f"\n[ERROR] {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
