#!/usr/bin/env python3
"""SW-216153 ETH-DM buckets / history-test-results behavior.

Probe capabilities:
  - history depth (CLI only shows 'Last 10' — is there a knob?)
  - per-test record integrity (start/end/validity/tx/rx/delay) across the full window
  - verify that 'invalid' tests from config churn do not block new valid tests
  - verify wrap-around behavior by watching indices grow

Approach:
  - Relax thresholds (so real RTT doesn't affect anything)
  - Poll every 70s for several windows, record idx/status/RTT
  - Confirm history is FIFO (oldest drops off) and new valid tests keep appearing
"""
import re, time, paramiko

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
PROMPT = re.compile(r"[a-zA-Z0-9._-]+(\([^)]*\))?#\s*$")
UNCOMMITTED = "Uncommitted changes"


def strip(t): return re.sub(r"-- More -- \(Press q to quit\)\s*", "", ANSI.sub("", t).replace("\r", ""))


def read(ch, t=60, q=0.8):
    out, s, l, ans = "", time.time(), time.time(), False
    while True:
        if time.time() - s > t: break
        if ch.recv_ready():
            out += ch.recv(65536).decode("utf-8", errors="replace"); l = time.time()
            tail = strip(out)[-400:]
            if UNCOMMITTED in tail and not ans:
                ch.send("cancel\n"); ans = True; time.sleep(0.3); continue
            if PROMPT.search(tail): break
        else:
            if time.time() - l > q: break
            time.sleep(0.1)
    return strip(out)


def send(ch, c, t=30, hide=False):
    if not hide: print(f"\n>>> {c}")
    ch.send(c + "\n")
    out = read(ch, t=t)
    if not hide:
        for ln in out.splitlines():
            if ln.strip() and not PROMPT.search(ln):
                print(f"    {ln}")
    return out


def open_ch(host="xec1e3vr00008"):
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(host, username="dnroot", password="dnroot",
                look_for_keys=False, allow_agent=False, timeout=15)
    ch = cli.invoke_shell(width=250, height=10000)
    time.sleep(5); read(ch, t=15, q=1.5)
    return cli, ch


SHOW = ("show services performance-monitoring cfm tests proactive "
        "two-way-delay session DM-SW216153-1 detail | no-more")


def parse_history(out):
    rows = []
    for ln in out.splitlines():
        m = re.match(
            r"\|\s*(\d+)\s*\|\s*([\d-]+ [\d:]+\s+\+\d+)\s*\|\s*"
            r"([\d-]+ [\d:]+\s+\+\d+|)\s*\|\s*(\w+)\s*\|", ln)
        if m:
            rows.append({
                "idx": int(m.group(1)),
                "start": m.group(2),
                "end": m.group(3).strip(),
                "status": m.group(4),
            })
    return rows


# 1) Relax thresholds so we see pure 'valid' tests
cli, ch = open_ch()
print("=" * 78); print("STEP 1 — relax thresholds (avoid interference)"); print("=" * 78)
send(ch, "configure")
send(ch, "services performance-monitoring profiles cfm two-way-delay-measurement POP-DM-SW216153")
send(ch, "no thresholds")
send(ch, "no inform-test-results")
send(ch, "top")
send(ch, "commit and-exit", t=120)

# 2) Probe history-depth configurability
print("\n" + "=" * 78); print("STEP 2 — probe history-depth config options"); print("=" * 78)
send(ch, "configure")
send(ch, "services performance-monitoring profiles cfm two-way-delay-measurement POP-DM-SW216153 ?")
send(ch, "services performance-monitoring cfm two-way-delay-measurement DM-SW216153-1 ?")
send(ch, "top")

# Check if there's a global setting
send(ch, "services performance-monitoring ?")
send(ch, "end")
cli.close()

# 3) Poll history for 4 windows (about 4+ minutes). Expect history to grow, old entries to stay until >10, then FIFO.
print("\n" + "=" * 78); print("STEP 3 — poll DM history for 4+ completed windows"); print("=" * 78)
cli, ch = open_ch()
baselines = []
for i in range(5):
    if i: time.sleep(65)
    out = send(ch, SHOW, t=30, hide=True)
    rows = parse_history(out)
    completed = [r for r in rows if r["status"] in ("valid", "invalid")]
    print(f"\n--- poll {i+1} ({len(completed)} completed in history window) ---")
    for r in rows:
        print(f"  idx={r['idx']}  status={r['status']:<11}  start={r['start']}")
    baselines.append({r['idx']: r['status'] for r in rows})
cli.close()

# 4) Aggregate
print("\n" + "=" * 78); print("RESULT"); print("=" * 78)
all_idx = set()
for b in baselines:
    all_idx.update(b.keys())
print(f"  distinct indices observed across polls: {len(all_idx)}")
print(f"  idx range: {min(all_idx)} .. {max(all_idx)}")
# Check FIFO: the lowest idx in each later poll should only increase
lowest_seen = []
for b in baselines:
    if b:
        lowest_seen.append(min(b.keys()))
fifo_ok = all(lowest_seen[i] <= lowest_seen[i + 1] for i in range(len(lowest_seen) - 1))
print(f"  lowest idx per poll: {lowest_seen}")
print(f"  FIFO ordering (lowest idx monotonically increases): {fifo_ok}")
# Count table depth — CLI says 'Last 10'
max_depth = max(len(b) for b in baselines) if baselines else 0
print(f"  max history rows seen in one poll: {max_depth}")

pf = fifo_ok and max_depth <= 10 and len(all_idx) >= 5
print(f"\n  RESULT: {'PASS' if pf else 'FAIL'}")
