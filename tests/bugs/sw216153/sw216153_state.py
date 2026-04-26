#!/usr/bin/env python3
"""Quick state check on both DUTs."""
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


def go(host, label, cmds):
    print(f"\n{'='*78}\n=== {label} ({host}) ===\n{'='*78}")
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(host, username="dnroot", password="dnroot",
                look_for_keys=False, allow_agent=False, timeout=15)
    ch = cli.invoke_shell(width=250, height=10000)
    time.sleep(5); read(ch, t=15, q=1.5)
    for c in cmds:
        send(ch, c)
    cli.close()


go("xec1e3vr00008", "DUT-A", [
    "show interfaces ge100-0/0/70 | no-more",
    "show interfaces ge100-0/0/70.100 | no-more",
    "show network-services bridge-domain | no-more",
    ("show services ethernet-oam connectivity-fault-management "
     "maintenance-domains MD-SW216153 maintenance-associations MA-SW216153 mep 1 | no-more"),
])

go("WKY1C7VD00008P2", "DUT-B", [
    "show interfaces ge400-0/0/33 | no-more",
    "show interfaces ge400-0/0/33.100 | no-more",
    "show network-services bridge-domain | no-more",
    ("show services ethernet-oam connectivity-fault-management "
     "maintenance-domains MD-SW216153 maintenance-associations MA-SW216153 mep 2 | no-more"),
])
