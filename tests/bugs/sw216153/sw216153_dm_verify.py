#!/usr/bin/env python3
"""Wait one DM window and confirm DMM/DMR is exchanging on the current run."""
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


def send(ch, c, t=20, hide=False):
    if not hide: print(f"\n>>> {c}")
    ch.send(c + "\n")
    out = read(ch, t=t)
    if not hide:
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

SHOW = ("show services performance-monitoring cfm tests proactive "
        "two-way-delay session DM-SW216153-1 detail | no-more")

print("=== waiting up to 150s for one completed test after testbed recovery ===")
start = time.time()
last = None
while time.time() - start < 150:
    out = send(ch, SHOW, t=20, hide=True)
    m = re.search(
        r"Latest Test Results \(Index (\d+)\).*?Measurement validity:\s*(\S+).*?"
        r"DMM PDUs transmitted:\s*(\d+).*?DMR PDUs received:\s*(\d+).*?"
        r"Success rate:\s*([\d.]+)%.*?Average:\s*(\d+)\s*usec",
        out, re.S)
    if m:
        idx, val, tx, rx, succ, avg = m.groups()
        if last != idx:
            print(f"  idx={idx} validity={val:<11} tx={tx:>3} rx={rx:>3} success={succ:>5}% avg={avg}us")
            last = idx
        if val == "valid" and int(rx) > 0 and float(succ) > 0:
            print(f"\n  -> DMR exchange confirmed (idx {idx} valid, success={succ}%)")
            break
    time.sleep(10)

# Final detail
send(ch, SHOW, t=20)
cli.close()
