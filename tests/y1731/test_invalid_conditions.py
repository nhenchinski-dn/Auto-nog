#!/usr/bin/env python3
"""
Test measurement initiation failure conditions and recovery behavior.
Conditions:
  1. CFM commit in progress during measurement
  2. On-demand overlap (same protocol, same source MEP)
  3. RMEP down (target mep-id with unavailable dst MAC)
"""
import sys, time, re, paramiko

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

def run_seq(client, commands, timeout=30):
    ch = client.invoke_shell()
    ch.settimeout(timeout)
    time.sleep(1.5)
    while ch.recv_ready():
        ch.recv(65536)
    results = []
    for cmd in commands:
        ch.send(cmd + "\n")
        out = ""
        end_t = time.time() + timeout
        last_data = time.time()
        while time.time() < end_t:
            if ch.recv_ready():
                out += ch.recv(65536).decode(errors="ignore")
                last_data = time.time()
            else:
                if time.time() - last_data > 3:
                    break
                time.sleep(0.2)
        results.append((cmd, ANSI.sub("", out)))
    ch.close()
    return results

def run_single(client, cmd, timeout=20):
    return run_seq(client, [cmd], timeout=timeout)[0][1]

def get_detail(client, session_name, test_type="two-way-delay"):
    return run_single(client,
        f"show services performance-monitoring cfm tests proactive {test_type} session-name {session_name} detail | no-more")

def get_last_entries(detail_text):
    entries = re.findall(r"\|\s*(\d+)\s*\|[^|]*\|[^|]*\|\s*(\w+)\s*\|", detail_text)
    return entries

def finding(severity, title, detail):
    tag = {"BUG": "!!!", "OBSERVATION": "???", "OK": "   "}.get(severity, "   ")
    print(f"  [{tag}] [{severity}] {title}")
    print(f"        {detail[:250]}")

# Connect to NCP3 (where on-demand interference was observed)
host = "WKY1C7VD00008P2"
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(host, username="dnroot", password="dnroot", timeout=15, banner_timeout=15, auth_timeout=15)
print(f"Connected to {host}")

# ================================================================
# TEST A: Condition #2 recovery -- on-demand overlap then recovery
# ================================================================
print(f"\n{'='*70}")
print("TEST A: On-demand overlap -> invalid -> recovery to valid")
print(f"{'='*70}")

# Get baseline
detail0 = get_detail(client, "DM_CLI_TAB")
entries0 = get_last_entries(detail0)
print(f"  Baseline: last entries = {entries0[-3:]}")

# Run on-demand DM on same MEP (MEP 2, same protocol=DM)
print("  Running on-demand DM (5 probes) to trigger condition #2...")
od_out = run_single(client,
    "run ethernet-oam cfm on-demand delay-measurement two-way maintenance-domain MD-CUST maintenance-association MA-CUST target mep-id 1 count 5",
    timeout=20)
print(f"  On-demand complete.")

# Immediately check - should see invalid entry
time.sleep(2)
detail1 = get_detail(client, "DM_CLI_TAB")
entries1 = get_last_entries(detail1)
print(f"  Right after on-demand: last entries = {entries1[-3:]}")
invalid_count_1 = sum(1 for _, s in entries1 if s == "invalid")

# Wait for next proactive cycle to complete (profile=test, 60 probes, 1s interval, 60s repeat)
# So need to wait up to ~120s for a full cycle to finish
print("  Waiting 130s for proactive DM to complete a full cycle and recover...")
time.sleep(130)

detail2 = get_detail(client, "DM_CLI_TAB")
entries2 = get_last_entries(detail2)
print(f"  After recovery wait: last entries = {entries2[-3:]}")
invalid_count_2 = sum(1 for _, s in entries2 if s == "invalid")

# Check if the latest completed entry is valid (recovery)
latest_valid = entries2 and entries2[-1][1] in ("valid", "incomplete")
if latest_valid and invalid_count_2 <= invalid_count_1:
    finding("OK", "Proactive DM recovers to valid after on-demand completes",
           f"Before: {invalid_count_1} invalid entries. After wait: {invalid_count_2}. Latest={entries2[-1] if entries2 else 'none'}")
else:
    finding("BUG", "Proactive DM does NOT recover after on-demand completes",
           f"Expected recovery to valid. Before: {invalid_count_1} invalid. After wait: {invalid_count_2} invalid. Entries: {entries2[-3:]}")

# ================================================================
# TEST B: Condition #1 -- CFM commit during active measurement
# ================================================================
print(f"\n{'='*70}")
print("TEST B: CFM config commit during active proactive measurement")
print(f"{'='*70}")

detail_b0 = get_detail(client, "DM_CLI_TAB")
entries_b0 = get_last_entries(detail_b0)
print(f"  Baseline: last entries = {entries_b0[-3:]}")

# Do a CFM-related commit while proactive test is running
# We'll modify the remote-meps config (add a dummy crosscheck)
print("  Committing a CFM config change (add dummy crosscheck mep-id 99)...")
run_seq(client, [
    "configure",
    "services ethernet-oam connectivity-fault-management maintenance-domains MD-CUST maintenance-associations MA-CUST",
    "remote-meps crosscheck mep-id 99",
    "exit", "exit", "exit", "exit", "exit",
    "commit",
    "exit",
], timeout=30)
print("  CFM commit done.")

time.sleep(5)
detail_b1 = get_detail(client, "DM_CLI_TAB")
entries_b1 = get_last_entries(detail_b1)
invalid_b1 = sum(1 for _, s in entries_b1 if s == "invalid")
print(f"  After CFM commit: last entries = {entries_b1[-3:]}, invalid count = {invalid_b1}")

# Revert the config change
print("  Reverting CFM change (remove dummy crosscheck)...")
run_seq(client, [
    "configure",
    "services ethernet-oam connectivity-fault-management maintenance-domains MD-CUST maintenance-associations MA-CUST",
    "no remote-meps crosscheck mep-id 99",
    "exit", "exit", "exit", "exit", "exit",
    "commit",
    "exit",
], timeout=30)

# Wait for recovery
print("  Waiting 130s for recovery cycle...")
time.sleep(130)
detail_b2 = get_detail(client, "DM_CLI_TAB")
entries_b2 = get_last_entries(detail_b2)
invalid_b2 = sum(1 for _, s in entries_b2 if s == "invalid")
print(f"  After recovery: last entries = {entries_b2[-3:]}, invalid count = {invalid_b2}")

if invalid_b1 > 0:
    if invalid_b2 < invalid_b1 or (entries_b2 and entries_b2[-1][1] in ("valid", "incomplete")):
        finding("OK", "CFM commit causes transient invalid that recovers",
               f"During commit: {invalid_b1} invalid entries. After recovery: {invalid_b2}. System recovers.")
    else:
        finding("BUG", "CFM commit causes PERSISTENT invalid state",
               f"During commit: {invalid_b1} invalid. After 130s: {invalid_b2} still invalid. No recovery!")
else:
    finding("OK", "CFM commit did not cause any invalid entries",
           f"Entries after commit: {entries_b1[-3:]}")

# ================================================================
# TEST C: Condition #3 -- RMEP down
# Check current RMEP status and test invalid when RMEP unreachable
# ================================================================
print(f"\n{'='*70}")
print("TEST C: Check RMEP status and its effect on measurement validity")
print(f"{'='*70}")

# Check RMEP status
rmep_out = run_single(client, "show services ethernet-oam connectivity-fault-management maintenance-domains MD-CUST maintenance-associations MA-CUST remote-meps | no-more")
print("  RMEP status:")
for ln in rmep_out.splitlines():
    s = ln.strip()
    if s and not s.startswith("show "):
        print(f"    {s}")

# Also check MD-CUST1 (for MEP 4 -> target 3)
rmep_out2 = run_single(client, "show services ethernet-oam connectivity-fault-management maintenance-domains MD-CUST1 maintenance-associations MA-CUST1 remote-meps | no-more")
print("\n  RMEP status (MD-CUST1):")
for ln in rmep_out2.splitlines():
    s = ln.strip()
    if s and not s.startswith("show "):
        print(f"    {s}")

# ================================================================
# TEST D: SLM to MAC -- check validity semantics
# ================================================================
print(f"\n{'='*70}")
print("TEST D: SLM validity semantics -- what makes a test 'valid'?")
print(f"{'='*70}")

# Check SLM with normal MEP target (should have good results)
slm_detail = get_detail(client, "SLM_CLI_TAB", test_type="two-way-synthetic-loss")
# Find completed entries
slm_entries = get_last_entries(slm_detail)
slm_valid = sum(1 for _, s in slm_entries if s == "valid")
slm_invalid = sum(1 for _, s in slm_entries if s == "invalid")
print(f"  SLM_CLI_TAB: {slm_valid} valid, {slm_invalid} invalid out of {len(slm_entries)} entries")

# Check the latest COMPLETED entry's loss stats
loss_match = re.search(r"Far-end loss percentage:\s*(\S+)", slm_detail)
if loss_match:
    print(f"  Latest far-end loss: {loss_match.group(1)}")
    
slr_match = re.search(r"SLR PDUs received:\s*(\d+)", slm_detail)
slm_tx_match = re.search(r"SLM PDUs transmitted:\s*(\d+)", slm_detail)
if slr_match and slm_tx_match:
    rx, tx = int(slr_match.group(1)), int(slm_tx_match.group(1))
    print(f"  Latest: TX={tx}, RX={rx}")
    if tx > 0 and rx < tx:
        finding("OBSERVATION", f"SLM shows {tx-rx} unacknowledged PDUs in latest (possibly mid-cycle)",
               f"TX={tx}, RX={rx}. If this is always mid-cycle, it's a display timing issue.")

# Now check the SLM session to fake MAC on xec1e3vr00008
print("\n  Checking xec1e3vr00008 for SLM to fake MAC validity...")
client2 = paramiko.SSHClient()
client2.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client2.connect("xec1e3vr00008", username="dnroot", password="dnroot", timeout=15, banner_timeout=15, auth_timeout=15)

slm_fake = get_detail(client2, "SLM_CLI_TAB_mep3", test_type="two-way-synthetic-loss")
slm_fake_entries = get_last_entries(slm_fake)
slm_fake_valid = sum(1 for _, s in slm_fake_entries if s == "valid")
slm_fake_invalid = sum(1 for _, s in slm_fake_entries if s == "invalid")
print(f"  SLM_CLI_TAB_mep3 (fake MAC): {slm_fake_valid} valid, {slm_fake_invalid} invalid out of {len(slm_fake_entries)} entries")

# Check loss stats
fake_loss = re.search(r"Far-end loss percentage:\s*(\S+)", slm_fake)
fake_rx = re.search(r"SLR PDUs received:\s*(\d+)", slm_fake)
fake_tx = re.search(r"SLM PDUs transmitted:\s*(\d+)", slm_fake)
if fake_loss:
    print(f"  Far-end loss: {fake_loss.group(1)}")
if fake_rx and fake_tx:
    print(f"  TX={fake_tx.group(1)}, RX={fake_rx.group(1)}")

# Key question: condition #3 says "when measurement target is remote mep-id and RMEP is down"
# The fake MAC session uses MAC target, NOT mep-id. So condition #3 does NOT apply.
# The test "completes" (sends all probes, waits for timeout) -> marked "valid"
# This is Y.1731 semantics: "valid" = measurement completed, not "link healthy"
if slm_fake_valid > 0 and fake_rx and int(fake_rx.group(1)) == 0:
    finding("OBSERVATION",
           "SLM to unreachable MAC target: 'valid' means measurement completed, not link healthy",
           f"{slm_fake_valid} entries marked valid with 0 SLR responses. "
           f"Per condition #3, only mep-id targets check RMEP status. MAC targets always attempt measurement. "
           f"Whether 'valid' is correct depends on Y.1731 semantics for measurement completion vs success.")

# Also check: does a threshold violation fire for the fake MAC session?
# (it has thresholds configured: near-end-loss 1.01, far-end-loss 23.2311)
# 100% far-end loss > 23.23% threshold -> should fire event
print("\n  Checking if threshold violation fires for fake MAC SLM session...")
log_ch = client2.invoke_shell()
log_ch.settimeout(5)
time.sleep(1.5)
while log_ch.recv_ready():
    log_ch.recv(65536)
log_ch.send("set logging terminal\n")
time.sleep(15)  # Wait for a cycle (repeat-interval=10s)
event_out = ""
try:
    log_ch.settimeout(2)
    while True:
        try:
            data = log_ch.recv(65536).decode(errors="ignore")
            if not data:
                break
            event_out += data
        except:
            break
except:
    pass
event_out = ANSI.sub("", event_out)
log_ch.close()

if "CFM_PROACTIVE_TEST_FAILURE" in event_out:
    # Parse which session triggered it
    if "SLM_CLI_TAB_mep3" in event_out:
        finding("OK", "Threshold violation fires for SLM to fake MAC",
               "CFM_PROACTIVE_TEST_FAILURE event generated for SLM session with 100% loss to unreachable MAC. "
               "Even though status is 'valid', the threshold mechanism works correctly.")
    else:
        finding("OBSERVATION", "Threshold event fires but for a different session",
               f"Event content: {event_out[:200]}")
elif "PROACTIVE" in event_out or "FAILURE" in event_out:
    finding("OBSERVATION", "Some event detected", event_out[:200])
else:
    finding("BUG", "No threshold violation event for SLM with 100% far-end loss",
           f"SLM to fake MAC has 100% far-end loss, threshold is 23.23%, but no CFM_PROACTIVE_TEST_FAILURE event generated. "
           f"Logging output ({len(event_out)} bytes): '{event_out[:100]}'")

client2.close()
client.close()
print(f"\n{'='*70}")
print("ALL CONDITION TESTS COMPLETE")
print(f"{'='*70}")
