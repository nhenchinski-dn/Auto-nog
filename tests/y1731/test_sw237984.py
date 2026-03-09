import paramiko
import time
import re
import json
import threading
from datetime import datetime, timezone

DEVICE_IP = "100.64.3.48"
USERNAME = "dnroot"
PASSWORD = "dnroot"

def clean_ansi(text):
    ansi_escape = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07|\x1b[()][AB012]|\x1b\[\?[0-9;]*[hlm]|\r')
    return ansi_escape.sub('', text).strip()

def create_shell(ip, username, password, label=""):
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(ip, username=username, password=password, timeout=30, look_for_keys=False, allow_agent=False)
    chan = ssh.invoke_shell(width=300, height=1000)
    time.sleep(5)
    chan.recv(65535)
    print(f"  [SSH {label}] Connected to {ip}")
    return ssh, chan

def run_cmd(chan, cmd, wait=10):
    chan.send(cmd + '\n')
    time.sleep(wait)
    out = b''
    while chan.recv_ready():
        out += chan.recv(65535)
    return clean_ansi(out.decode(errors='replace'))

def run_cmd_async(chan, cmd):
    """Send command without waiting - for long-running run commands."""
    chan.send(cmd + '\n')

def drain_channel(chan, wait=2):
    time.sleep(wait)
    out = b''
    while chan.recv_ready():
        out += chan.recv(65535)
    return clean_ansi(out.decode(errors='replace'))

results = {}
timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

DM_CMD = "run ethernet-oam cfm on-demand delay-measurement two-way maintenance-domain MD-CUST maintenance-association MA-CUST target mep-id 2"
SLM_CMD = "run ethernet-oam cfm on-demand synthetic-loss-measurement maintenance-domain MD-CUST maintenance-association MA-CUST target mep-id 2"
SHOW_OD = "show services performance-monitoring cfm tests on-demand | no-more"
SHOW_OD_DETAIL = "show services performance-monitoring cfm tests on-demand detail | no-more"
STOP_ALL = "request ethernet-oam cfm on-demand stop all"
STOP_DM_MD = "request ethernet-oam cfm on-demand stop maintenance-domain MD-CUST maintenance-association MA-CUST test-type two-way-delay-measurement"
STOP_SLM_MD = "request ethernet-oam cfm on-demand stop maintenance-domain MD-CUST maintenance-association MA-CUST test-type two-way-synthetic-loss-measurement"
STOP_DM_TYPE = "request ethernet-oam cfm on-demand stop test-type two-way-delay-measurement"

def section(title):
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}")

print(f"=== SW-237984 Test Execution Started: {timestamp} ===")
print(f"Device: ncpl-cfm-nog (xec1e3vr00008) @ {DEVICE_IP}")
print(f"Test: Ethernet OAM Y.1731 | CLI | request ethernet-oam cfm on-demand stop")
print()

print("Establishing 3 SSH sessions...")
ssh1, chan1 = create_shell(DEVICE_IP, USERNAME, PASSWORD, "DM")
ssh2, chan2 = create_shell(DEVICE_IP, USERNAME, PASSWORD, "SLM")
ssh3, chan3 = create_shell(DEVICE_IP, USERNAME, PASSWORD, "Control")
print("All sessions ready.\n")

section("PRE-CHECK: System Version")
ver_out = run_cmd(chan3, "show system version | no-more", wait=5)
print(ver_out)
results['version'] = ver_out

section("PRE-CHECK: Current On-Demand Sessions")
pre_show = run_cmd(chan3, SHOW_OD, wait=5)
print(pre_show)
results['pre_check'] = pre_show

# ============================================================
section("STEP 1 & 2: Start DM two-way and SLM simultaneously")
# ============================================================
print(f"\n[Session 1] CMD: {DM_CMD}")
run_cmd_async(chan1, DM_CMD)
time.sleep(0.5)
print(f"[Session 2] CMD: {SLM_CMD}")
run_cmd_async(chan2, SLM_CMD)
time.sleep(3)

# ============================================================
section("STEP 3: Verify sessions are running")
# ============================================================
step3_show = run_cmd(chan3, SHOW_OD, wait=5)
print(step3_show)
results['step3_show'] = step3_show

step3_detail = run_cmd(chan3, SHOW_OD_DETAIL, wait=5)
print(step3_detail)
results['step3_detail'] = step3_detail

# ============================================================
section("STEP 4: Stop all on-demand sessions (from Session 3)")
# ============================================================
print(f"CMD: {STOP_ALL}")
step4_stop = run_cmd(chan3, STOP_ALL, wait=8)
print(step4_stop)
results['step4_stop'] = step4_stop

time.sleep(5)
step1_dm_out = drain_channel(chan1, wait=2)
print(f"\n[Session 1 DM final output]:\n{step1_dm_out}")
results['step1_dm'] = step1_dm_out

step2_slm_out = drain_channel(chan2, wait=2)
print(f"\n[Session 2 SLM final output]:\n{step2_slm_out}")
results['step2_slm'] = step2_slm_out

# ============================================================
section("STEP 5: Verify sessions stopped/cleared")
# ============================================================
step5_show = run_cmd(chan3, SHOW_OD, wait=5)
print(step5_show)
results['step5_show'] = step5_show

step5_detail_dm = run_cmd(chan3, "show services performance-monitoring cfm tests on-demand two-way-delay detail | no-more", wait=5)
print(step5_detail_dm)
results['step5_detail_dm'] = step5_detail_dm

step5_detail_slm = run_cmd(chan3, "show services performance-monitoring cfm tests on-demand two-way-synthetic-loss detail | no-more", wait=5)
print(step5_detail_slm)
results['step5_detail_slm'] = step5_detail_slm

# ============================================================
section("STEP 6a: Stop variant - DM only (with MD/MA filter)")
# ============================================================
print(f"[Session 1] Starting DM: {DM_CMD}")
run_cmd_async(chan1, DM_CMD)
time.sleep(3)

print(f"\nCMD: {STOP_DM_MD}")
step6a_stop = run_cmd(chan3, STOP_DM_MD, wait=8)
print(step6a_stop)
results['step6a_stop'] = step6a_stop

time.sleep(5)
step6a_dm_out = drain_channel(chan1, wait=2)
print(f"[Session 1 DM output after stop]:\n{step6a_dm_out}")
results['step6a_dm'] = step6a_dm_out

step6a_show = run_cmd(chan3, SHOW_OD, wait=5)
print(step6a_show)
results['step6a_show'] = step6a_show

# ============================================================
section("STEP 6b: Stop variant - SLM only (with MD/MA filter)")
# ============================================================
print(f"[Session 2] Starting SLM: {SLM_CMD}")
run_cmd_async(chan2, SLM_CMD)
time.sleep(3)

print(f"\nCMD: {STOP_SLM_MD}")
step6b_stop = run_cmd(chan3, STOP_SLM_MD, wait=8)
print(step6b_stop)
results['step6b_stop'] = step6b_stop

time.sleep(5)
step6b_slm_out = drain_channel(chan2, wait=2)
print(f"[Session 2 SLM output after stop]:\n{step6b_slm_out}")
results['step6b_slm'] = step6b_slm_out

step6b_show = run_cmd(chan3, SHOW_OD, wait=5)
print(step6b_show)
results['step6b_show'] = step6b_show

# ============================================================
section("STEP 6c: Stop variant - by test-type only (no MD/MA)")
# ============================================================
print(f"[Session 1] Starting DM: {DM_CMD}")
run_cmd_async(chan1, DM_CMD)
time.sleep(3)

print(f"\nCMD: {STOP_DM_TYPE}")
step6c_stop = run_cmd(chan3, STOP_DM_TYPE, wait=8)
print(step6c_stop)
results['step6c_stop'] = step6c_stop

time.sleep(5)
step6c_dm_out = drain_channel(chan1, wait=2)
print(f"[Session 1 DM output after stop]:\n{step6c_dm_out}")
results['step6c_dm'] = step6c_dm_out

step6c_show = run_cmd(chan3, SHOW_OD, wait=5)
print(step6c_show)
results['step6c_show'] = step6c_show

# ============================================================
section("STEP 6d: Stop variant - both DM+SLM running, stop DM only")
# ============================================================
print(f"[Session 1] Starting DM: {DM_CMD}")
run_cmd_async(chan1, DM_CMD)
time.sleep(0.5)
print(f"[Session 2] Starting SLM: {SLM_CMD}")
run_cmd_async(chan2, SLM_CMD)
time.sleep(3)

print(f"\nStopping DM only: {STOP_DM_MD}")
step6d_stop = run_cmd(chan3, STOP_DM_MD, wait=8)
print(step6d_stop)
results['step6d_stop'] = step6d_stop

step6d_show_mid = run_cmd(chan3, SHOW_OD, wait=5)
print(f"After stopping DM, SLM should still be running:\n{step6d_show_mid}")
results['step6d_show_mid'] = step6d_show_mid

time.sleep(8)
step6d_dm_out = drain_channel(chan1, wait=2)
results['step6d_dm'] = step6d_dm_out
step6d_slm_out = drain_channel(chan2, wait=2)
results['step6d_slm'] = step6d_slm_out
print(f"[DM output]: {step6d_dm_out}")
print(f"[SLM output]: {step6d_slm_out}")

# ============================================================
section("STEP 7: Negative test - stop with no active sessions")
# ============================================================
time.sleep(3)
verify_empty = run_cmd(chan3, SHOW_OD, wait=5)
print(f"Current sessions (should be idle):\n{verify_empty}")

print(f"\nCMD: {STOP_ALL}")
step7_stop = run_cmd(chan3, STOP_ALL, wait=8)
print(step7_stop)
results['step7_stop'] = step7_stop

# ============================================================
section("STEP 8: Start new on-demand DM session after stop (recovery)")
# ============================================================
print(f"[Session 1] CMD: {DM_CMD}")
step8_dm = run_cmd(chan1, DM_CMD, wait=15)
print(step8_dm)
results['step8_dm'] = step8_dm

step8_show = run_cmd(chan3, SHOW_OD, wait=5)
print(step8_show)
results['step8_show'] = step8_show

step8_detail = run_cmd(chan3, "show services performance-monitoring cfm tests on-demand two-way-delay detail | no-more", wait=5)
print(step8_detail)
results['step8_detail'] = step8_detail

# Cleanup
print("\n=== Closing SSH sessions ===")
ssh1.close()
ssh2.close()
ssh3.close()

print(f"\n=== SW-237984 Test Execution Complete: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} ===")

with open('/home/dn/sw237984_results.json', 'w') as f:
    json.dump(results, f, indent=2)
print("Results saved to /home/dn/sw237984_results.json")
