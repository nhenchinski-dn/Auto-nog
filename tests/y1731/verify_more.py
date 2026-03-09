#!/usr/bin/env python3
"""Verify remaining bug candidates: historic results, events, boundaries, on-demand."""
import sys, time, re, paramiko

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

def run_single(client, cmd, timeout=20):
    ch = client.invoke_shell()
    ch.settimeout(timeout)
    time.sleep(1.5)
    while ch.recv_ready():
        ch.recv(65536)
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
    ch.close()
    return ANSI.sub("", out)

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

host = "WKY1C7VD00008P2"
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(host, username="dnroot", password="dnroot", timeout=15, banner_timeout=15, auth_timeout=15)
print(f"Connected to {host}")

# 1. Historic DM results
print(f"\n{'='*70}")
print("HISTORIC DM RESULTS (DM_CLI_TAB)")
print(f"{'='*70}")
out = run_single(client, "show services performance-monitoring cfm tests proactive two-way-delay-measurement DM_CLI_TAB historic | no-more")
for ln in out.splitlines():
    s = ln.strip()
    if s:
        print(f"  {s}")

# 2. Historic SLM results
print(f"\n{'='*70}")
print("HISTORIC SLM RESULTS (SLM_CLI_TAB)")
print(f"{'='*70}")
out = run_single(client, "show services performance-monitoring cfm tests proactive two-way-synthetic-loss-measurement SLM_CLI_TAB historic | no-more")
for ln in out.splitlines():
    s = ln.strip()
    if s:
        print(f"  {s}")

# 3. System events
print(f"\n{'='*70}")
print("SYSTEM EVENTS (CFM)")
print(f"{'='*70}")
out = run_single(client, "show system events service-name cfm | no-more")
lines = [l.strip() for l in out.splitlines() if l.strip()]
for ln in lines[-25:]:
    print(f"  {ln}")

# 4. Proactive DM detail
print(f"\n{'='*70}")
print("PROACTIVE DM DETAIL (DM_CLI_TAB)")
print(f"{'='*70}")
out = run_single(client, "show services performance-monitoring cfm tests proactive two-way-delay-measurement DM_CLI_TAB results | no-more")
for ln in out.splitlines():
    s = ln.strip()
    if s:
        print(f"  {s}")

# 5. Proactive SLM detail
print(f"\n{'='*70}")
print("PROACTIVE SLM DETAIL (SLM_CLI_TAB)")
print(f"{'='*70}")
out = run_single(client, "show services performance-monitoring cfm tests proactive two-way-synthetic-loss-measurement SLM_CLI_TAB results | no-more")
for ln in out.splitlines():
    s = ln.strip()
    if s:
        print(f"  {s}")

# 6. On-demand test results
print(f"\n{'='*70}")
print("ON-DEMAND TEST RESULTS")
print(f"{'='*70}")
out = run_single(client, "show services performance-monitoring cfm tests on-demand | no-more")
for ln in out.splitlines():
    s = ln.strip()
    if s:
        print(f"  {s}")

# 7. Boundary value tests for thresholds
print(f"\n{'='*70}")
print("BOUNDARY VALUE TESTS")
print(f"{'='*70}")
boundaries = [
    ("success-rate 0.0", True),
    ("success-rate 100.0", True),
    ("success-rate 100.1", False),
    ("success-rate -1", False),
    ("delay-rtt-avg 0", True),
    ("delay-rtt-avg 4294967295", True),
    ("delay-rtt-avg 4294967296", False),
]
for thresh, expect_ok in boundaries:
    pn = "BNDRY_TEST"
    cmds = ["configure",
        f"services performance-monitoring profiles cfm two-way-delay-measurement {pn}",
        "inform-test-results enabled",
        "test-duration probes probe-count 5 probe-interval 1 repeat-interval 10",
        f"thresholds {thresh}", "commit check"]
    outs = run_seq(client, cmds, timeout=15)
    cc = ""
    for c, o in outs:
        if c == "commit check":
            cc = o
    has_err = any(p.lower() in cc.lower() for p in ["ERROR", "error", "Invalid", "out of range"])
    actual_ok = not has_err
    match = actual_ok == expect_ok
    status = "PASS" if match else "FAIL"
    print(f"  [{status}] {thresh}: expected={'OK' if expect_ok else 'ERR'} actual={'OK' if actual_ok else 'ERR'}")
    if not match:
        err_lines = [l.strip() for l in cc.splitlines() if l.strip() and ("error" in l.lower() or "ERROR" in l)]
        print(f"    {'; '.join(err_lines[:3])}")
    run_seq(client, ["rollback 0", "exit"], timeout=10)
    run_seq(client, ["configure", f"no services performance-monitoring profiles cfm two-way-delay-measurement {pn}", "commit", "exit"], timeout=15)

# 8. Run a quick on-demand DM and check output
print(f"\n{'='*70}")
print("ON-DEMAND DM TEST (live)")
print(f"{'='*70}")
out = run_single(client, "run ethernet-oam cfm on-demand two-way-delay-measurement maintenance-domain MD-CUST maintenance-association MA-CUST mep-id 2 target mep-id 1 count 3", timeout=30)
for ln in out.splitlines():
    s = ln.strip()
    if s:
        print(f"  {s}")

time.sleep(5)

# Check the on-demand result
out = run_single(client, "show services performance-monitoring cfm tests on-demand two-way-delay-measurement DM-MD-CUST-MA-CUST-2 results | no-more")
for ln in out.splitlines():
    s = ln.strip()
    if s:
        print(f"  {s}")

# 9. Stop any on-demand tests
out = run_single(client, "request ethernet-oam cfm on-demand stop all")
for ln in out.splitlines():
    s = ln.strip()
    if s:
        print(f"  {s}")

client.close()
print(f"\nDONE")
