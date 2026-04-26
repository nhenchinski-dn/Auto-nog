#!/usr/bin/env python3
"""Find where DNOS surfaces proactive-DM threshold crossings."""
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


cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect("xec1e3vr00008", username="dnroot", password="dnroot",
            look_for_keys=False, allow_agent=False, timeout=15)
ch = cli.invoke_shell(width=250, height=10000)
time.sleep(5); read(ch, t=15, q=1.5)

# Discover show sub-tree for PM
send(ch, "show services performance-monitoring ?")
send(ch, "show services performance-monitoring cfm ?")
send(ch, "show services performance-monitoring cfm tests ?")
send(ch, "show services performance-monitoring cfm tests proactive two-way-delay ?")
send(ch, "show services performance-monitoring cfm tests proactive two-way-delay session DM-SW216153-1 ?")

# Alarms & event-manager
send(ch, "show system alarms | no-more")
send(ch, "show system event-manager ?")

# inform / notifications
send(ch, "show services performance-monitoring cfm notifications | no-more")
send(ch, "show services performance-monitoring cfm statistics | no-more")

# Bin-thresholds might be where crossings show up
send(ch, ("show services performance-monitoring cfm tests proactive "
          "two-way-delay session DM-SW216153-1 statistics | no-more"), t=20)
send(ch, ("show services performance-monitoring cfm tests proactive "
          "two-way-delay session DM-SW216153-1 bins | no-more"), t=20)

cli.close()
