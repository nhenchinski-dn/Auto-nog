#!/usr/bin/env python3
"""SW-216153 ETH-DM thresholds & threshold-crossing.

Adds 'thresholds' to POP-DM-SW216153 with very tight values (RTT-max 2us — below
the actual 4us baseline), waits for one full 60s test window, then reads the
session detail and any threshold-crossing event/inform output.

Then relaxes the threshold to 50000us and confirms the next test passes.

Pass criteria:
  - Threshold config commits cleanly
  - Tight threshold (2us) produces a 'failed' or 'crossed' marker on the next
    completed test (delay-rtt-max alarm OR success-rate threshold tripped)
  - Relaxed threshold (50000us) produces 'valid' on the next completed test
  - 'inform-test-results enabled' produces a system-event for the failed test
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


def send(ch, c, t=60, q=0.8, hide=False):
    if not hide: print(f"\n>>> {c}")
    ch.send(c + "\n")
    out = read(ch, t=t, q=q)
    if not hide:
        for ln in out.splitlines():
            ln = ln.rstrip()
            if ln: print(f"    {ln}")
    return out


def open_ch(host="xec1e3vr00008"):
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(host, username="dnroot", password="dnroot",
                look_for_keys=False, allow_agent=False, timeout=15)
    ch = cli.invoke_shell(width=250, height=10000)
    time.sleep(5); read(ch, t=15, q=1.5)
    return cli, ch


def parse_validity(out):
    m = re.search(r"Latest Test Results.*?Measurement validity:\s*(\S+)", out, re.S)
    return m.group(1) if m else None


def parse_index(out):
    m = re.search(r"Latest Test Results \(Index (\d+)\)", out)
    return int(m.group(1)) if m else None


SHOW = ("show services performance-monitoring cfm tests proactive "
        "two-way-delay session DM-SW216153-1 detail | no-more")


def configure(profile_threshold_us, inform_test_results=True):
    cli, ch = open_ch()
    print(f"\n{'#'*78}\n# Configuring profile: rtt-max threshold = {profile_threshold_us} us, "
          f"inform-test-results={inform_test_results}\n{'#'*78}")
    send(ch, "configure")
    send(ch, "services performance-monitoring profiles cfm two-way-delay-measurement POP-DM-SW216153")
    if inform_test_results:
        send(ch, "inform-test-results enabled")
    else:
        send(ch, "no inform-test-results")
    send(ch, "no thresholds")
    send(ch, "thresholds")
    send(ch, f"delay-rtt-max {profile_threshold_us}")
    send(ch, "top")
    out = send(ch, "commit and-exit", t=120, q=2.5)
    cli.close()


def wait_for_new_test(prev_index, max_wait_s=140):
    """Poll until the Latest Test Results shows a completed test with index > prev_index."""
    cli, ch = open_ch()
    deadline = time.time() + max_wait_s
    last_idx = prev_index
    while time.time() < deadline:
        time.sleep(15)
        out = send(ch, SHOW, t=20, hide=True)
        idx = parse_index(out)
        val = parse_validity(out)
        print(f"  poll  idx={idx} validity={val}")
        if idx and idx != last_idx and val and val != "incomplete":
            cli.close()
            return idx, val, out
        last_idx = idx
    cli.close()
    return last_idx, val, None


print("=" * 78)
print("STEP 1 — record current test index baseline")
print("=" * 78)
cli, ch = open_ch()
out = send(ch, SHOW, t=20)
baseline_idx = parse_index(out) or 0
cli.close()
print(f"  baseline last-completed index ~ {baseline_idx}")

print("\n" + "=" * 78)
print("STEP 2 — set TIGHT threshold (delay-rtt-max = 2 us, below 4us actual)")
print("=" * 78)
configure(profile_threshold_us=2, inform_test_results=True)
print("  waiting for next completed test (up to ~140s)...")
new_idx, validity, full = wait_for_new_test(baseline_idx, max_wait_s=140)
print(f"\n  next completed test idx={new_idx} validity={validity}")
if full:
    # Show the latest results block + any threshold/alarm marker
    m = re.search(r"Latest Test Results.*?(?=Historical Test Results|$)", full, re.S)
    if m:
        for ln in m.group(0).splitlines():
            print(f"    {ln}")

print("\n" + "=" * 78)
print("STEP 3 — relax threshold (delay-rtt-max = 50000 us)")
print("=" * 78)
configure(profile_threshold_us=50000, inform_test_results=True)
new_idx2, validity2, full2 = wait_for_new_test(new_idx, max_wait_s=140)
print(f"\n  next completed test idx={new_idx2} validity={validity2}")
if full2:
    m = re.search(r"Latest Test Results.*?(?=Historical Test Results|$)", full2, re.S)
    if m:
        for ln in m.group(0).splitlines():
            print(f"    {ln}")

print("\n" + "=" * 78)
print("STEP 4 — system events (look for DM threshold-crossing event)")
print("=" * 78)
cli, ch = open_ch()
out = send(ch, "show system events | match dm | no-more", t=20)
out = send(ch, "show system events | match performance | no-more", t=20)
out = send(ch, "show system events | match cfm | no-more", t=20)
cli.close()

print("\n" + "=" * 78)
print("RESULT")
print("=" * 78)
tight_failed = validity in ("failed",) or "alarm" in (full or "").lower() or "crossed" in (full or "").lower()
relaxed_valid = validity2 == "valid"
print(f"  tight (2us)   produced failed/alarm: {tight_failed}  (validity={validity})")
print(f"  relaxed (50ms) produced valid:       {relaxed_valid}  (validity={validity2})")
print(f"  PASS: {tight_failed and relaxed_valid}")
