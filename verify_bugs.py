#!/usr/bin/env python3
"""
Bug verification script for Q3D Multicast Epic SW-212074.
Runs NON-DESTRUCTIVE checks against the Q3D device.
"""
import paramiko
import time
import re
import sys
import json

DEVICE_IP = "100.64.6.171"
USERNAME = "dnroot"
PASSWORD = "dnroot"
ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')

class DeviceSession:
    def __init__(self, ip, user, pwd):
        self.ssh = paramiko.SSHClient()
        self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.ssh.connect(ip, username=user, password=pwd,
                         timeout=30, look_for_keys=False, allow_agent=False)
        self.chan = self.ssh.invoke_shell(width=500, height=500)
        time.sleep(5)
        self.chan.recv(65535)
        self._run("no-paging", 3)
        print(f"Connected to {ip}")

    def _run(self, cmd, wait=8):
        self.chan.send(cmd + "\n")
        time.sleep(wait)
        out = b""
        for _ in range(10):
            if self.chan.recv_ready():
                out += self.chan.recv(65535)
                time.sleep(0.5)
            else:
                break
        return ANSI_RE.sub("", out.decode(errors="replace"))

    def run(self, cmd, wait=8):
        raw = self._run(cmd, wait)
        lines = raw.split("\n")
        result = []
        for l in lines:
            s = l.rstrip()
            if s and cmd not in s and "no-paging" not in s and "-- More --" not in s:
                result.append(s)
        return "\n".join(result)

    def close(self):
        self.chan.send("exit\n")
        time.sleep(1)
        self.chan.close()
        self.ssh.close()


def section(title):
    print("\n" + "=" * 70)
    print(f"  {title}")
    print("=" * 70)


def test_unsupported_feature_commit_validation(dev):
    """BUG-03: Test that unsupported features are blocked at commit check."""
    section("BUG-03: UNSUPPORTED FEATURE COMMIT VALIDATION")

    test_configs = {
        "PIM in VRF": [
            "set vrf test-vrf",
            "set pim vrf test-vrf interface lo1 admin-state enabled"
        ],
        "PIMv6 (IPv6 Multicast)": [
            "set pim6 interface lo1 admin-state enabled"
        ],
        "ASM (Any-Source Multicast / RP config)": [
            "set pim rp address 1.1.1.1 group-range 239.0.0.0/8"
        ],
    }

    for name, cmds in test_configs.items():
        print(f"\n--- Testing: {name} ---")
        dev._run("configure", 3)
        for cmd in cmds:
            out = dev._run(cmd, 3)
            print(f"  CMD: {cmd}")
            if "ERROR" in out or "error" in out:
                print(f"  >> BLOCKED AT CONFIG: {out.strip()}")

        out = dev.run("commit check", 15)
        print(f"  commit check output:")
        for l in out.split("\n"):
            ls = l.strip()
            if ls:
                print(f"    {ls}")

        if "error" in out.lower() or "fail" in out.lower() or "reject" in out.lower() or "not supported" in out.lower():
            print(f"  RESULT: PASS - {name} is properly blocked")
        elif "ok" in out.lower() or "success" in out.lower():
            print(f"  RESULT: *** BUG CONFIRMED *** - {name} passes commit check on Q3D!")
        else:
            print(f"  RESULT: INCONCLUSIVE - review output above")

        dev._run("rollback 0", 5)
        dev._run("exit", 3)


def test_mofrr_commit_validation(dev):
    """BUG-08: Test that MoFRR is blocked on Q3D."""
    section("BUG-08: MoFRR COMMIT VALIDATION ON Q3D")

    print("\n--- Testing: MoFRR configuration ---")
    dev._run("configure", 3)
    out = dev._run("set pim mofrr admin-state enabled", 5)
    print(f"  CMD: set pim mofrr admin-state enabled")
    if "ERROR" in out or "error" in out:
        print(f"  >> BLOCKED AT CONFIG: {out.strip()}")

    out = dev.run("commit check", 15)
    print(f"  commit check output:")
    for l in out.split("\n"):
        ls = l.strip()
        if ls:
            print(f"    {ls}")

    if "error" in out.lower() or "fail" in out.lower() or "not supported" in out.lower():
        print(f"  RESULT: PASS - MoFRR is properly blocked")
    elif "ok" in out.lower() or "success" in out.lower():
        print(f"  RESULT: *** BUG CONFIRMED *** - MoFRR passes commit check on Q3D!")
    else:
        print(f"  RESULT: INCONCLUSIVE - review output above")

    dev._run("rollback 0", 5)
    dev._run("exit", 3)


def test_counter_consistency(dev):
    """BUG-09 / BUG-01: Check multicast counter consistency."""
    section("BUG-01/BUG-09: MULTICAST COUNTER CONSISTENCY")

    print("\n--- Multicast Route Details ---")
    out = dev.run("show multicast route", 12)
    print(out)

    print("\n--- Multicast Counters ---")
    out = dev.run("show multicast counters", 10)
    print(out)

    print("\n--- MCDB Summary ---")
    out = dev.run("show multicast database summary", 10)
    print(out)

    print("\n--- Interface ge800-0/0/8 counters (IIF candidate) ---")
    out = dev.run("show interfaces ge800-0/0/8 counters", 8)
    for l in out.split("\n"):
        ls = l.strip()
        if any(k in ls.lower() for k in ["multicast", "drop", "error", "discard", "rx ", "tx "]):
            print(f"  {ls}")

    print("\n--- Interface ge800-0/0/9 counters (OIF candidate) ---")
    out = dev.run("show interfaces ge800-0/0/9 counters", 8)
    for l in out.split("\n"):
        ls = l.strip()
        if any(k in ls.lower() for k in ["multicast", "drop", "error", "discard", "rx ", "tx "]):
            print(f"  {ls}")

    print("\n--- RPF check ---")
    out = dev.run("show multicast rpf-intact", 8)
    print(out)


def test_multicast_lookup_failure(dev):
    """BUG-01: Check for persistent multicast lookup failure drops."""
    section("BUG-01: MULTICAST LOOKUP FAILURE DROPS")

    print("\n--- Checking NDP interface counters for lookup failures ---")
    out = dev.run("show forwarding multicast counters", 10)
    print(out)

    print("\n--- Checking system drops ---")
    out = dev.run("show system drops", 10)
    for l in out.split("\n"):
        ls = l.strip()
        if any(k in ls.lower() for k in ["multicast", "lookup", "drop", "rpf", "trap"]):
            print(f"  {ls}")

    print("\n--- First snapshot of multicast counters ---")
    out1 = dev.run("show multicast counters", 8)
    print(out1)

    print("\n--- Waiting 10 seconds ---")
    time.sleep(10)

    print("--- Second snapshot of multicast counters ---")
    out2 = dev.run("show multicast counters", 8)
    print(out2)

    if out1 != out2:
        print("\n  *** OBSERVATION: Counters changed between snapshots (active drops/traffic)")
    else:
        print("\n  Counters are stable between snapshots")


def test_spt_switchover_config(dev):
    """Check SPT switchover and rpf-intact configuration."""
    section("SPT SWITCHOVER & RPF-INTACT CONFIG CHECK")

    print("\n--- Current PIM config ---")
    out = dev.run("show running-config pim", 12)
    print(out)

    print("\n--- Multicast rpf-intact config ---")
    out = dev.run("show running-config multicast rpf-intact", 8)
    print(out)

    print("\n--- PIM tree ---")
    out = dev.run("show pim tree", 12)
    for l in out.split("\n")[:40]:
        print(l)

    print("\n--- IGMP groups ---")
    out = dev.run("show igmp groups summary", 8)
    print(out)


def test_cprl_protection(dev):
    """Check CPRL settings for multicast trap protection."""
    section("CPRL PROTECTION CHECK")

    print("\n--- CPRL multicast settings ---")
    out = dev.run("show system cprl", 12)
    mc_lines = []
    all_lines = out.split("\n")
    for i, l in enumerate(all_lines):
        if any(k in l.lower() for k in ["multicast", "mcast", "rpf", "igmp", "pim"]):
            mc_lines.append(l)
    if mc_lines:
        for l in mc_lines:
            print(f"  {l}")
    else:
        print("  No multicast-specific CPRL entries found")
        for l in all_lines[:30]:
            print(f"  {l}")


def test_platform_info(dev):
    """Get Q3D platform identification."""
    section("PLATFORM IDENTIFICATION")

    out = dev.run("show system information", 10)
    for l in out.split("\n"):
        ls = l.strip()
        if ls:
            print(f"  {ls}")

    print("\n--- NCP version ---")
    out = dev.run("show version", 8)
    for l in out.split("\n"):
        ls = l.strip()
        if ls:
            print(f"  {ls}")


def main():
    print("Bug Verification Script for Q3D Multicast (SW-212074)")
    print("Device:", DEVICE_IP)
    print("=" * 70)

    dev = DeviceSession(DEVICE_IP, USERNAME, PASSWORD)

    try:
        test_platform_info(dev)
        test_spt_switchover_config(dev)
        test_counter_consistency(dev)
        test_multicast_lookup_failure(dev)
        test_cprl_protection(dev)
        test_unsupported_feature_commit_validation(dev)
        test_mofrr_commit_validation(dev)
    except Exception as e:
        print(f"\nERROR during testing: {e}")
        import traceback
        traceback.print_exc()
    finally:
        try:
            dev._run("rollback 0", 5)
            dev._run("exit", 3)
        except:
            pass
        dev.close()
        print("\n\nDONE - All tests completed")

if __name__ == "__main__":
    main()
