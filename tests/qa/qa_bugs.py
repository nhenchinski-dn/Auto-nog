#!/usr/bin/env python3
"""Bug verification for Y.1731 Proactive PM (SW-141523)."""
import pexpect, re, time, sys
sys.stdout.reconfigure(line_buffering=True)

HOST = "WKY1C7VD00008P2"
USER = "dnroot"
PASS = "dnroot"
P = r'[\w\-\(\)]+[#>]\s*\Z'
M = r'(?:-- More --|-- End --|\(Press q to quit\))'
C = r'\(yes/no(?:/cancel)?\)\s*\[(?:cancel|no)\]\?'

child = pexpect.spawn(
    f"sshpass -p '{PASS}' ssh -tt -o StrictHostKeyChecking=no "
    f"-o PreferredAuthentications=password,keyboard-interactive "
    f"-o PubkeyAuthentication=no {USER}@{HOST}",
    encoding='utf-8', timeout=60, maxread=200000)
child.expect(P)
print('Connected to DNOS CLI!', flush=True)

def x(c, t=30):
    child.sendline(c)
    o = ''
    while True:
        i = child.expect([P, M, C, pexpect.TIMEOUT, pexpect.EOF], timeout=t)
        o += child.before
        if i == 0:
            break
        elif i == 1:
            child.send(' ')
        elif i == 2:
            o += child.after
            child.sendline('no')
            try:
                child.expect(P, timeout=10)
            except:
                pass
            break
        elif i == 4:
            print(f'  [EOF!]', flush=True)
            break
        else:
            print(f'  [TIMEOUT]', flush=True)
            break
    o = re.sub(r'\x1b\[[0-9;]*[mKHJrl]', '', o)
    o = re.sub(r'\x1b\[\?[0-9;]*[hl]', '', o)
    return o.replace('\r\n','\n').replace('\r','').strip()

def h(t):
    print(f"\n{'='*70}\n  {t}\n{'='*70}", flush=True)

results = []
def verdict(name, passed, detail=""):
    s = "PASS" if passed else "FAIL"
    results.append((name, passed, detail))
    print(f"\n  [{s}] {name}", flush=True)
    if detail:
        print(f"         {detail}", flush=True)

# ====================================================================
h("TEST 1: Verify proactive sessions are running")
out = x("show services performance-monitoring cfm tests proactive")
print(out, flush=True)
verdict("Proactive DM session visible", "dm-test-1" in out and "Ongoing" in out)
verdict("Proactive SLM session visible", "slm-test-1" in out and "Ongoing" in out)

# ====================================================================
h("TEST 2: Session cycling - Last Run timestamp changes")
match1 = re.search(r'dm-test-1.*?(\d{2}:\d{2}:\d{2})', out)
ts1 = match1.group(1) if match1 else ""
print(f"  DM Last Run T=0: {ts1}", flush=True)
print("  Waiting 20s for next cycle...", flush=True)
time.sleep(20)
out2 = x("show services performance-monitoring cfm tests proactive")
print(out2, flush=True)
match2 = re.search(r'dm-test-1.*?(\d{2}:\d{2}:\d{2})', out2)
ts2 = match2.group(1) if match2 else ""
print(f"  DM Last Run T=20: {ts2}", flush=True)
verdict("DM session cycling (timestamp advances)", ts1 != "" and ts2 != "" and ts1 != ts2,
        f"{ts1} -> {ts2}")

# ====================================================================
h("TEST 3: NI-06 Delete MD-CUST while proactive sessions active")
x("configure")
out = x("no services ethernet-oam connectivity-fault-management maintenance-domains MD-CUST")
print(f"  Delete MD cmd: {out}", flush=True)
out = x("commit", t=30)
print(f"  Commit result: {out[-400:]}", flush=True)
rejected = "error" in out.lower() or "abort" in out.lower() or "reject" in out.lower() or "fail" in out.lower()
verdict("NI-06: Commit rejects MD deletion with active proactive", rejected, out[-200:])
x("rollback")
x("exit")

# ====================================================================
h("TEST 4: NI-08 Delete local MEP-2 while proactive sessions active")
x("configure")
x("services ethernet-oam connectivity-fault-management maintenance-domains MD-CUST")
x("maintenance-associations MA-CUST")
out = x("no local-mep 2")
print(f"  Delete MEP cmd: {out}", flush=True)
x("top")
out = x("commit", t=30)
print(f"  Commit result: {out[-400:]}", flush=True)
rejected = "error" in out.lower() or "abort" in out.lower() or "reject" in out.lower() or "fail" in out.lower()
verdict("NI-08: Commit rejects MEP deletion with active proactive", rejected, out[-200:])
x("rollback")
x("exit")

# ====================================================================
h("TEST 5: BUG-08 Modify running proactive DM target")
print("  Before state:", flush=True)
before = x("show services performance-monitoring cfm tests proactive")
print(before, flush=True)

x("configure")
x("services performance-monitoring")
x("cfm")
x("two-way-delay-measurement dm-test-1")
out = x("target mac-address 84:40:76:bd:58:fb")
print(f"\n  Change target to MAC: {out}", flush=True)
x("top")
out = x("commit", t=60)
print(f"  Commit: {out[-300:]}", flush=True)
x("exit")

print("  Waiting 25s for session restart...", flush=True)
time.sleep(25)

after = x("show services performance-monitoring cfm tests proactive")
print(f"  After modification:\n{after}", flush=True)

# Check if target field changed
before_target = re.search(r'dm-test-1\s+\|[^|]+\|[^|]+\|[^|]+\|[^|]+\|\s*(\S+)', before)
after_target = re.search(r'dm-test-1\s+\|[^|]+\|[^|]+\|[^|]+\|[^|]+\|\s*(\S+)', after)
bt = before_target.group(1) if before_target else ""
at = after_target.group(1) if after_target else ""
print(f"  Target before: '{bt}', after: '{at}'", flush=True)

if "84:40:76:bd:58:fb" in after or at != bt:
    verdict("BUG-08: Session params updated after modification", True,
            f"Target changed from '{bt}' to '{at}'")
else:
    verdict("BUG-08: Session params NOT updated (insert silently dropped)",
            False, "This confirms BUG-08: AddSession uses insert() not insert_or_assign()")

# Revert target back to mep-id 1
x("configure")
x("services performance-monitoring")
x("cfm")
x("two-way-delay-measurement dm-test-1")
x("target mep-id 1")
x("top")
out = x("commit", t=60)
print(f"  Revert: {out[-200:]}", flush=True)
x("exit")

# ====================================================================
h("TEST 6: Second proactive session on different MEP")
x("configure")
x("services performance-monitoring")
x("cfm")
x("two-way-delay-measurement dm-test-2")
x("source maintenance-domain MD-CUST1 maintenance-association MA-CUST1 mep-id 4")
x("target mep-id 3")
x("profile dm-prof1")
x("admin-state enabled")
x("top")
out = x("commit", t=60)
print(f"  Commit 2nd session: {out[-300:]}", flush=True)
x("exit")

time.sleep(15)
out = x("show services performance-monitoring cfm tests proactive")
print(f"  With 3 sessions:\n{out}", flush=True)
dm2_found = "dm-test-2" in out
verdict("Second DM session on different MEP", dm2_found)

# Cleanup dm-test-2
x("configure")
x("no services performance-monitoring cfm two-way-delay-measurement dm-test-2")
x("top")
x("commit", t=60)
x("exit")

# ====================================================================
h("TEST 7: Disable and re-enable proactive session")
x("configure")
x("services performance-monitoring")
x("cfm")
x("two-way-delay-measurement dm-test-1")
x("admin-state disabled")
x("top")
out = x("commit", t=60)
print(f"  Disable commit: {out[-200:]}", flush=True)
x("exit")

time.sleep(5)
out = x("show services performance-monitoring cfm tests proactive")
print(f"  After disable:\n{out}", flush=True)
dm_disabled = "dm-test-1" not in out or "disabled" in out.lower()

# Re-enable
x("configure")
x("services performance-monitoring")
x("cfm")
x("two-way-delay-measurement dm-test-1")
x("admin-state enabled")
x("top")
out = x("commit", t=60)
print(f"  Re-enable commit: {out[-200:]}", flush=True)
x("exit")

time.sleep(15)
out = x("show services performance-monitoring cfm tests proactive")
print(f"  After re-enable:\n{out}", flush=True)
dm_reenabled = "dm-test-1" in out and "Ongoing" in out
verdict("Disable/re-enable proactive session", dm_reenabled)

# ====================================================================
h("TEST 8: Show all tests (on-demand + proactive)")
out = x("show services performance-monitoring cfm tests")
print(out, flush=True)

# ====================================================================
# SUMMARY
# ====================================================================
print(f"\n{'='*70}", flush=True)
print(f"  RESULTS SUMMARY", flush=True)
print(f"{'='*70}", flush=True)
pass_count = sum(1 for _, p, _ in results if p)
fail_count = sum(1 for _, p, _ in results if not p)
for name, passed, detail in results:
    s = "PASS" if passed else "FAIL"
    print(f"  [{s}] {name}", flush=True)
    if detail and not passed:
        print(f"         {detail}", flush=True)
print(f"\n  Passed: {pass_count}, Failed: {fail_count}, Total: {len(results)}", flush=True)

child.sendline("exit")
try:
    child.expect(pexpect.EOF, timeout=5)
except:
    pass
print(f"\n{'='*70}\n  ALL TESTS COMPLETED\n{'='*70}", flush=True)
