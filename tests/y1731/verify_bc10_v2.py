#!/usr/bin/env python3
import sys, time, re, paramiko

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

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
    for p in ["ERROR:", "Error:", "Commit check failed",
              "commit check has failed", "Commit failed",
              "Command failed", "TRANSACTION_COMMIT_CHECK_FAILED"]:
        if p.lower() in text.lower():
            return True
    return False

TESTS = [
    ("WKY1C7VD00008P2", "MD-CUST1", "MA-CUST1", "4", "3", "down"),
    ("xec1e3vr00008", "MD-CUST1", "MA-CUST1", "3", "4", "down"),
]

for host, md, ma, mep, target, direction in TESTS:
    print(f"\n== {host}: MEP {mep} ({direction}) ==")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, username="dnroot", password="dnroot",
                   timeout=15, banner_timeout=15, auth_timeout=15)
    prof, sess = "BC10_V2_P", "BC10_V2_S"
    for use_dir in [False, True]:
        tag = f"WITH direction {direction}" if use_dir else "NO direction"
        print(f"  {tag}:")
        src = f"source maintenance-domain {md} maintenance-association {ma} mep-id {mep}"
        if use_dir:
            src += f" direction {direction}"
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
            src,
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
        print(f"    -> {'ERROR' if err else 'OK'}")
        if err:
            for ln in cc.splitlines():
                s = ln.strip()
                if s and ("error" in s.lower() or "ERROR" in s):
                    print(f"       {s[:120]}")
        run_seq(client, ["rollback 0", "exit"], timeout=10)
        run_seq(client, [
            "configure",
            f"no services performance-monitoring cfm two-way-delay-measurement {sess}",
            f"no services performance-monitoring profiles cfm two-way-delay-measurement {prof}",
            "commit", "exit",
        ], timeout=30)
        time.sleep(2)
    client.close()
