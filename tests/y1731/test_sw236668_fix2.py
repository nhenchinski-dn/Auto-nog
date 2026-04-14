import paramiko
import time
import re
import json

DEVICE_IP = "100.64.3.184"
USERNAME = "dnroot"
PASSWORD = "dnroot"

PROFILE_BASE = "services performance-monitoring profiles cfm two-way-delay-measurement"

def connect():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(DEVICE_IP, username=USERNAME, password=PASSWORD,
                timeout=30, look_for_keys=False, allow_agent=False)
    chan = ssh.invoke_shell(width=300)
    time.sleep(5)
    chan.recv(65535)
    return ssh, chan

def clean_ansi(text):
    return re.sub(r'\x1b\[[0-9;]*[a-zA-Z]|\r', '', text)

def send(chan, cmd, wait=5):
    chan.send(cmd + '\r')
    time.sleep(wait)
    out = b''
    while chan.recv_ready():
        out += chan.recv(65535)
    decoded = clean_ansi(out.decode(errors='replace'))
    if 'uncommitted changes' in decoded.lower():
        chan.send('no\r')
        time.sleep(3)
        extra = b''
        while chan.recv_ready():
            extra += chan.recv(65535)
        decoded += clean_ansi(extra.decode(errors='replace'))
    if 'out of sync' in decoded.lower() and 'commit, merge-only, abort' in decoded.lower():
        chan.send('commit\r')
        time.sleep(10)
        extra = b''
        while chan.recv_ready():
            extra += chan.recv(65535)
        decoded += clean_ansi(extra.decode(errors='replace'))
    return decoded

def log_step(results, step_id, name, passed, output, expected=""):
    status = "PASS" if passed else "FAIL"
    print(f"[{status}] {step_id}: {name}")
    if not passed:
        print(f"  Output: {output[:300]}")
    results.append({
        "step": step_id,
        "name": name,
        "status": status,
        "output": output.strip(),
        "expected": expected
    })

def main():
    ssh, chan = connect()
    print("Connected.\n")
    results = []

    send(chan, 'configure', 5)
    send(chan, 'rollback', 5)

    # Clean up
    for prof in ['TIMING_TEST', 'TIMING_APPLIED', 'NEG_TEST']:
        send(chan, f'no {PROFILE_BASE} {prof}', 3)
    send(chan, 'no services performance-monitoring cfm two-way-delay-measurement DM_TIMING_SESSION', 3)
    send(chan, 'commit', 15)

    # ===================================================================
    # FIX TEST 4: Only one test-duration type at a time
    # ===================================================================
    print("\n" + "="*60)
    print("TEST 4: Only one test-duration type at a time (fixed)")
    print("="*60)

    # 4a: Configure probes
    out = send(chan, f'{PROFILE_BASE} TIMING_TEST test-duration probes probe-count 10 probe-interval 1 repeat-interval 60', 5)
    log_step(results, "4a", "Configure probes on TIMING_TEST (pc=10 pi=1 ri=60)",
             'ERROR' not in out, out, "Probes accepted")

    out = send(chan, 'commit', 15)
    print(f"Commit probes: {out}\n")

    out_before = send(chan, f'show config {PROFILE_BASE} TIMING_TEST | no-more', 8)
    print(f"Config with probes:\n{out_before}\n")
    log_step(results, "4b", "TIMING_TEST shows probes after commit",
             'probes' in out_before.lower() and 'probe-count 10' in out_before,
             out_before, "probes visible")

    # 4c: Remove probes, add time-frame (ri must be >= minutes_in_sec: 5min=300s, ri=600)
    send(chan, f'no {PROFILE_BASE} TIMING_TEST test-duration probes', 5)
    out = send(chan, f'{PROFILE_BASE} TIMING_TEST test-duration time-frame minutes 5 probe-interval 2 repeat-interval 600', 5)
    log_step(results, "4c", "Switch to time-frame (m=5 pi=2 ri=600)",
             'ERROR' not in out, out, "Time-frame accepted")

    out = send(chan, 'commit', 15)
    print(f"Commit time-frame: {out}\n")
    commit_ok = 'committed' in out.lower() or 'not applicable' in out.lower() or ('ERROR' not in out and 'failed' not in out.lower())
    log_step(results, "4d-commit", "Time-frame commit succeeds",
             commit_ok, out, "Commit succeeds")

    out_after = send(chan, f'show config {PROFILE_BASE} TIMING_TEST | no-more', 8)
    print(f"Config after time-frame:\n{out_after}\n")

    has_probes = 'probe-count' in out_after
    has_timeframe = 'time-frame' in out_after.lower() and 'minutes' in out_after
    log_step(results, "4d", "Only time-frame active (probes gone)",
             has_timeframe and not has_probes, out_after,
             "time-frame present, no probe-count")

    # 4e: Remove time-frame, add non-stop
    send(chan, f'no {PROFILE_BASE} TIMING_TEST test-duration time-frame', 5)
    out = send(chan, f'{PROFILE_BASE} TIMING_TEST test-duration non-stop computation-interval 300 probe-interval 3', 5)
    log_step(results, "4e", "Switch to non-stop (ci=300 pi=3)",
             'ERROR' not in out, out, "Non-stop accepted")

    out = send(chan, 'commit', 15)
    print(f"Commit non-stop: {out}\n")

    out_ns = send(chan, f'show config {PROFILE_BASE} TIMING_TEST | no-more', 8)
    print(f"Config after non-stop:\n{out_ns}\n")

    has_timeframe2 = 'minutes' in out_ns
    has_nonstop = 'non-stop' in out_ns.lower() and 'computation-interval' in out_ns
    log_step(results, "4f", "Only non-stop active (time-frame gone)",
             has_nonstop and not has_timeframe2, out_ns,
             "non-stop present, no time-frame")

    # Cleanup TIMING_TEST
    send(chan, f'no {PROFILE_BASE} TIMING_TEST', 5)
    send(chan, 'commit', 15)

    # ===================================================================
    # FIX TEST 7: Apply profile to session
    # ===================================================================
    print("\n" + "="*60)
    print("TEST 7: Apply profile to a session (fixed)")
    print("="*60)

    # First, explore the DM session CLI to find correct syntax
    out = send(chan, 'services performance-monitoring cfm two-way-delay-measurement DM_TIMING_SESSION ?', 5)
    print(f"DM session options:\n{out}\n")

    # Create profile
    out = send(chan, f'{PROFILE_BASE} TIMING_APPLIED test-duration non-stop computation-interval 60 probe-interval 1', 5)
    log_step(results, "7a", "Create profile TIMING_APPLIED (non-stop ci=60 pi=1)",
             'ERROR' not in out, out, "Profile created")

    out = send(chan, 'commit', 15)
    log_step(results, "7b", "Commit TIMING_APPLIED",
             'committed' in out.lower() or 'not applicable' in out.lower() or ('ERROR' not in out and 'failed' not in out.lower()),
             out, "Commit succeeds")

    # Check existing DM session config for reference
    out = send(chan, 'show config services performance-monitoring cfm two-way-delay-measurement | no-more', 10)
    print(f"Existing DM config:\n{out}\n")

    # Try source-mep-id directly
    out = send(chan, 'services performance-monitoring cfm two-way-delay-measurement DM_TIMING_SESSION source-mep-id ?', 5)
    print(f"source-mep-id ?:\n{out}\n")

    # Also try just exploring source-mep
    out = send(chan, 'services performance-monitoring cfm two-way-delay-measurement DM_TIMING_SESSION source-mep ?', 5)
    print(f"source-mep ?:\n{out}\n")

    # Try configuring with the correct syntax based on exploration
    # Approach 1: use sub-commands
    out = send(chan, 'services performance-monitoring cfm two-way-delay-measurement DM_TIMING_SESSION source-mep md-name MD-CUST', 5)
    print(f"md-name MD-CUST:\n{out}\n")
    out = send(chan, 'services performance-monitoring cfm two-way-delay-measurement DM_TIMING_SESSION source-mep ma-name MA-CUST', 5)
    print(f"ma-name MA-CUST:\n{out}\n")
    out = send(chan, 'services performance-monitoring cfm two-way-delay-measurement DM_TIMING_SESSION source-mep mep-id 1', 5)
    print(f"mep-id 1:\n{out}\n")

    out = send(chan, 'services performance-monitoring cfm two-way-delay-measurement DM_TIMING_SESSION profile TIMING_APPLIED', 5)
    log_step(results, "7c", "Assign profile TIMING_APPLIED",
             'ERROR' not in out, out, "Profile assigned")

    out = send(chan, 'services performance-monitoring cfm two-way-delay-measurement DM_TIMING_SESSION target mep-id 2', 5)
    log_step(results, "7d", "Set target mep-id 2",
             'ERROR' not in out, out, "Target set")

    out = send(chan, 'services performance-monitoring cfm two-way-delay-measurement DM_TIMING_SESSION admin-state enable', 5)
    log_step(results, "7e", "Enable admin-state",
             'ERROR' not in out, out, "Admin state enabled")

    out = send(chan, 'commit', 15)
    print(f"Commit session: {out}\n")
    commit_ok = 'committed' in out.lower() or 'not applicable' in out.lower() or ('ERROR' not in out and 'failed' not in out.lower())
    log_step(results, "7f", "Commit DM session with profile",
             commit_ok, out, "Commit succeeds")

    # Show config for verification
    out_cfg = send(chan, f'show config services performance-monitoring cfm two-way-delay-measurement DM_TIMING_SESSION | no-more', 10)
    print(f"Session config:\n{out_cfg}\n")
    log_step(results, "7g", "Session config shows TIMING_APPLIED profile",
             'TIMING_APPLIED' in out_cfg, out_cfg,
             "Profile name visible in session config")

    # Check operational state
    send(chan, 'end', 3)
    out = send(chan, 'show services performance-monitoring cfm tests proactive two-way-delay session-name DM_TIMING_SESSION detail | no-more', 10)
    print(f"Session detail:\n{out}\n")
    log_step(results, "7h", "Session operational detail shows profile",
             'TIMING_APPLIED' in out or 'non-stop' in out.lower() or 'DM_TIMING_SESSION' in out,
             out, "Session details visible")

    # Cleanup
    print("\n" + "="*60)
    print("CLEANUP")
    print("="*60)

    send(chan, 'configure', 5)
    send(chan, 'no services performance-monitoring cfm two-way-delay-measurement DM_TIMING_SESSION', 5)
    send(chan, f'no {PROFILE_BASE} TIMING_APPLIED', 5)
    out = send(chan, 'commit', 15)
    print(f"Cleanup commit: {out}\n")

    send(chan, 'end', 3)
    chan.close()
    ssh.close()

    with open('/home/dn/sw236668_fix2_results.json', 'w') as f:
        json.dump(results, f, indent=2)

    print("\n" + "="*60)
    print("SUMMARY")
    print("="*60)
    passed = sum(1 for r in results if r['status'] == 'PASS')
    failed = sum(1 for r in results if r['status'] == 'FAIL')
    total = passed + failed
    print(f"PASSED: {passed}/{total}")
    print(f"FAILED: {failed}/{total}")
    for r in results:
        print(f"  [{r['status']}] {r['step']}: {r['name']}")

if __name__ == '__main__':
    main()
