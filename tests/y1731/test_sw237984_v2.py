import paramiko
import time
import re
import json
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
    print(f"  [SSH {label}] Connected")
    return ssh, chan

def run_cmd(chan, cmd, wait=10):
    chan.send(cmd + '\n')
    time.sleep(wait)
    out = b''
    while chan.recv_ready():
        out += chan.recv(65535)
    return clean_ansi(out.decode(errors='replace'))

def run_cmd_async(chan, cmd):
    chan.send(cmd + '\n')

def drain_channel(chan, wait=2):
    time.sleep(wait)
    out = b''
    while chan.recv_ready():
        out += chan.recv(65535)
    return clean_ansi(out.decode(errors='replace'))

def section(title):
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}")

# Fixed SLM command: includes "two-way" keyword
DM_CMD = "run ethernet-oam cfm on-demand delay-measurement two-way maintenance-domain MD-CUST maintenance-association MA-CUST target mep-id 2"
SLM_CMD = "run ethernet-oam cfm on-demand synthetic-loss-measurement two-way maintenance-domain MD-CUST maintenance-association MA-CUST target mep-id 2"
SHOW_OD = "show services performance-monitoring cfm tests on-demand | no-more"
STOP_ALL = "request ethernet-oam cfm on-demand stop all"
STOP_DM_MD = "request ethernet-oam cfm on-demand stop maintenance-domain MD-CUST maintenance-association MA-CUST test-type two-way-delay-measurement"
STOP_SLM_MD = "request ethernet-oam cfm on-demand stop maintenance-domain MD-CUST maintenance-association MA-CUST test-type two-way-synthetic-loss-measurement"
STOP_DM_TYPE = "request ethernet-oam cfm on-demand stop test-type two-way-delay-measurement"

results = {}
timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

print(f"=== SW-237984 Re-run (fixed SLM + timing): {timestamp} ===")
print(f"Device: ncpl-cfm-nog (xec1e3vr00008) @ {DEVICE_IP}\n")

print("Establishing SSH sessions...")
ssh1, chan1 = create_shell(DEVICE_IP, USERNAME, PASSWORD, "DM")
ssh2, chan2 = create_shell(DEVICE_IP, USERNAME, PASSWORD, "SLM")
ssh3, chan3 = create_shell(DEVICE_IP, USERNAME, PASSWORD, "Control")

# ============================================================
section("TEST A: Verify fixed SLM command works")
# ============================================================
print(f"CMD: {SLM_CMD}")
slm_test = run_cmd(chan2, SLM_CMD, wait=15)
print(slm_test)
results['slm_test'] = slm_test

show_after_slm = run_cmd(chan3, SHOW_OD, wait=5)
print(show_after_slm)
results['slm_show'] = show_after_slm

# ============================================================
section("TEST B: Start BOTH DM+SLM, stop ALL while running")
# ============================================================
print(f"[S1] Starting DM...")
run_cmd_async(chan1, DM_CMD)
time.sleep(1)
print(f"[S2] Starting SLM...")
run_cmd_async(chan2, SLM_CMD)
time.sleep(2)

print(f"\n[S3] Checking status while running...")
show_running = run_cmd(chan3, SHOW_OD, wait=3)
print(show_running)
results['both_running_show'] = show_running

print(f"[S3] Stopping ALL immediately...")
stop_out = run_cmd(chan3, STOP_ALL, wait=5)
print(stop_out)
results['stop_all_while_running'] = stop_out

time.sleep(5)
dm_out = drain_channel(chan1, wait=3)
slm_out = drain_channel(chan2, wait=3)
print(f"[S1 DM after stop]:\n{dm_out}")
print(f"[S2 SLM after stop]:\n{slm_out}")
results['both_dm_after_stop'] = dm_out
results['both_slm_after_stop'] = slm_out

show_after_stop = run_cmd(chan3, SHOW_OD, wait=5)
print(f"Sessions after stop:\n{show_after_stop}")
results['both_after_stop_show'] = show_after_stop

# ============================================================
section("TEST C: Start DM only, stop with MD/MA filter while running")
# ============================================================
time.sleep(3)
print(f"[S1] Starting DM...")
run_cmd_async(chan1, DM_CMD)
time.sleep(2)

print(f"[S3] Stopping DM with MD/MA filter...")
stop_dm = run_cmd(chan3, STOP_DM_MD, wait=5)
print(stop_dm)
results['stop_dm_md'] = stop_dm

time.sleep(5)
dm_after = drain_channel(chan1, wait=3)
print(f"[S1 DM after stop]:\n{dm_after}")
results['dm_after_md_stop'] = dm_after

# ============================================================
section("TEST D: Start SLM only, stop with MD/MA filter while running")
# ============================================================
time.sleep(3)
print(f"[S2] Starting SLM...")
run_cmd_async(chan2, SLM_CMD)
time.sleep(2)

print(f"[S3] Stopping SLM with MD/MA filter...")
stop_slm = run_cmd(chan3, STOP_SLM_MD, wait=5)
print(stop_slm)
results['stop_slm_md'] = stop_slm

time.sleep(5)
slm_after = drain_channel(chan2, wait=3)
print(f"[S2 SLM after stop]:\n{slm_after}")
results['slm_after_md_stop'] = slm_after

# ============================================================
section("TEST E: Start both, stop DM only, verify SLM continues")
# ============================================================
time.sleep(3)
print(f"[S1] Starting DM...")
run_cmd_async(chan1, DM_CMD)
time.sleep(1)
print(f"[S2] Starting SLM...")
run_cmd_async(chan2, SLM_CMD)
time.sleep(2)

print(f"[S3] Stopping DM only...")
stop_dm2 = run_cmd(chan3, STOP_DM_MD, wait=5)
print(stop_dm2)
results['stop_dm_only'] = stop_dm2

show_mid = run_cmd(chan3, SHOW_OD, wait=3)
print(f"After DM stopped (SLM should still be running/present):\n{show_mid}")
results['after_dm_stop_slm_running'] = show_mid

time.sleep(8)
dm_e = drain_channel(chan1, wait=2)
slm_e = drain_channel(chan2, wait=2)
print(f"[DM]: {dm_e}")
print(f"[SLM]: {slm_e}")
results['test_e_dm'] = dm_e
results['test_e_slm'] = slm_e

# ============================================================
section("TEST F: Stop with no active sessions (negative)")
# ============================================================
time.sleep(3)
print(f"[S3] Verifying no active sessions...")
show_empty = run_cmd(chan3, SHOW_OD, wait=5)
print(show_empty)

print(f"\n[S3] Stopping all (none active)...")
stop_empty = run_cmd(chan3, STOP_ALL, wait=5)
print(stop_empty)
results['stop_no_active'] = stop_empty

print(f"[S3] Stop DM type (none active)...")
stop_dm_empty = run_cmd(chan3, STOP_DM_TYPE, wait=5)
print(stop_dm_empty)
results['stop_dm_type_no_active'] = stop_dm_empty

# ============================================================
section("TEST G: Recovery - new DM session after all stops")
# ============================================================
time.sleep(3)
print(f"[S1] Starting fresh DM...")
dm_final = run_cmd(chan1, DM_CMD, wait=15)
print(dm_final)
results['recovery_dm'] = dm_final

show_final = run_cmd(chan3, SHOW_OD, wait=5)
print(show_final)
results['recovery_show'] = show_final

detail_final = run_cmd(chan3, "show services performance-monitoring cfm tests on-demand two-way-delay detail | no-more", wait=5)
print(detail_final)
results['recovery_detail'] = detail_final

# ============================================================
section("TEST H: Recovery - new SLM session after all stops")
# ============================================================
time.sleep(3)
print(f"[S2] Starting fresh SLM...")
slm_final = run_cmd(chan2, SLM_CMD, wait=15)
print(slm_final)
results['recovery_slm'] = slm_final

show_slm_final = run_cmd(chan3, SHOW_OD, wait=5)
print(show_slm_final)
results['recovery_slm_show'] = show_slm_final

detail_slm_final = run_cmd(chan3, "show services performance-monitoring cfm tests on-demand two-way-synthetic-loss detail | no-more", wait=5)
print(detail_slm_final)
results['recovery_slm_detail'] = detail_slm_final

print("\n=== Closing SSH sessions ===")
ssh1.close()
ssh2.close()
ssh3.close()

print(f"\n=== Re-run Complete: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} ===")

with open('/home/dn/sw237984_results_v2.json', 'w') as f:
    json.dump(results, f, indent=2)
print("Results saved to /home/dn/sw237984_results_v2.json")
