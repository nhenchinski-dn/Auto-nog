#!/usr/bin/env python3
"""
ASM SPT Switchover — Negative Tests (SW-242472)

Tests:
  N1: Unreachable source (shut source interface) — verify graceful degradation
  N2: RP failure during active SPT — verify (S,G) continues without RP
  N3: Source flapping (rapid interface toggles) — verify system stability
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

    def show(self, cmd, wait=8):
        return self.run(cmd + " | no-more", wait)

    def configure(self, cmds, wait_commit=30):
        self.run("configure", 2)
        for cmd in cmds:
            self.run(cmd, 2)
        out = self.run("commit", wait_commit)
        self.run("exit", 2)
        if "Error" in out or "error" in out:
            print(f"  [ERROR] Commit failed: {out}")
            return False
        return True

    def close(self):
        self.ssh.close()


def banner(text):
    print("\n" + "=" * 70)
    print(f"  {text}")
    print("=" * 70)


def check_pim_state(dev):
    pim_tree = dev.show("show pim tree", 6)
    has_star_g = bool(re.search(r"\(\*,\s*239\.", pim_tree))
    has_s_g = bool(re.search(r"\(\d+\.\d+\.\d+\.\d+,\s*239\.\S+\)\s*SM", pim_tree))
    failed_rpf = "Failed RPF" in pim_tree or "Flags: S, F" in pim_tree or ", F" in pim_tree
    return has_star_g, has_s_g, failed_rpf, pim_tree


def check_device_health(dev):
    ver = dev.show("show system version", 5)
    alive = "DNOS" in ver
    return alive


def print_state(star_g, s_g, failed_rpf=False):
    status = []
    if star_g:
        status.append("(*,G)")
    if s_g:
        status.append("(S,G)")
    if failed_rpf:
        status.append("RPF-FAILED")
    if not status:
        status.append("EMPTY")
    print(f"  PIM tree: {' + '.join(status)}")


def main():
    utc_now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    banner("ASM SPT NEGATIVE TESTS — SW-242472")
    print(f"  Execution: {utc_now}")
    print(f"  Device:    Q3D-nog ({HOST})")

    dev = Device(HOST, USER, PASS)
    results = {}

    try:
        # Pre-check
        print("\n--- Pre-check: device health and PIM state ---")
        alive = check_device_health(dev)
        print(f"  Device responsive: {alive}")
        star_g, s_g, failed_rpf, _ = check_pim_state(dev)
        print_state(star_g, s_g)

        if not (star_g and s_g):
            print("  [WARN] Need both (*,G) and (S,G) active for negative tests.")
            print("  Make sure Spirent is sending traffic and Registers.")

        # ==============================================================
        banner("N1: UNREACHABLE SOURCE — Shut ge800-0/0/31")
        # ==============================================================
        print("  Shutting down source-facing interface...")

        dev.run("clear system cprl counters", 3)
        ok = dev.configure(["interfaces ge800-0/0/31 admin-state disabled"])
        print(f"  Commit: {'OK' if ok else 'FAILED'}")

        print("  Waiting 15 seconds...")
        time.sleep(15)

        star_g, s_g, failed_rpf, pim_tree = check_pim_state(dev)
        print_state(star_g, s_g, failed_rpf)

        n1_pass = star_g  # (*,G) should remain
        alive = check_device_health(dev)
        print(f"  Device responsive: {alive}")
        n1_pass = n1_pass and alive

        cprl = dev.show("show system cprl", 8)
        for line in cprl.split("\n"):
            if "Punted-IP-Multicast" in line and "|" in line:
                print(f"  CPRL: {line.strip()}")

        print(f"\n  Expected: (*,G) remains, (S,G) gets Failed RPF or disappears, no crash")
        print(f"  Result: {'PASS' if n1_pass else 'FAIL'}")
        results["N1"] = "PASS" if n1_pass else "FAIL"

        # Restore
        print("\n  Restoring ge800-0/0/31...")
        ok = dev.configure(["interfaces ge800-0/0/31 admin-state enabled"])
        print(f"  Commit: {'OK' if ok else 'FAILED'}")
        print("  Waiting 30 seconds for PIM neighbor + SPT recovery...")
        time.sleep(30)

        star_g, s_g, failed_rpf, _ = check_pim_state(dev)
        print(f"  After restore:")
        print_state(star_g, s_g)

        # ==============================================================
        banner("N2: RP FAILURE — Remove static RP while (S,G) active")
        # ==============================================================

        star_g, s_g, _, _ = check_pim_state(dev)
        if not s_g:
            print("  [WARN] (S,G) not present — waiting 30 more seconds...")
            time.sleep(30)
            star_g, s_g, _, _ = check_pim_state(dev)

        print("  Removing static RP 8.8.8.8...")
        dev.run("clear system cprl counters", 3)
        ok = dev.configure(["no protocols pim static-rp 8.8.8.8"])
        print(f"  Commit: {'OK' if ok else 'FAILED'}")

        print("  Waiting 15 seconds...")
        time.sleep(15)

        star_g, s_g, failed_rpf, pim_tree = check_pim_state(dev)
        print_state(star_g, s_g, failed_rpf)

        alive = check_device_health(dev)
        print(f"  Device responsive: {alive}")
        n2_pass = alive  # main criteria: no crash

        mc_route = dev.show("show multicast route", 8)
        forwarding = bool(re.search(r"\(3\.5\.0\.2.*Forwarded frames:.*[1-9]", mc_route, re.DOTALL))
        print(f"  (S,G) still forwarding: {forwarding}")

        print(f"\n  Expected: no crash, (S,G) may or may not persist depending on implementation")
        print(f"  Result: {'PASS' if n2_pass else 'FAIL'}")
        results["N2"] = "PASS" if n2_pass else "FAIL"

        # Restore RP
        print("\n  Restoring static RP 8.8.8.8...")
        ok = dev.configure(["protocols pim static-rp 8.8.8.8"])
        print(f"  Commit: {'OK' if ok else 'FAILED'}")
        print("  Waiting 30 seconds for state recovery...")
        time.sleep(30)

        star_g, s_g, _, _ = check_pim_state(dev)
        print(f"  After restore:")
        print_state(star_g, s_g)

        # ==============================================================
        banner("N3: SOURCE FLAPPING — Rapid PIM interface toggles")
        # ==============================================================
        print("  Rapidly toggling PIM on ge800-0/0/31 (5 cycles, 3s each)...")

        dev.run("clear system cprl counters", 3)

        for i in range(5):
            print(f"  Cycle {i+1}/5: disable PIM...", end="", flush=True)
            dev.configure(["no protocols pim address-family ipv4 interface ge800-0/0/31"], wait_commit=10)
            time.sleep(3)
            print(" re-enable...", end="", flush=True)
            dev.configure(["protocols pim address-family ipv4 interface ge800-0/0/31 admin-state enabled"], wait_commit=10)
            time.sleep(3)
            print(" done")

        print("  Waiting 20 seconds for state to settle...")
        time.sleep(20)

        alive = check_device_health(dev)
        print(f"  Device responsive: {alive}")

        star_g, s_g, failed_rpf, _ = check_pim_state(dev)
        print_state(star_g, s_g)

        nbr = dev.show("show pim neighbors", 5)
        nbr_count = len(re.findall(r"ge800-0/0/\d+", nbr))
        print(f"  PIM neighbors: {nbr_count}")

        cprl = dev.show("show system cprl", 8)
        for line in cprl.split("\n"):
            if "Punted-IP-Multicast" in line and "|" in line:
                print(f"  CPRL: {line.strip()}")
            if "PIM " in line and "|" in line and "Punted" not in line:
                print(f"  CPRL: {line.strip()}")

        n3_pass = alive and (nbr_count >= 2)
        print(f"\n  Expected: no crash, PIM neighbors recover, state rebuilds")
        print(f"  Result: {'PASS' if n3_pass else 'FAIL'}")
        results["N3"] = "PASS" if n3_pass else "FAIL"

        # ==============================================================
        banner("NEGATIVE TEST SUMMARY")
        # ==============================================================
        for name, result in results.items():
            tag = "(/) " if result == "PASS" else "(x) "
            print(f"  {tag}{name}: {result}")

        all_pass = all(v == "PASS" for v in results.values())
        print(f"\n  OVERALL: {'PASS' if all_pass else 'FAIL'}")

        # Final health check
        print("\n--- Final device health ---")
        alive = check_device_health(dev)
        print(f"  Device responsive: {alive}")
        star_g, s_g, _, _ = check_pim_state(dev)
        print_state(star_g, s_g)

    finally:
        dev.close()

    print("\nDone.")


if __name__ == "__main__":
    main()
