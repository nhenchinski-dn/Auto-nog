#!/usr/bin/env python3
"""Dig into a recently completed test by index, plus poll the CFM PDU stats and
remote-MEP state on both sides to look for drops/flaps."""
import re, time, paramiko

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
PROMPT = re.compile(r"[a-zA-Z0-9._-]+(\([^)]*\))?#\s*$")


def strip(t): return re.sub(r"-- More -- \(Press q to quit\)\s*", "", ANSI.sub("", t).replace("\r", ""))


def read(ch, t=20, q=0.6):
    out, s, l = "", time.time(), time.time()
    while True:
        if time.time() - s > t: break
        if ch.recv_ready():
            out += ch.recv(65536).decode("utf-8", errors="replace"); l = time.time()
            if PROMPT.search(strip(out)[-200:]): break
        else:
            if time.time() - l > q: break
            time.sleep(0.1)
    return strip(out)


def send(ch, c, t=20):
    print(f"\n>>> {c}")
    ch.send(c + "\n")
    out = read(ch, t=t)
    for ln in out.splitlines():
        if ln.strip() and not PROMPT.search(ln):
            print(f"    {ln}")
    return out


def open_ch(host):
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(host, username="dnroot", password="dnroot",
                look_for_keys=False, allow_agent=False, timeout=15)
    ch = cli.invoke_shell(width=250, height=10000)
    time.sleep(5); read(ch, t=15, q=1.5)
    return cli, ch


# DUT-A: dig into the DM session
print("\n" + "#" * 78); print("# DUT-A — DM session detail + recent completed test"); print("#" * 78)
cli, ch = open_ch("xec1e3vr00008")
out = send(ch, ("show services performance-monitoring cfm tests proactive "
               "two-way-delay session DM-SW216153-1 detail | no-more"), t=30)
# Extract last 'valid' index from history table
valids = re.findall(r"\|\s*(\d+)\s*\|\s*[\d-]+ [\d:]+\s+\+\d+\s*\|\s*[\d-]+ [\d:]+\s+\+\d+\s*\|\s*valid\s*\|", out)
if valids:
    last_valid = valids[-1]
    print(f"\n  -> last 'valid' completed test index: {last_valid}")
    out = send(ch, ("show services performance-monitoring cfm tests proactive "
                   "two-way-delay session DM-SW216153-1 history-test-result-index "
                   f"{last_valid} | no-more"), t=20)
    out = send(ch, ("show services performance-monitoring cfm tests proactive "
                   "two-way-delay session DM-SW216153-1 detail history-test-result-index "
                   f"{last_valid} | no-more"), t=20)

# CFM PDU stats on both sides — interested in DMM/DMR counts since session start
print("\n" + "#" * 78); print("# DUT-A — CFM PDU stats (MEP 1)"); print("#" * 78)
out = send(ch, ("show services ethernet-oam connectivity-fault-management "
               "maintenance-domains MD-SW216153 maintenance-associations MA-SW216153 "
               "mep 1 | no-more"), t=20)
cli.close()

print("\n" + "#" * 78); print("# DUT-B — CFM PDU stats (MEP 2)"); print("#" * 78)
cli, ch = open_ch("WKY1C7VD00008P2")
out = send(ch, ("show services ethernet-oam connectivity-fault-management "
               "maintenance-domains MD-SW216153 maintenance-associations MA-SW216153 "
               "mep 2 | no-more"), t=20)
cli.close()
