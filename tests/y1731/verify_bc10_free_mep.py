#!/usr/bin/env python3
"""BC-10 re-test using free MEPs (MD-CUST1 direction down)."""
import sys, time, re
import paramiko

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

DEVICES = {
    "WKY1C7VD00008P2": {"md": "MD-CUST1", "ma": "MA-CUST1", "mep": "4", "target": "3", "direction": "down"},
    "xec1e3vr00008":   {"md": "MD-CUST1", "ma": "MA-CUST1", "mep": "3", "target": "4", "direction": "down"},
}

def run_seq(client, commands, timeout=30):
    ch = client.invoke_shell()
    ch.settimeout(timeout)
    time.sleep(1.5)
    while ch.recv_ready():
        ch.recv(65536)
    results = []
    for cmd in commands:
        ch.send(cmd + "\n")
        out = ""
        end_t = time.time() + timeout
        last_data = time.time()
        while time.time() < end_t:
            if ch.recv_ready():
                out += ch.recv(65536).decode(errors="ignore")
                last_data = time.time()
            else:
                if time.time() - last_data > 3:
                    break
                time.sleep(0.2)
        results.append((cmd, ANSI.sub("", out)))
    ch.close()
    return results

def has_error(text):
    pats = ["ERROR:", "Error:", "Unknown command", "Invalid command",
            "Commit check failed", "commit check has failed", "Commit failed",
            "Command failed", "TRANSACTION_COMMIT_CHECK_FAILED"]
    for p in pats:
        if p.lower() in text.lower():
            return True
    return False

for host, ctx in DEVICES.items():
    print(f"\n{'='*70}")
    print(f"BC-10 RETEST: {host}")
    md, ma, mep = ctx["md"], ctx["ma"], ctx["mep"]
    target, direction = ctx["target"], ctx["direction"]
    print(f"  MEP {mep} ({direction}) in {md}/{ma} -> target {target}")
    print(f"{'='*70}")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(host, username="dnroot", password="dnroot",
                       timeout=15, banner_timeout=15, auth_timeout=15)
    except Exception as e:
        print(f"  [FAIL] Cannot connect: {e}")
        continue

    prof, sess = "BC10_RETEST_P", "BC10_RETEST_S"

    for test_num, use_dir in [(1, False), (2, True)]:
        dir_label = f"direction {direction}" if use_dir else "NO direction"
        print(f"\n  TEST {test_num}: source ... mep-id {mep} ({dir_label})")
        source_line = f"source maintenance-domain {md} maintenance-association {ma} mep-id {mep}"
        if use_dir:
            source_line += f" direction {direction}"
        cmds = [
            "configure",
            f"services performance-monitoring profiles cfm two-way-delay-measurement {prof}",
            "inform-test-results enabled",
            "test-duration probes probe-count 3 probe-interval 1 repeat-interval 5",
            "thresholds delay-rtt-max 1",
            "exit", "exit", "exit", "exit", "exit",
            f"services performance-monitoring cfm two-way-delay-measurement {sess}",
            "admin-state enabled",
            f"profile {prof}",
            source_line,
            f"target mep-id {target}",
            "exit", "exit", "exit", "exit",
            "commit check",
        ]
        outs = run_seq(client, cmds, timeout=30)
        cc = ""
        for c, o in outs:
            if c == "commit check":
                cc = o
        err = has_error(cc)
        print(f"  Result: {'ERROR' if err else 'OK'}")
        if err:
            for ln in cc.splitlines():
                s = ln.strip()
                if s and ("error" in s.lower() or "ERROR" in s):
                    print(f"    {s[:120]}")
        run_seq(client, ["rollback 0", "exit"], timeout=10)
        run_seq(client, [
            "configure",
            f"no services performance-monitoring cfm two-way-delay-measurement {sess}",
            f"no services performance-monitoring profiles cfm two-way-delay-measurement {prof}",
            "commit", "exit",
        ], timeout=30)
        time.sleep(2)

    print(f"\n  Done with {host}.")
    client.close()
