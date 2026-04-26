#!/usr/bin/env python3
"""Verify threshold config is persisted, walk SNMP/NETCONF for DM test results,
and try the 'run' / restconf angles for threshold-crossing events."""
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

# 1) Apply a TIGHT threshold cleanly, then verify config persisted
send(ch, "configure")
send(ch, "services performance-monitoring profiles cfm two-way-delay-measurement POP-DM-SW216153")
send(ch, "inform-test-results enabled")
send(ch, "no thresholds")
send(ch, "thresholds delay-rtt-max 2")
send(ch, "thresholds delay-rtt-avg 2")
send(ch, "thresholds success-rate 99")
send(ch, "top")
send(ch, "commit and-exit", t=60)

# 2) Verify config shows through
send(ch, ("show config services performance-monitoring profiles cfm "
          "two-way-delay-measurement POP-DM-SW216153 | no-more"))
send(ch, "show config services performance-monitoring | no-more")

# 3) Wait for a completed test under tight thresholds and look hard at the detail output
print("\n--- waiting 75s for a full test window under tight thresholds ---")
time.sleep(75)
send(ch, ("show services performance-monitoring cfm tests proactive "
          "two-way-delay session DM-SW216153-1 detail | no-more"), t=30)

# 4) Alarms now? Any traps?
send(ch, "show system alarms | no-more")
send(ch, "show snmp ?")
send(ch, "show snmp trap | no-more")

cli.close()
