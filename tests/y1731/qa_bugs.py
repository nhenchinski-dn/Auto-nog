#!/usr/bin/env python3
"""
Bug verification tests for Y.1731 Proactive PM (SW-141523).
Assumes dm-test-1 and slm-test-1 are already configured and running.
"""
import pexpect, re, time, sys

HOST = "WKY1C7VD00008P2"
USER = "dnroot"
PASS = "dnroot"
P = r'[\w\-\(\)]+[#>]\s*\Z'
M = r'-- More --'
C = r'\[cancel\]\?'

def mk():
    child = pexpect.spawn(
        f"sshpass -p '{PASS}' ssh -tt -o StrictHostKeyChecking=no "
        f"-o PreferredAuthentications=password,keyboard-interactive "
        f"-o PubkeyAuthentication=no {USER}@{HOST}",
        encoding='utf-8', timeout=30, maxread=200000)
    child.expect(P)
    return child

def x(child, c, t=30):
    child.sendline(c)
    o = ''
    while True:
        i = child.expect([P, M, C, pexpect.TIMEOUT], timeout=t)
        o += child.before
        if i == 0: break
        elif i == 1: child.send(' ')
        elif i == 2:
            o += child.after
            child.sendline('no')
            child.expect(P, timeout=10)
            break
        else: break
    o = re.sub(r'\x1b\[[0-9;]*[mKHJrl]', '', o)
    o = re.sub(r'\x1b\[\?[0-9;]*[hl]', '', o)
    return o.replace('\r\n','\n').replace('\r','').strip()

def hdr(t): print(f"\n{'='*70}\n  {t}\n{'='*70}")

ch = mk()

# ===== TEST 1: Verify sessions running =====
hdr("VERIFY: Proactive DM + SLM sessions running")
out = x(ch, "show services performance-monitoring cfm tests proactive")
print(out)
assert "dm-test-1" in out, "dm-test-1 not found!"
assert "slm-test-1" in out, "slm-test-1 not found!"
print("\n  [OK] Both sessions visible")

# ===== TEST 2: Detailed DM results =====
hdr("DETAIL: DM test-session dm-test-1 detailed results")
out = x(ch, "show services performance-monitoring cfm tests proactive two-way-delay-measurement dm-test-1")
print(out)

# ===== TEST 3: Detailed SLM results =====
hdr("DETAIL: SLM test-session slm-test-1 detailed results")
out = x(ch, "show services performance-monitoring cfm tests proactive two-way-synthetic-loss-measurement slm-test-1")
print(out)

# ===== TEST 4: CO-01 - On-demand DM same MEP =====
hdr("CO-01: On-demand DM on same MEP while proactive DM running")
out = x(ch, "request ethernet-oam cfm on-demand two-way-delay-measurement md-name MD-CUST ma-name MA-CUST mep-id 2 target mep-id 1", t=30)
print(f"  ON-DEMAND DM: {out}")
# Check if on-demand conflicts with proactive (same sess_id)
if "error" in out.lower() or "exists" in out.lower() or "fail" in out.lower():
    print("\n  [BUG CO-01] On-demand FAILS when proactive is running on same MEP!")
    print("  Evidence: sess_compute_id(oam_id, SESS_DMM) produces same ID for both")
else:
    print("\n  [INFO] On-demand DM accepted - checking results...")
    time.sleep(8)
    out2 = x(ch, "show services performance-monitoring cfm tests on-demand")
    print(out2)

# ===== TEST 5: CO-01b - On-demand SLM same MEP =====
hdr("CO-01b: On-demand SLM on same MEP while proactive SLM running")
out = x(ch, "request ethernet-oam cfm on-demand two-way-synthetic-loss-measurement md-name MD-CUST ma-name MA-CUST mep-id 2 target mep-id 1", t=30)
print(f"  ON-DEMAND SLM: {out}")
if "error" in out.lower() or "exists" in out.lower() or "fail" in out.lower():
    print("\n  [BUG CO-01b] On-demand SLM FAILS when proactive SLM is running on same MEP!")
else:
    print("\n  [INFO] On-demand SLM accepted")
    time.sleep(8)

# ===== TEST 6: BUG-08 - Modify running proactive session =====
hdr("BUG-08: Modify proactive DM session - change target")
print("  Before: checking current state")
out = x(ch, "show services performance-monitoring cfm tests proactive two-way-delay-measurement dm-test-1")
before = out
print(out)

x(ch, "configure")
x(ch, "services performance-monitoring")
x(ch, "cfm")
x(ch, "two-way-delay-measurement dm-test-1")
# Change target to MAC address instead of mep-id
out = x(ch, "target mac-address 84:40:76:bd:58:fb")
print(f"\n  Target change to MAC: {out}")
x(ch, "top")
out = x(ch, "commit", t=60)
print(f"  Commit: {out[-300:]}")
x(ch, "exit")

time.sleep(15)
print("\n  After modification:")
out = x(ch, "show services performance-monitoring cfm tests proactive two-way-delay-measurement dm-test-1")
print(out)
after = out

# Revert to mep-id target
x(ch, "configure")
x(ch, "services performance-monitoring")
x(ch, "cfm")
x(ch, "two-way-delay-measurement dm-test-1")
x(ch, "target mep-id 1")
x(ch, "top")
out = x(ch, "commit", t=60)
print(f"  Revert commit: {out[-200:]}")
x(ch, "exit")

# ===== TEST 7: NI-06 - Delete MD with proactive active =====
hdr("NI-06: Delete MD-CUST while proactive sessions configured")
x(ch, "configure")
out = x(ch, "delete services ethernet-oam connectivity-fault-management maintenance-domains MD-CUST")
print(f"  Delete MD: {out}")
out = x(ch, "commit", t=30)
print(f"  Commit: {out[-400:]}")
rejected = "error" in out.lower() or "abort" in out.lower() or "reject" in out.lower() or "fail" in out.lower()
if rejected:
    print("\n  [PASS NI-06] Commit correctly rejected deletion of MD with proactive sessions")
else:
    print("\n  [FAIL NI-06] Commit did NOT reject - MD deleted despite active proactive sessions!")
x(ch, "exit")

# ===== TEST 8: NI-08 - Delete MEP with proactive active =====
hdr("NI-08: Delete MEP-2 while proactive sessions configured")
x(ch, "configure")
out = x(ch, "delete services ethernet-oam connectivity-fault-management maintenance-domains MD-CUST maintenance-associations MA-CUST local-mep 2")
print(f"  Delete MEP: {out}")
out = x(ch, "commit", t=30)
print(f"  Commit: {out[-400:]}")
rejected = "error" in out.lower() or "abort" in out.lower() or "reject" in out.lower() or "fail" in out.lower()
if rejected:
    print("\n  [PASS NI-08] Commit correctly rejected MEP deletion")
else:
    print("\n  [FAIL NI-08] Commit did NOT reject MEP deletion!")
x(ch, "exit")

# ===== TEST 9: HP-08 - Historic results =====
hdr("HP-08: Check historic results (N-list)")
time.sleep(10)
out = x(ch, "show services performance-monitoring cfm tests proactive two-way-delay-measurement dm-test-1")
print(out)

# ===== TEST 10: Check non-stop behavior =====
hdr("NON-STOP: Verify session keeps restarting")
out1 = x(ch, "show services performance-monitoring cfm tests proactive")
print(f"  T=0: {out1}")
print("  Waiting 30s...")
time.sleep(30)
out2 = x(ch, "show services performance-monitoring cfm tests proactive")
print(f"  T=30: {out2}")

# ===== Show all on-demand tests too =====
hdr("ON-DEMAND: All on-demand test results")
out = x(ch, "show services performance-monitoring cfm tests on-demand")
print(out)

ch.sendline("exit")
ch.expect(pexpect.EOF, timeout=5)

print(f"\n{'='*70}")
print("  ALL TESTS COMPLETED")
print(f"{'='*70}")
