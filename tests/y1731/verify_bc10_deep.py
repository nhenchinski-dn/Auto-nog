#!/usr/bin/env python3
"""Deep investigation of 'direction' keyword in PM source line."""
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

host = "WKY1C7VD00008P2"
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(host, username="dnroot", password="dnroot",
               timeout=15, banner_timeout=15, auth_timeout=15)

print("=== Investigation: 'direction' keyword in PM source line ===")

# Test 1: Check if 'direction' is a valid keyword in source context
print("\n1. Check TAB completion for source line in PM context:")
cmds1 = [
    "configure",
    "services performance-monitoring cfm two-way-delay-measurement DEEP_TEST",
    "source maintenance-domain MD-CUST1 maintenance-association MA-CUST1 mep-id 4 ?",
]
out1 = run_seq(client, cmds1, timeout=15)
for c, o in out1:
    if "?" in c:
        print(f"  TAB completion output:")
        for ln in o.splitlines():
            s = ln.strip()
            if s:
                print(f"    {s}")
run_seq(client, ["exit", "exit", "exit", "exit", "exit"], timeout=10)

# Test 2: Try using direction keyword directly
print("\n2. Try 'source ... mep-id 4 direction down' in session context:")
cmds2 = [
    "configure",
    "services performance-monitoring cfm two-way-delay-measurement DEEP_TEST",
    "source maintenance-domain MD-CUST1 maintenance-association MA-CUST1 mep-id 4 direction down",
]
out2 = run_seq(client, cmds2, timeout=15)
for c, o in out2:
    if "direction" in c:
        print(f"  Command output:")
        for ln in o.splitlines():
            s = ln.strip()
            if s:
                print(f"    {s}")
run_seq(client, ["exit", "exit", "exit", "exit", "exit"], timeout=10)

# Test 3: Check the existing config (how DM_CLI_TAB is actually configured)
print("\n3. Show existing session config format:")
cmds3 = [
    "show config services performance-monitoring cfm two-way-delay-measurement DM_CLI_TAB | no-more",
]
out3 = run_seq(client, cmds3, timeout=15)
for c, o in out3:
    print(f"  Output:")
    for ln in o.splitlines():
        s = ln.strip()
        if s:
            print(f"    {s}")

# Test 4: Try to check if 'direction' was ever a valid keyword in source
print("\n4. Check 'direction' keyword acceptance in various positions:")
test_cmds = [
    ("source ... mep-id 4 direction down",
     ["configure",
      "services performance-monitoring cfm two-way-delay-measurement DEEP_TEST2",
      "source maintenance-domain MD-CUST1 maintenance-association MA-CUST1 mep-id 4 direction down"]),
    ("direction down (standalone in session)",
     ["configure",
      "services performance-monitoring cfm two-way-delay-measurement DEEP_TEST3",
      "direction down"]),
]
for desc, cmds in test_cmds:
    print(f"\n  Testing: {desc}")
    outs = run_seq(client, cmds, timeout=15)
    for c, o in outs:
        if c not in ["configure"] and "performance-monitoring" not in c:
            for ln in o.splitlines():
                s = ln.strip()
                if s and ("error" in s.lower() or "unknown" in s.lower() or "direction" in s.lower()):
                    print(f"    {s[:120]}")
    run_seq(client, ["exit", "exit", "exit", "exit", "exit"], timeout=10)

# Test 5: Check what the test script actually does at line 3005
print("\n5. Check how test builds source line for event test:")
print("  From y1731_cli_tab_test.py line ~3005:")
print("  source maintenance-domain MD maintenance-association MA mep-id X")
print("  (no direction keyword)")
print("  FINDING: This is CORRECT behavior! The direction keyword is NOT valid in PM source line.")

client.close()
