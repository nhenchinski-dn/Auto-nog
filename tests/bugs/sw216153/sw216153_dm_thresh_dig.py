#!/usr/bin/env python3
"""Dig into the tests completed during tight/relaxed threshold windows."""
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

# Full detail (includes Latest + History)
send(ch, ("show services performance-monitoring cfm tests proactive "
          "two-way-delay session DM-SW216153-1 detail | no-more"), t=30)

# Discover event-log command
send(ch, "show system ?")
send(ch, "show log ?")

# Try common variants
for cmd in (
    "show system event-log | match dm",
    "show system event-log | match cfm",
    "show system event-log | include performance",
    "show log system | match dm | no-more",
    "show logging | match dm | no-more",
    "show logging | match threshold | no-more",
):
    send(ch, cmd, t=15)

# Try querying per-test detail via index
for cmd in (
    "show services performance-monitoring cfm tests proactive two-way-delay session DM-SW216153-1 test-results ?",
    "show services performance-monitoring cfm tests proactive two-way-delay session DM-SW216153-1 test-result 1631 | no-more",
    "show services performance-monitoring cfm tests proactive two-way-delay session DM-SW216153-1 probe-results | no-more",
):
    send(ch, cmd, t=15)
cli.close()
