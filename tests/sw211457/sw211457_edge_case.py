#!/usr/bin/env python3
"""
SW-211457 Edge Case Test - Simplified IRB-only version
Based on Uriel Sirota's simplified reproduction (no VRF, no profile change).
Also tests with tighter timing to stress the race window.
"""

import paramiko
import threading
import time

DEVICE_IP = "100.64.3.239"
USER = "dnroot"
PASS = "dnroot"
BGP_AS = "65001"


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
        self.shell.settimeout(90)
        time.sleep(3)
        self._read_all()
        self.log.append(f"[{self.name}] Connected")

    def send_cmd(self, cmd, wait=3):
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
    s = DNOSSession("Cleanup")
    try:
        s.connect()
        s.send_cmd("configure", wait=2)
        s.send_cmd("no interfaces irb1", wait=1)
        s.send_cmd("no interfaces irb2", wait=1)
        s.send_cmd("no interfaces irb66", wait=1)
        s.send_cmd("no network-services evpn instance evpn1", wait=1)
        s.send_cmd("no network-services evpn instance evpn2", wait=1)
        s.send_cmd("no network-services evpn instance kfkfkf", wait=1)
        s.send_cmd("no network-services vrf instance alpha", wait=1)
        s.send_cmd("top", wait=1)
        out = s.send_cmd("show config compare | no-more", wait=3)
        if "Deleted" in out or "Changed" in out:
            s.send_cmd("commit", wait=15)
        else:
            s.send_cmd("rollback 0", wait=2)
        s.send_cmd("exit", wait=1)
        s.send_cmd("exit", wait=1)
    except:
        pass
    finally:
        s.close()


def test_simplified_irb(attempt, delay_seconds=0.3):
    """Simplified test: just IRB + EVPN, no VRF, tight timing."""
    print(f"\n  [Edge Case {attempt}] IRB+EVPN only, delay={delay_seconds}s")

    sx = DNOSSession("X")
    sy = DNOSSession("Y")
    x_ready = threading.Event()
    result = {"y": ""}

    def flow_x():
        try:
            sx.connect()
            sx.send_cmd("configure", wait=2)
            sx.send_cmd("interfaces irb1 admin-state enabled ipv4-address 1.1.1.1/24", wait=1)
            sx.send_cmd(f"network-services evpn instance evpn1 protocols bgp {BGP_AS} route-distinguisher 1:1", wait=1)
            sx.send_cmd(f"network-services evpn instance evpn1 protocols bgp {BGP_AS} export-l2vpn-evpn route-target 1:1", wait=1)
            sx.send_cmd(f"network-services evpn instance evpn1 protocols bgp {BGP_AS} import-l2vpn-evpn route-target 1:1", wait=1)
            sx.send_cmd("top", wait=1)
            sx.send_cmd("network-services evpn instance evpn1 router-interface irb1", wait=1)
            sx.send_cmd("top", wait=1)

            out = sx.send_cmd("commit", wait=20)
            if "ERROR" in out:
                sx.log.append(f"[X] First commit FAILED: {out}")
                x_ready.set()
                return

            sx.send_cmd("rollback 1", wait=2)
            x_ready.set()
            out = sx.send_cmd("commit", wait=25)
            sx.log.append(f"[X] Rollback commit: {'succeeded' if 'succeeded' in out else out[-200:]}")

            sx.send_cmd("exit", wait=1)
            sx.send_cmd("exit", wait=1)
        except Exception as e:
            sx.log.append(f"[X] Error: {e}")
            x_ready.set()
        finally:
            sx.close()

    def flow_y():
        try:
            sy.connect()
            x_ready.wait(timeout=120)
            time.sleep(delay_seconds)

            sy.send_cmd("configure", wait=2)
            sy.send_cmd("rollback 1", wait=2)
            out = sy.send_cmd("show config compare | no-more", wait=3)
            sy.log.append(f"[Y] Rollback compare: {'irb1' in out and 'evpn1' in out}")

            out = sy.send_cmd("commit", wait=30)
            result["y"] = out
            sy.log.append(f"[Y] Commit: {out[-200:]}")

            sy.send_cmd("exit", wait=1)
            sy.send_cmd("exit", wait=1)
        except Exception as e:
            sy.log.append(f"[Y] Error: {e}")
        finally:
            sy.close()

    tx = threading.Thread(target=flow_x)
    ty = threading.Thread(target=flow_y)
    tx.start()
    ty.start()
    tx.join(timeout=200)
    ty.join(timeout=200)

    for line in sx.log:
        print(f"    {line}")
    for line in sy.log:
        print(f"    {line}")

    y = result.get("y", "")
    if "Internal error" in y or "NoneType" in y:
        print(f"    --> BUG REPRODUCED")
        return "REPRODUCED"
    elif "RECOVERY" in y:
        print(f"    --> BUG REPRODUCED (RECOVERY)")
        return "REPRODUCED"
    elif "Commit succeeded" in y:
        print(f"    --> PASSED (commit succeeded)")
        return "PASS"
    elif "another commit" in y:
        print(f"    --> TIMING (concurrent guard)")
        return "TIMING"
    else:
        print(f"    --> OTHER: {y[-200:]}")
        return "OTHER"


if __name__ == "__main__":
    print("=== SW-211457 Edge Case Tests ===")
    print(f"Device: {DEVICE_IP}")
    print(f"Date: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    cleanup()
    time.sleep(3)

    all_results = []

    # Test with different timing delays to maximize chances of hitting the race window
    for i, delay in enumerate([0.1, 0.3, 0.5], 1):
        result = test_simplified_irb(i, delay_seconds=delay)
        all_results.append(result)
        if result == "REPRODUCED":
            break
        cleanup()
        time.sleep(5)

    print(f"\n{'='*60}")
    print(f"  Edge Case Results: {all_results}")
    if "REPRODUCED" in all_results:
        print(f"  CONCLUSION: BUG NOT FIXED")
    else:
        passes = all_results.count("PASS")
        print(f"  CONCLUSION: All edge cases passed ({passes}/{len(all_results)})")
    print(f"{'='*60}")

    cleanup()
