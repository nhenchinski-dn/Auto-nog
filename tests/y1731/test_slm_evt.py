#!/usr/bin/env python3
import paramiko, time, re
ANSI = re.compile(r"\[[0-9;]*[A-Za-z]")
def run_seq(client, cmds, timeout=30):
    ch = client.invoke_shell(); ch.settimeout(timeout); time.sleep(1.5)
    while ch.recv_ready(): ch.recv(65536)
    results = []
    for cmd in cmds:
        ch.send(cmd + chr(10)); out = ""; end_t = time.time() + timeout; last_data = time.time()
        while time.time() < end_t:
            if ch.recv_ready(): out += ch.recv(65536).decode(errors="ignore"); last_data = time.time()
            else:
                if time.time() - last_data > 3: break
                time.sleep(0.2)
        results.append((cmd, ANSI.sub("", out)))
    ch.close(); return results
client = paramiko.SSHClient(); client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect("WKY1C7VD00008P2", username="dnroot", password="dnroot", timeout=15, banner_timeout=15, auth_timeout=15)
log_ch = client.invoke_shell(); log_ch.settimeout(5); time.sleep(1.5)
while log_ch.recv_ready(): log_ch.recv(65536)
log_ch.send("set logging terminal" + chr(10)); time.sleep(2)
while log_ch.recv_ready(): log_ch.recv(65536)
print("Logging terminal opened.")
prof, sess = "SLM_EVT_P", "SLM_EVT_S"
cmds = ["configure", f"services performance-monitoring profiles cfm two-way-synthetic-loss-measurement {prof}",
    "pcp 5", "inform-test-results enabled", "test-duration probes probe-count 3 probe-interval 1 repeat-interval 5",
    "thresholds far-end-loss 0.01", "exit", "exit", "exit", "exit", "exit",
    f"services performance-monitoring cfm two-way-synthetic-loss-measurement {sess}", "admin-state enabled",
    f"profile {prof}", "source maintenance-domain MD-CUST1 maintenance-association MA-CUST1 mep-id 4",
    "target mep-id 3", "exit", "exit", "exit", "exit", "commit", "exit"]
outs = run_seq(client, cmds, timeout=45)
ok = True
for c, o in outs:
    if c == "commit" and ("ERROR" in o or "Commit failed" in o): print(f"Commit failed: {o[:150]}"); ok = False
if ok:
    print("SLM session created. Waiting 25s for probes + threshold check...")
    time.sleep(25)
    event_out = ""
    try:
        log_ch.settimeout(2)
        while True:
            try:
                data = log_ch.recv(65536).decode(errors="ignore")
                if not data: break
                event_out += data
            except: break
    except: pass
    event_out = ANSI.sub("", event_out)
    print(f"Captured {len(event_out)} bytes.")
    has_slm = "SYNTHETIC_LOSS" in event_out or "FAR_END" in event_out
    has_event = "CFM_PROACTIVE_TEST_FAILURE" in event_out
    if has_slm: print("OK: SLM threshold event generated!")
    elif has_event: print("CFM event found but checking if SLM:")
    else: print("BUG: No CFM_PROACTIVE_TEST_FAILURE for SLM!")
    for ln in event_out.splitlines():
        if "CFM" in ln or "PROACTIVE" in ln or "SYNTHETIC" in ln or "LOSS" in ln:
            print(f"  {ln.strip()[:200]}")
    if not has_event: print(f"  Raw: {event_out[:300]}")
    run_seq(client, ["configure", f"no services performance-monitoring cfm two-way-synthetic-loss-measurement {sess}",
        f"no services performance-monitoring profiles cfm two-way-synthetic-loss-measurement {prof}", "commit", "exit"], timeout=30)
log_ch.close(); client.close(); print("DONE")
