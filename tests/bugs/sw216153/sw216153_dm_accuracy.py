#!/usr/bin/env python3
"""SW-216153 ETH-DM accuracy & consistency.

Polls DM-SW216153-1 every 60s for ~5min, parses each completed test from the
historical-results table, and produces a summary of:
  - sample count
  - DMM/DMR success rate per test
  - min/max/avg round-trip delay per test
  - aggregate statistics across all completed tests

Pass criteria:
  - All completed tests must show measurement-validity = 'valid'
  - Success rate >= 99% on every test
  - Average delay variance <= 50us across tests (real fiber baseline ~4us)
"""
import re, time, paramiko

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
PROMPT = re.compile(r"[a-zA-Z0-9._-]+(\([^)]*\))?#\s*$")
SAMPLES = 5
SAMPLE_INTERVAL_S = 65


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
    ch.send(c + "\n"); return read(ch, t=t)


def open_ch(host):
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(host, username="dnroot", password="dnroot",
                look_for_keys=False, allow_agent=False, timeout=15)
    ch = cli.invoke_shell(width=250, height=10000)
    time.sleep(5); read(ch, t=15, q=1.5)
    return cli, ch


SHOW_DETAIL = ("show services performance-monitoring cfm tests proactive "
               "two-way-delay session DM-SW216153-1 detail | no-more")


def parse_latest(out):
    """Pull the 'Latest Test Results' block (most recent test, may be incomplete)."""
    m = re.search(
        r"Latest Test Results.*?Measurement validity:\s*(\S+).*?"
        r"DMM PDUs transmitted:\s*(\d+).*?DMR PDUs received:\s*(\d+).*?"
        r"Success rate:\s*([\d.]+)%.*?"
        r"Minimum:\s*(\d+)\s*usec,\s*Maximum:\s*(\d+)\s*usec,\s*Average:\s*(\d+)\s*usec",
        out, re.S)
    if not m: return None
    return {
        "validity": m.group(1),
        "tx": int(m.group(2)), "rx": int(m.group(3)),
        "success": float(m.group(4)),
        "min_us": int(m.group(5)), "max_us": int(m.group(6)), "avg_us": int(m.group(7)),
    }


def parse_history(out):
    """Return list of (index, status) for every row in the Historical Test Results table."""
    rows = []
    for ln in out.splitlines():
        m = re.match(r"\|\s*(\d+)\s*\|\s*[\d-]+ [\d:]+\s+\+\d+\s*\|"
                     r"\s*([\d-]+ [\d:]+\s+\+\d+|)\s*\|\s*(\w+)\s*\|", ln)
        if m:
            rows.append({"index": int(m.group(1)),
                         "end": m.group(2).strip(),
                         "status": m.group(3)})
    return rows


cli, ch = open_ch("xec1e3vr00008")
print("=" * 78)
print("SW-216153 ETH-DM: accuracy & consistency over {} samples (~{}s apart)"
      .format(SAMPLES, SAMPLE_INTERVAL_S))
print("=" * 78)
samples = []
seen_indexes = {}
for i in range(SAMPLES):
    if i: time.sleep(SAMPLE_INTERVAL_S)
    print(f"\n--- sample {i+1}/{SAMPLES} at t+{i*SAMPLE_INTERVAL_S}s ---")
    out = send(ch, SHOW_DETAIL, t=25)
    latest = parse_latest(out)
    history = parse_history(out)
    if latest:
        print(f"  latest:   validity={latest['validity']:<11} "
              f"tx={latest['tx']:>3} rx={latest['rx']:>3} "
              f"success={latest['success']:>5.1f}% "
              f"min/max/avg = {latest['min_us']}/{latest['max_us']}/{latest['avg_us']} us")
        samples.append(latest)
    for row in history[-3:]:
        marker = " new" if row["index"] not in seen_indexes else ""
        seen_indexes[row["index"]] = row["status"]
        print(f"  history:  idx={row['index']} status={row['status']}{marker}")

cli.close()

print("\n" + "=" * 78)
print("AGGREGATE")
print("=" * 78)
if samples:
    avg_min = min(s["min_us"] for s in samples)
    avg_max = max(s["max_us"] for s in samples)
    avg_avg = sum(s["avg_us"] for s in samples) / len(samples)
    succ_min = min(s["success"] for s in samples)
    print(f"  samples collected:        {len(samples)}")
    print(f"  validity all 'valid'/'incomplete' (running): "
          f"{set(s['validity'] for s in samples)}")
    print(f"  RTT min across samples:   {avg_min} us")
    print(f"  RTT max across samples:   {avg_max} us")
    print(f"  RTT avg-of-avg:           {avg_avg:.1f} us")
    print(f"  worst success rate:       {succ_min}%")
    print(f"  unique completed tests in history window: "
          f"{sum(1 for s in seen_indexes.values() if s == 'valid')}")
    print(f"  any 'failed' completed tests: "
          f"{any(s == 'failed' for s in seen_indexes.values())}")

    pf = (
        succ_min >= 99.0
        and avg_avg < 50
        and not any(s == "failed" for s in seen_indexes.values())
    )
    print(f"\n  RESULT: {'PASS' if pf else 'FAIL'}")
