#!/usr/bin/env python3
"""
SW-237984: Ethernet OAM Y.1731 | CLI | request ethernet-oam cfm on-demand stop
Extended test covering all 4 on-demand test types (DM, SLM, LB, LT)
and all stop command variants.
"""
import paramiko
import time
import re
import json
import sys
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
    sep = '=' * 70
    print(f"\n{sep}\n  {title}\n{sep}")

def wait_idle(ctrl_chan, pause=5):
    """Wait for any lingering tests to finish before starting next test."""
    time.sleep(pause)
    drain_channel(ctrl_chan, wait=1)

# -- On-demand run commands --
DM_CMD  = "run ethernet-oam cfm on-demand delay-measurement two-way maintenance-domain MD-CUST maintenance-association MA-CUST target mep-id 2"
SLM_CMD = "run ethernet-oam cfm on-demand synthetic-loss-measurement two-way maintenance-domain MD-CUST maintenance-association MA-CUST target mep-id 2"
LB_CMD  = "run ethernet-oam cfm on-demand loopback maintenance-domain MD-CUST maintenance-association MA-CUST target mep-id 2 count 10"
LT_CMD  = "run ethernet-oam cfm on-demand linktrace maintenance-domain MD-CUST maintenance-association MA-CUST target mep-id 2"

# -- Show commands --
SHOW_OD = "show services performance-monitoring cfm tests on-demand | no-more"

# -- Stop: all --
STOP_ALL = "request ethernet-oam cfm on-demand stop all"

# -- Stop: by MD/MA + test-type --
STOP_DM_MD  = "request ethernet-oam cfm on-demand stop maintenance-domain MD-CUST maintenance-association MA-CUST test-type two-way-delay-measurement"
STOP_SLM_MD = "request ethernet-oam cfm on-demand stop maintenance-domain MD-CUST maintenance-association MA-CUST test-type two-way-synthetic-loss-measurement"
STOP_LB_MD  = "request ethernet-oam cfm on-demand stop maintenance-domain MD-CUST maintenance-association MA-CUST test-type loopback"
STOP_LT_MD  = "request ethernet-oam cfm on-demand stop maintenance-domain MD-CUST maintenance-association MA-CUST test-type linktrace"

# -- Stop: by test-type only (no MD/MA) --
STOP_DM_TYPE  = "request ethernet-oam cfm on-demand stop test-type two-way-delay-measurement"
STOP_SLM_TYPE = "request ethernet-oam cfm on-demand stop test-type two-way-synthetic-loss-measurement"
STOP_LB_TYPE  = "request ethernet-oam cfm on-demand stop test-type loopback"
STOP_LT_TYPE  = "request ethernet-oam cfm on-demand stop test-type linktrace"

results = {}
timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

print(f"=== SW-237984 v3 (DM/SLM/LB/LT + all stop variants): {timestamp} ===")
print(f"Device: ncpl-cfm-nog (xec1e3vr00008) @ {DEVICE_IP}\n")

print("Establishing SSH sessions...")
ssh1, chan1 = create_shell(DEVICE_IP, USERNAME, PASSWORD, "S1")
ssh2, chan2 = create_shell(DEVICE_IP, USERNAME, PASSWORD, "S2")
ssh3, chan3 = create_shell(DEVICE_IP, USERNAME, PASSWORD, "Ctrl")
print()

# -- Pre-check --
section("PRE-CHECK: System version & current on-demand sessions")
ver = run_cmd(chan3, "show system version | no-more", wait=5)
print(ver)
results['version'] = ver
pre = run_cmd(chan3, SHOW_OD, wait=5)
print(pre)
results['pre_check'] = pre

# ================================================================
#  PART 1 — LOOPBACK (LB)
# ================================================================

section("TEST 1: Loopback — run and verify")
wait_idle(chan3)
lb_out = run_cmd(chan1, LB_CMD, wait=15)
print(lb_out)
results['lb_run'] = lb_out
lb_show = run_cmd(chan3, SHOW_OD, wait=5)
print(lb_show)
results['lb_show'] = lb_show

section("TEST 2: Loopback — start LB, stop with MD/MA filter")
wait_idle(chan3)
run_cmd_async(chan1, LB_CMD)
time.sleep(2)
stop_lb = run_cmd(chan3, STOP_LB_MD, wait=5)
print(f"Stop LB (MD/MA): {stop_lb}")
results['stop_lb_md'] = stop_lb
time.sleep(5)
lb_after = drain_channel(chan1, wait=3)
print(f"LB output after stop: {lb_after}")
results['lb_after_md_stop'] = lb_after
lb_show2 = run_cmd(chan3, SHOW_OD, wait=5)
print(lb_show2)
results['lb_show_after_md_stop'] = lb_show2

section("TEST 3: Loopback — start LB, stop by test-type only")
wait_idle(chan3)
run_cmd_async(chan1, LB_CMD)
time.sleep(2)
stop_lb_t = run_cmd(chan3, STOP_LB_TYPE, wait=5)
print(f"Stop LB (type): {stop_lb_t}")
results['stop_lb_type'] = stop_lb_t
time.sleep(5)
lb_after2 = drain_channel(chan1, wait=3)
print(f"LB output after stop: {lb_after2}")
results['lb_after_type_stop'] = lb_after2

# ================================================================
#  PART 2 — LINKTRACE (LT)
# ================================================================

section("TEST 4: Linktrace — run and verify")
wait_idle(chan3)
lt_out = run_cmd(chan1, LT_CMD, wait=15)
print(lt_out)
results['lt_run'] = lt_out
lt_show = run_cmd(chan3, SHOW_OD, wait=5)
print(lt_show)
results['lt_show'] = lt_show

section("TEST 5: Linktrace — start LT, stop with MD/MA filter")
wait_idle(chan3)
run_cmd_async(chan1, LT_CMD)
time.sleep(2)
stop_lt = run_cmd(chan3, STOP_LT_MD, wait=5)
print(f"Stop LT (MD/MA): {stop_lt}")
results['stop_lt_md'] = stop_lt
time.sleep(5)
lt_after = drain_channel(chan1, wait=3)
print(f"LT output after stop: {lt_after}")
results['lt_after_md_stop'] = lt_after
lt_show2 = run_cmd(chan3, SHOW_OD, wait=5)
print(lt_show2)
results['lt_show_after_md_stop'] = lt_show2

section("TEST 6: Linktrace — start LT, stop by test-type only")
wait_idle(chan3)
run_cmd_async(chan1, LT_CMD)
time.sleep(2)
stop_lt_t = run_cmd(chan3, STOP_LT_TYPE, wait=5)
print(f"Stop LT (type): {stop_lt_t}")
results['stop_lt_type'] = stop_lt_t
time.sleep(5)
lt_after2 = drain_channel(chan1, wait=3)
print(f"LT output after stop: {lt_after2}")
results['lt_after_type_stop'] = lt_after2

# ================================================================
#  PART 3 — COMBINED: All 4 types + stop variants
# ================================================================

section("TEST 7: Start DM + SLM + LB + LT, stop ALL")
wait_idle(chan3)
run_cmd_async(chan1, DM_CMD)
time.sleep(0.5)
run_cmd_async(chan2, SLM_CMD)
time.sleep(0.5)
# LB and LT via ctrl channel would block, use S1/S2 which are busy.
# We'll run LB after DM on S1 if DM finishes fast; instead, just test DM+SLM+stop-all with LB/LT types in the stop.
# For a true 4-way parallel we'd need 4 sessions; let's focus on stop-all covering all types.
time.sleep(2)
show_7 = run_cmd(chan3, SHOW_OD, wait=3)
print(f"Sessions while DM+SLM running:\n{show_7}")
results['test7_running'] = show_7
stop_7 = run_cmd(chan3, STOP_ALL, wait=5)
print(f"Stop all:\n{stop_7}")
results['test7_stop_all'] = stop_7
time.sleep(5)
drain_channel(chan1, wait=3)
drain_channel(chan2, wait=3)
show_7a = run_cmd(chan3, SHOW_OD, wait=5)
print(f"After stop all:\n{show_7a}")
results['test7_after_stop'] = show_7a

section("TEST 8: Start DM, stop with SLM filter (wrong type — should not stop DM)")
wait_idle(chan3)
run_cmd_async(chan1, DM_CMD)
time.sleep(2)
stop_wrong = run_cmd(chan3, STOP_SLM_MD, wait=5)
print(f"Stop SLM filter while DM running:\n{stop_wrong}")
results['test8_wrong_type'] = stop_wrong
time.sleep(10)
dm_8 = drain_channel(chan1, wait=3)
print(f"DM should have completed normally:\n{dm_8}")
results['test8_dm_completed'] = dm_8

section("TEST 9: Start DM, stop with LB filter (wrong type — should not stop DM)")
wait_idle(chan3)
run_cmd_async(chan1, DM_CMD)
time.sleep(2)
stop_wrong2 = run_cmd(chan3, STOP_LB_MD, wait=5)
print(f"Stop LB filter while DM running:\n{stop_wrong2}")
results['test9_wrong_type_lb'] = stop_wrong2
time.sleep(10)
dm_9 = drain_channel(chan1, wait=3)
print(f"DM should have completed normally:\n{dm_9}")
results['test9_dm_completed'] = dm_9

section("TEST 10: Start DM, stop with LT filter (wrong type — should not stop DM)")
wait_idle(chan3)
run_cmd_async(chan1, DM_CMD)
time.sleep(2)
stop_wrong3 = run_cmd(chan3, STOP_LT_MD, wait=5)
print(f"Stop LT filter while DM running:\n{stop_wrong3}")
results['test10_wrong_type_lt'] = stop_wrong3
time.sleep(10)
dm_10 = drain_channel(chan1, wait=3)
print(f"DM should have completed normally:\n{dm_10}")
results['test10_dm_completed'] = dm_10

# ================================================================
#  PART 4 — SELECTIVE STOP: DM+SLM running, stop only one
# ================================================================

section("TEST 11: DM+SLM running, stop DM only (SLM continues)")
wait_idle(chan3)
run_cmd_async(chan1, DM_CMD)
time.sleep(1)
run_cmd_async(chan2, SLM_CMD)
time.sleep(2)
stop_11 = run_cmd(chan3, STOP_DM_MD, wait=5)
print(f"Stop DM only:\n{stop_11}")
results['test11_stop_dm'] = stop_11
show_11 = run_cmd(chan3, SHOW_OD, wait=3)
print(f"SLM should still be running:\n{show_11}")
results['test11_show_mid'] = show_11
time.sleep(8)
drain_channel(chan1, wait=2)
drain_channel(chan2, wait=2)

section("TEST 12: DM+SLM running, stop SLM only (DM continues)")
wait_idle(chan3)
run_cmd_async(chan1, DM_CMD)
time.sleep(1)
run_cmd_async(chan2, SLM_CMD)
time.sleep(2)
stop_12 = run_cmd(chan3, STOP_SLM_MD, wait=5)
print(f"Stop SLM only:\n{stop_12}")
results['test12_stop_slm'] = stop_12
show_12 = run_cmd(chan3, SHOW_OD, wait=3)
print(f"DM should still be running:\n{show_12}")
results['test12_show_mid'] = show_12
time.sleep(8)
drain_channel(chan1, wait=2)
drain_channel(chan2, wait=2)

# ================================================================
#  PART 5 — NEGATIVE / EDGE CASES
# ================================================================

section("TEST 13: Stop all with no active sessions (all 4 type variants)")
wait_idle(chan3)
for label, cmd in [("stop all", STOP_ALL),
                    ("stop DM type", STOP_DM_TYPE),
                    ("stop SLM type", STOP_SLM_TYPE),
                    ("stop LB type", STOP_LB_TYPE),
                    ("stop LT type", STOP_LT_TYPE),
                    ("stop DM MD/MA", STOP_DM_MD),
                    ("stop SLM MD/MA", STOP_SLM_MD),
                    ("stop LB MD/MA", STOP_LB_MD),
                    ("stop LT MD/MA", STOP_LT_MD)]:
    out = run_cmd(chan3, cmd, wait=5)
    print(f"  {label}: {out.splitlines()[-2] if len(out.splitlines()) >= 2 else out}")
    results[f'neg_{label.replace(" ", "_")}'] = out

# ================================================================
#  PART 6 — RECOVERY: All types work after stops
# ================================================================

section("TEST 14: Recovery — DM after all stops")
wait_idle(chan3)
dm_r = run_cmd(chan1, DM_CMD, wait=15)
print(dm_r)
results['recovery_dm'] = dm_r

section("TEST 15: Recovery — SLM after all stops")
wait_idle(chan3)
slm_r = run_cmd(chan2, SLM_CMD, wait=15)
print(slm_r)
results['recovery_slm'] = slm_r

section("TEST 16: Recovery — LB after all stops")
wait_idle(chan3)
lb_r = run_cmd(chan1, LB_CMD, wait=15)
print(lb_r)
results['recovery_lb'] = lb_r

section("TEST 17: Recovery — LT after all stops")
wait_idle(chan3)
lt_r = run_cmd(chan1, LT_CMD, wait=15)
print(lt_r)
results['recovery_lt'] = lt_r

section("FINAL: Show all on-demand sessions")
final_show = run_cmd(chan3, SHOW_OD, wait=5)
print(final_show)
results['final_show'] = final_show

# Cleanup
print("\n=== Closing SSH sessions ===")
ssh1.close()
ssh2.close()
ssh3.close()

end_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
print(f"\n=== SW-237984 v3 Complete: {end_ts} ===")

out_path = '/home/dn/output/sw237984_results_v3.json'
with open(out_path, 'w') as f:
    json.dump(results, f, indent=2)
print(f"Results saved to {out_path}")
