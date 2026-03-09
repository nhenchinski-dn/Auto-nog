#!/usr/bin/env python3
import sys, time, re, paramiko

ANSI = re.compile(r"\[[0-9;]*[A-Za-z]")

def run_single(client, cmd, timeout=20):
    ch = client.invoke_shell()
    ch.settimeout(timeout)
    time.sleep(1.5)
    while ch.recv_ready():
        ch.recv(65536)
    ch.send(cmd + chr(10))
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

print("=== HISTORIC RESULTS ===")
cmds = [
    "show services performance-monitoring cfm tests proactive two-way-delay-measurement session-name DM_CLI_TAB detail | no-more",
    "show services performance-monitoring cfm tests proactive detail | no-more",
    "show services performance-monitoring cfm tests proactive | no-more",
]
for cmd in cmds:
    print(f"CMD: {cmd}")
    out = run_single(client, cmd, timeout=20)
    lines = out.splitlines()
    has_err = any("ERROR" in l or "Unknown word" in l for l in lines)
    if has_err:
        for ln in lines:
            if "ERROR" in ln:
                print(f"  ERR: {ln.strip()}")
    else:
        for ln in lines:
            s = ln.strip()
            if s:
                print(f"  {s}")
        break

print()
print("=== SYSTEM EVENTS ===")
evt_cmds = [
    "show system event-log | match CFM | no-more",
    "show system events | match CFM | no-more",
    "show log messages | match CFM | no-more",
]
for cmd in evt_cmds:
    print(f"CMD: {cmd}")
    out = run_single(client, cmd, timeout=20)
    lines = out.splitlines()
    has_err = any("ERROR" in l or "Unknown word" in l for l in lines)
    if has_err:
        for ln in lines:
            if "ERROR" in ln:
                print(f"  ERR: {ln.strip()}")
    else:
        for ln in lines[-30:]:
            s = ln.strip()
            if s:
                print(f"  {s}")
        break

print()
print("=== ON-DEMAND DM ===")
od_cmd = "run ethernet-oam cfm on-demand delay-measurement two-way maintenance-domain MD-CUST maintenance-association MA-CUST target mep-id 1 count 3"
out = run_single(client, od_cmd, timeout=25)
for ln in out.splitlines():
    s = ln.strip()
    if s:
        print(f"  {s}")

time.sleep(8)
print()
print("=== ON-DEMAND RESULTS ===")
out = run_single(client, "show services performance-monitoring cfm tests on-demand | no-more")
for ln in out.splitlines():
    s = ln.strip()
    if s:
        print(f"  {s}")

run_single(client, "request ethernet-oam cfm on-demand stop all")

client.close()
print("DONE")
