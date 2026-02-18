#!/usr/bin/env python3
"""
SW-211457 - Larger commit test
Creates a much bigger config change (multiple IRBs, EVPNs, BGP neighbors)
to widen the race window and stress the ORM compose logic.
"""

import paramiko
import threading
import time

DEVICE_IP = "100.64.3.239"
USER = "dnroot"
PASS = "dnroot"
BGP_AS = "65001"

NUM_IRBS = 15  # Create 15 IRB interfaces + EVPN instances


class DNOSSession:
    def __init__(self, name):
        self.name = name
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.shell = None
        self.log = []

    def connect(self):
        self.client.connect(DEVICE_IP, username=USER, password=PASS,
                          look_for_keys=False, allow_agent=False, timeout=30)
        self.shell = self.client.invoke_shell(width=200, height=50)
        self.shell.settimeout(120)
        time.sleep(3)
        self._read_all()
        self.log.append(f"[{self.name}] Connected")

    def send_cmd(self, cmd, wait=2):
        self.log.append(f"[{self.name}] >>> {cmd}")
        self.shell.send(cmd + "\n")
        time.sleep(wait)
        output = self._strip_ansi(self._read_all())
        self.log.append(f"[{self.name}] <<< {output}")
        return output

    def _read_all(self):
        output = ""
        while self.shell.recv_ready():
            chunk = self.shell.recv(65536).decode('utf-8', errors='replace')
            output += chunk
            time.sleep(0.3)
        return output

    @staticmethod
    def _strip_ansi(text):
        import re
        return re.sub(r'\x1b\[[0-9;]*m', '', text)

    def close(self):
        try:
            self.shell.close()
            self.client.close()
        except:
            pass


def cleanup():
    print("  [Cleanup] Removing test config...")
    s = DNOSSession("Cleanup")
    try:
        s.connect()
        s.send_cmd("configure", wait=2)
        for i in range(100, 100 + NUM_IRBS):
            s.send_cmd(f"no interfaces irb{i}", wait=0.5)
            s.send_cmd(f"no network-services evpn instance evpn{i}", wait=0.5)
        s.send_cmd("no network-services vrf instance testvrf", wait=0.5)

        # Also clean up the BGP neighbor we add
        s.send_cmd(f"no protocols bgp {BGP_AS} neighbor 99.99.99.99", wait=0.5)

        s.send_cmd("top", wait=1)
        out = s.send_cmd("show config compare | no-more", wait=3)
        if "Deleted" in out or "Changed" in out:
            result = s.send_cmd("commit", wait=30)
            print(f"  [Cleanup] Committed: {'succeeded' if 'succeeded' in result else result[-150:]}")
        else:
            s.send_cmd("rollback 0", wait=2)
            print("  [Cleanup] Nothing to clean")
        s.send_cmd("exit", wait=1)
        s.send_cmd("exit", wait=1)
    except Exception as e:
        print(f"  [Cleanup] Error: {e}")
    finally:
        s.close()


def run_big_commit_test(attempt, delay_s=0.2):
    print(f"\n{'='*70}")
    print(f"  ATTEMPT {attempt} (large commit, {NUM_IRBS} IRBs, delay={delay_s}s)")
    print(f"{'='*70}")

    sx = DNOSSession("X")
    sy = DNOSSession("Y")
    x_ready = threading.Event()
    result = {"y": "", "x": ""}

    def flow_x():
        try:
            sx.connect()
            sx.send_cmd("configure", wait=2)

            # Create a large config: multiple IRBs + EVPN instances + VRF + BGP neighbor
            for i in range(100, 100 + NUM_IRBS):
                ip = f"10.{(i-100)//250}.{(i-100)%250}.1/24"
                sx.send_cmd(f"interfaces irb{i} admin-state enabled ipv4-address {ip}", wait=0.5)

            for i in range(100, 100 + NUM_IRBS):
                sx.send_cmd(f"network-services evpn instance evpn{i} protocols bgp {BGP_AS} route-distinguisher {i}:{i}", wait=0.5)
                sx.send_cmd(f"network-services evpn instance evpn{i} protocols bgp {BGP_AS} export-l2vpn-evpn route-target {i}:{i}", wait=0.5)
                sx.send_cmd(f"network-services evpn instance evpn{i} protocols bgp {BGP_AS} import-l2vpn-evpn route-target {i}:{i}", wait=0.5)
                sx.send_cmd("top", wait=0.3)
                sx.send_cmd(f"network-services evpn instance evpn{i} router-interface irb{i}", wait=0.5)
                sx.send_cmd("top", wait=0.3)
                sx.send_cmd(f"network-services evpn instance evpn{i} counters service-counters enabled", wait=0.5)
                sx.send_cmd("top", wait=0.3)

            # Add VRF with all IRBs
            for i in range(100, 100 + NUM_IRBS):
                sx.send_cmd(f"network-services vrf instance testvrf interface irb{i}", wait=0.5)
                sx.send_cmd("top", wait=0.3)

            # Add a BGP neighbor with many address families (makes commit bigger)
            sx.send_cmd(f"protocols bgp {BGP_AS} neighbor 99.99.99.99 remote-as {BGP_AS}", wait=0.5)
            sx.send_cmd(f"protocols bgp {BGP_AS} neighbor 99.99.99.99 admin-state enabled", wait=0.5)
            sx.send_cmd(f"protocols bgp {BGP_AS} neighbor 99.99.99.99 address-family ipv4-unicast", wait=0.5)
            sx.send_cmd(f"protocols bgp {BGP_AS} neighbor 99.99.99.99 address-family ipv4-vpn", wait=0.5)
            sx.send_cmd(f"protocols bgp {BGP_AS} neighbor 99.99.99.99 address-family ipv6-unicast", wait=0.5)
            sx.send_cmd(f"protocols bgp {BGP_AS} neighbor 99.99.99.99 address-family ipv6-vpn", wait=0.5)
            sx.send_cmd(f"protocols bgp {BGP_AS} neighbor 99.99.99.99 address-family l2vpn-evpn", wait=0.5)
            sx.send_cmd("top", wait=0.5)

            out = sx.send_cmd("show config compare | no-more", wait=5)
            irb_count = out.count("irb1")
            sx.log.append(f"[X] Config compare has ~{irb_count} IRB references")

            # Step 1: Commit the large config
            print(f"  [X] Committing large config ({NUM_IRBS} IRBs + EVPNs + VRF + BGP)...")
            t_start = time.time()
            out = sx.send_cmd("commit", wait=45)
            t_elapsed = time.time() - t_start
            sx.log.append(f"[X] First commit took {t_elapsed:.1f}s")
            print(f"  [X] First commit took {t_elapsed:.1f}s: {'succeeded' if 'succeeded' in out else 'FAILED'}")

            if "ERROR" in out:
                sx.log.append(f"[X] FIRST COMMIT FAILED: {out[-300:]}")
                print(f"  [X] FAILED: {out[-300:]}")
                x_ready.set()
                return

            # Step 2: Rollback 1 to delete everything
            sx.send_cmd("rollback 1", wait=3)
            sx.log.append(f"[X] Rollback 1 done, about to commit deletion")

            # Step 3: Signal Y and commit the deletion (this is the race commit)
            x_ready.set()
            print(f"  [X] Signaling Y and committing large rollback deletion...")
            t_start = time.time()
            out = sx.send_cmd("commit", wait=60)
            t_elapsed = time.time() - t_start
            result["x"] = out
            sx.log.append(f"[X] Rollback commit took {t_elapsed:.1f}s: {out[-200:]}")
            print(f"  [X] Rollback commit took {t_elapsed:.1f}s: {'succeeded' if 'succeeded' in out else 'check log'}")

            sx.send_cmd("exit", wait=1)
            sx.send_cmd("exit", wait=1)
        except Exception as e:
            sx.log.append(f"[X] Error: {e}")
            print(f"  [X] Error: {e}")
            x_ready.set()
        finally:
            sx.close()

    def flow_y():
        try:
            sy.connect()
            sy.log.append(f"[Y] Waiting for signal from X...")
            x_ready.wait(timeout=300)
            if not x_ready.is_set():
                sy.log.append(f"[Y] Timeout")
                return

            time.sleep(delay_s)
            print(f"  [Y] Entering configure mode during X's commit window...")

            sy.send_cmd("configure", wait=2)
            sy.log.append(f"[Y] In configure mode, doing rollback 1")
            sy.send_cmd("rollback 1", wait=3)

            out = sy.send_cmd("show config compare | no-more", wait=5)
            has_irbs = "irb100" in out or "irb10" in out
            sy.log.append(f"[Y] Config compare has IRBs: {has_irbs}")
            print(f"  [Y] Rollback compare has IRBs: {has_irbs}")

            # THE BUG TRIGGER
            print(f"  [Y] Committing (bug trigger point)...")
            t_start = time.time()
            out = sy.send_cmd("commit", wait=60)
            t_elapsed = time.time() - t_start
            result["y"] = out
            sy.log.append(f"[Y] COMMIT: {t_elapsed:.1f}s - {out[-300:]}")
            print(f"  [Y] Commit took {t_elapsed:.1f}s")

            sy.send_cmd("exit", wait=1)
            sy.send_cmd("exit", wait=1)
        except Exception as e:
            sy.log.append(f"[Y] Error: {e}")
            print(f"  [Y] Error: {e}")
        finally:
            sy.close()

    tx = threading.Thread(target=flow_x)
    ty = threading.Thread(target=flow_y)
    tx.start()
    ty.start()
    tx.join(timeout=600)
    ty.join(timeout=600)

    # Print key logs
    print("\n  --- Key Session X Logs ---")
    for l in sx.log:
        if any(k in l for k in ["commit", "FAIL", "Error", "rollback", "Config compare"]):
            print(f"    {l}")
    print("\n  --- Key Session Y Logs ---")
    for l in sy.log:
        if any(k in l for k in ["commit", "COMMIT", "FAIL", "Error", "rollback", "Config compare", "IRBs"]):
            print(f"    {l}")

    y = result.get("y", "")
    if "Internal error" in y or "NoneType" in y:
        print(f"\n  *** BUG REPRODUCED ***")
        return "REPRODUCED"
    elif "RECOVERY" in y:
        print(f"\n  *** BUG REPRODUCED (RECOVERY) ***")
        return "REPRODUCED"
    elif "Commit succeeded" in y:
        print(f"\n  *** PASSED ***")
        return "PASS"
    elif "another commit" in y:
        print(f"\n  *** TIMING (concurrent guard) ***")
        return "TIMING"
    elif "Commit failed" in y:
        print(f"\n  *** COMMIT FAILED: {y[-200:]} ***")
        return "DIFFERENT_ERROR"
    else:
        print(f"\n  *** INCONCLUSIVE: {y[-200:]} ***")
        return "OTHER"


if __name__ == "__main__":
    print("=" * 70)
    print("  SW-211457 Large Commit Race Condition Test")
    print(f"  Device: {DEVICE_IP}")
    print(f"  Config size: {NUM_IRBS} IRBs + EVPNs + VRF + BGP neighbor")
    print(f"  Date: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    cleanup()
    time.sleep(5)

    all_results = []
    for attempt in range(1, 4):
        r = run_big_commit_test(attempt, delay_s=0.2)
        all_results.append(r)
        if r == "REPRODUCED":
            break
        cleanup()
        time.sleep(5)

    print(f"\n{'='*70}")
    print(f"  RESULTS: {all_results}")
    passes = all_results.count("PASS")
    if "REPRODUCED" in all_results:
        print(f"  CONCLUSION: BUG NOT FIXED")
    elif passes >= 2:
        print(f"  CONCLUSION: Bug appears FIXED ({passes}/{len(all_results)} passed)")
    else:
        print(f"  CONCLUSION: Inconclusive")
    print(f"{'='*70}")

    cleanup()
    print("\nDone.")
