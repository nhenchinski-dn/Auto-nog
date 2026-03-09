#!/usr/bin/env python3
"""Re-run tests with correct DNOS CLI syntax."""
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

host = "WKY1C7VD00008P2"
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(host, username="dnroot", password="dnroot", timeout=15, banner_timeout=15, auth_timeout=15)

# 1. Historic results - try various syntax variants
print("=== HISTORIC RESULTS (trying multiple show variants) ===")
cmds = [
    "show services performance-monitoring cfm tests proactive two-way-delay-measurement session-name DM_CLI_TAB detail | no-more",
    "show services performance-monitoring cfm tests proactive two-way-delay-measurement detail | no-more",
    "show services performance-monitoring cfm tests proactive detail | no-more",
    "show services performance-monitoring cfm tests proactive | no-more",
]
for cmd in cmds:
    print(f"\n--- CMD: {cmd} ---")
    out = run_single(client, cmd, timeout=20)
    lines = out.splitlines()
    has_err = any("ERROR" in l or "Unknown word" in l for l in lines)
    if has_err:
        for ln in lines:
            if "ERROR" in ln or "Unknown" in ln:
                print(f"  {ln.strip()}")
    else:
        for ln in lines:
            s = ln.strip()
            if s:
                print(f"  {s}")
        break

# 2. System events
print("\n\n=== SYSTEM EVENTS (trying variants) ===")
evt_cmds = [
    "show system event-log | match CFM | no-more",
    "show system events | match CFM | no-more",
    "show system event-log | no-more",
    "show log messages | match CFM | no-more",
]
for cmd in evt_cmds:
    print(f"\n--- CMD: {cmd} ---")
    out = run_single(client, cmd, timeout=20)
    lines = out.splitlines()
    has_err = any("ERROR" in l or "Unknown word" in l for l in lines)
    if has_err:
        for ln in lines:
            if "ERROR" in ln or "Unknown" in ln:
                print(f"  {ln.strip()}")
    else:
        for ln in lines[-30:]:
            s = ln.strip()
            if s:
                print(f"  {s}")
        break

# 3. On-demand commands
print("\n\n=== ON-DEMAND DM (correct syntax) ===")
od_cmds = [
    "run ethernet-oam cfm on-demand delay-measurement two-way maintenance-domain MD-CUST maintenance-association MA-CUST target mep-id 1 count 3",
    "run ethernet-oam cfm on-demand delay-measurement two-way maintenance-domain MD-CUST maintenance-association MA-CUST target mep-id 1",
]
for cmd in od_cmds:
    print(f"\n--- CMD: {cmd} ---")
    out = run_single(client, cmd, timeout=25)
    lines = out.splitlines()
    has_err = any("ERROR" in l or "Unknown word" in l for l in lines)
    if has_err:
        for ln in lines:
            if "ERROR" in ln or "Unknown" in ln:
                print(f"  {ln.strip()}")
    else:
        for ln in lines[:30]:
            s = ln.strip()
            if s:
                print(f"  {s}")
        break

# Wait for on-demand to produce results
time.sleep(8)

# 4. Check on-demand results
print("\n\n=== ON-DEMAND RESULTS ===")
out = run_single(client, "show services performance-monitoring cfm tests on-demand | no-more")
for ln in out.splitlines():
    s = ln.strip()
    if s:
        print(f"  {s}")

# Stop on-demand
run_single(client, "request ethernet-oam cfm on-demand stop all")

# 5. SLM historic
print("\n\n=== SLM HISTORIC (trying variants) ===")
slm_cmds = [
    "show services performance-monitoring cfm tests proactive two-way-synthetic-loss-measurement session-name SLM_CLI_TAB detail | no-more",
    "show services performance-monitoring cfm tests proactive session-name SLM_CLI_TAB detail | no-more",
]
for cmd in slm_cmds:
    print(f"\n--- CMD: {cmd} ---")
    out = run_single(client, cmd, timeout=20)
    lines = out.splitlines()
    has_err = any("ERROR" in l or "Unknown word" in l for l in lines)
    if has_err:
        for ln in lines:
            if "ERROR" in ln or "Unknown" in ln:
                print(f"  {ln.strip()}")
    else:
        for ln in lines:
            s = ln.strip()
            if s:
                print(f"  {s}")
        break

client.close()
print("\nDONE")
