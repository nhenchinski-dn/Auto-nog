#!/usr/bin/env python3
"""SW-216153 ETH-DM link-break / recovery scenario.

1) Capture DM + CFM baseline (rmep-ok, no defects, DMR success).
2) Admin-disable ge400-0/0/33 sub-interface .100 on DUT-B (breaks L2 path
   without disturbing the physical link — fail mode we care about for CFM).
3) Wait ~15s for DefRemoteCCM to fire on DUT-A, then read alarms/defects and
   DM detail. Expect:
     - DUT-A MEP 1: Active alarm = def-remote-ccm
     - Remote MEP 2: 'rmep-failed' and Missing Remote MEPs = 1
     - In-progress DM test: success rate drops, eventually 'invalid' or 'valid with 0% success'
4) Re-enable .100 sub-interface on DUT-B; wait ~20s; confirm:
     - rmep-ok returns
     - New DM tests complete as 'valid' with 100% DMR success
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


def open_ch(host):
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(host, username="dnroot", password="dnroot",
                look_for_keys=False, allow_agent=False, timeout=15)
    ch = cli.invoke_shell(width=250, height=10000)
    time.sleep(5); read(ch, t=15, q=1.5)
    return cli, ch


MEP1 = ("show services ethernet-oam connectivity-fault-management "
        "maintenance-domains MD-SW216153 maintenance-associations MA-SW216153 "
        "mep 1 | no-more")
DM = ("show services performance-monitoring cfm tests proactive "
      "two-way-delay session DM-SW216153-1 detail | no-more")


def check_state(label):
    print(f"\n{'='*78}\n== {label}\n{'='*78}")
    cli, ch = open_ch("xec1e3vr00008")
    mep = send(ch, MEP1, t=30, hide=True)
    dm = send(ch, DM, t=30, hide=True)
    cli.close()

    active = re.search(r"Active alarm:\s*(\S+)", mep)
    rmep_state = re.search(r"\| 2\s+\|[^|]+\|\s*(\S+)\s*\|", mep)
    miss = re.search(r"Missing Remote MEPs count:\s*(\d+)", mep)
    succ = re.search(r"Success rate:\s+([\d.]+)%", dm)
    val = re.search(r"Measurement validity:\s*(\S+)", dm)
    hist = re.findall(
        r"\|\s*(\d+)\s*\|\s*[\d-]+ [\d:]+\s+\+\d+\s*\|\s*[\d-]+ [\d:]+\s+\+\d+\s*\|\s*(\w+)\s*\|",
        dm)
    print(f"  MEP 1 active alarm:       {active.group(1) if active else '?'}")
    print(f"  Remote MEP 2 CCM-RX:      {rmep_state.group(1) if rmep_state else '?'}")
    print(f"  Missing Remote MEPs:      {miss.group(1) if miss else '?'}")
    print(f"  DM latest validity:       {val.group(1) if val else '?'}")
    print(f"  DM latest success rate:   {succ.group(1) if succ else '?'}%")
    print(f"  Last 5 completed tests:   {hist[-5:]}")
    return {
        "active_alarm": active.group(1) if active else None,
        "rmep": rmep_state.group(1) if rmep_state else None,
        "missing": int(miss.group(1)) if miss else None,
        "validity": val.group(1) if val else None,
        "success": float(succ.group(1)) if succ else None,
        "history": hist,
    }


def toggle_dutb(enable):
    label = "ENABLE" if enable else "DISABLE"
    print(f"\n{'#'*78}\n# DUT-B: {label} ge400-0/0/33.100\n{'#'*78}")
    cli, ch = open_ch("WKY1C7VD00008P2")
    send(ch, "configure")
    send(ch, f"interfaces ge400-0/0/33.100 admin-state {'enabled' if enable else 'disabled'}")
    send(ch, "commit and-exit", t=60)
    cli.close()


print("=" * 78); print("STEP 0 — baseline"); print("=" * 78)
base = check_state("baseline")

print("\n" + "=" * 78); print("STEP 1 — disable DUT-B's L2 sub-interface"); print("=" * 78)
toggle_dutb(enable=False)

print("\n--- waiting 15s for CCM loss-threshold (3 * 1s) to trip ---")
time.sleep(15)
broken = check_state("after disable (t+15s)")

print("\n--- waiting another 60s for a DM window to complete under failure ---")
time.sleep(60)
broken2 = check_state("after disable (t+75s)")

print("\n" + "=" * 78); print("STEP 2 — re-enable DUT-B's L2 sub-interface"); print("=" * 78)
toggle_dutb(enable=True)

print("\n--- waiting 20s for CCM re-establish ---")
time.sleep(20)
recovered = check_state("after re-enable (t+20s)")

print("\n--- waiting another 70s for a DM window to complete under recovery ---")
time.sleep(70)
recovered2 = check_state("after re-enable (t+90s)")

print("\n" + "=" * 78); print("RESULT"); print("=" * 78)
fault_fired = broken["active_alarm"] in ("def-remote-ccm",) and broken["rmep"] in ("rmep-failed",)
dm_affected = broken2["success"] is not None and broken2["success"] < 50
recovered_ok = recovered2["active_alarm"] == "none" and recovered2["rmep"] == "rmep-ok" \
               and recovered2["validity"] == "valid" and (recovered2["success"] or 0) >= 99
print(f"  fault fired on disable:    {fault_fired}  "
      f"(alarm={broken['active_alarm']}, rmep={broken['rmep']})")
print(f"  DM success dropped:        {dm_affected}  (success={broken2['success']}%)")
print(f"  full recovery:             {recovered_ok}  "
      f"(alarm={recovered2['active_alarm']}, rmep={recovered2['rmep']}, "
      f"val={recovered2['validity']}, success={recovered2['success']}%)")
print(f"\n  RESULT: {'PASS' if fault_fired and dm_affected and recovered_ok else 'FAIL'}")
