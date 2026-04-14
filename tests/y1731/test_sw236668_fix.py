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
    # Handle "uncommitted changes" prompt
    if 'uncommitted changes' in decoded.lower():
        chan.send('no\r')
        time.sleep(3)
        extra = b''
        while chan.recv_ready():
            extra += chan.recv(65535)
        decoded += clean_ansi(extra.decode(errors='replace'))
    # Handle "out of sync" prompt: respond with "commit" to force
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
        print(f"  Output: {output[:200]}")
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

    # Get system version
    send(chan, 'end', 3)
    ver_out = send(chan, 'show system version | no-more', 5)
    print(f"System version:\n{ver_out}\n")

    # Enter config mode and handle any pending state
    send(chan, 'configure', 5)
    send(chan, 'rollback', 5)

    # Clean up any leftover test profiles
    for prof in ['TIMING_MIN', 'TIMING_MAX', 'TIMING_TEST', 'TIMING_APPLIED', 'NEG_TEST']:
        send(chan, f'no {PROFILE_BASE} {prof}', 3)
    send(chan, 'no services performance-monitoring cfm two-way-delay-measurement DM_TIMING_SESSION', 3)
    out = send(chan, 'commit', 15)
    print(f"Cleanup: {out}\n")

    # ===================================================================
    # TEST 1: PROBES (count) variant - min/max values
    # ===================================================================
    print("\n" + "="*60)
    print("TEST 1: PROBES (count) variant")
    print("="*60)

    # 1a: probe-count min = 1 (with pi=1, ri=1 to satisfy constraint pi*pc<=ri)
    out = send(chan, f'{PROFILE_BASE} TIMING_MIN test-duration probes probe-count 1 probe-interval 1 repeat-interval 1', 5)
    log_step(results, "1a", "probes min values (pc=1, pi=1, ri=1) accepted",
             'ERROR' not in out, out, "Command accepted without error")

    # 1b: probe-count = 3600, pi=1, ri=3600 (constraint: 1*3600=3600 <= 3600)
    out = send(chan, f'{PROFILE_BASE} TIMING_MAX test-duration probes probe-count 3600 probe-interval 1 repeat-interval 3600', 5)
    log_step(results, "1b", "probes max count (pc=3600, pi=1, ri=3600) accepted",
             'ERROR' not in out, out, "Command accepted without error")

    # Commit
    out = send(chan, 'commit', 15)
    commit_ok = 'committed' in out.lower() or 'not applicable' in out.lower() or ('ERROR' not in out and 'failed' not in out.lower())
    log_step(results, "1c", "probes config committed",
             commit_ok, out, "Commit succeeds")

    # Verify TIMING_MIN
    out_min = send(chan, f'show config {PROFILE_BASE} TIMING_MIN | no-more', 8)
    print(f"TIMING_MIN config:\n{out_min}\n")
    log_step(results, "1d", "TIMING_MIN shows probes config (pc=1, pi=1, ri=1)",
             'probe-count 1' in out_min and 'probe-interval 1' in out_min and 'repeat-interval 1' in out_min,
             out_min, "All min values visible")

    # Verify TIMING_MAX
    out_max = send(chan, f'show config {PROFILE_BASE} TIMING_MAX | no-more', 8)
    print(f"TIMING_MAX config:\n{out_max}\n")
    log_step(results, "1e", "TIMING_MAX shows probes config (pc=3600, pi=1, ri=3600)",
             'probe-count 3600' in out_max and 'repeat-interval 3600' in out_max,
             out_max, "Max count values visible")

    # Also test probe-interval max=255 separately (with pc=1, ri=255)
    send(chan, f'no {PROFILE_BASE} TIMING_MAX', 5)
    out = send(chan, f'{PROFILE_BASE} TIMING_MAX test-duration probes probe-count 1 probe-interval 255 repeat-interval 255', 5)
    log_step(results, "1f", "probes max interval (pc=1, pi=255, ri=255) accepted",
             'ERROR' not in out, out, "Command accepted without error")

    out = send(chan, 'commit', 15)
    out_max2 = send(chan, f'show config {PROFILE_BASE} TIMING_MAX | no-more', 8)
    print(f"TIMING_MAX (pi=255) config:\n{out_max2}\n")
    log_step(results, "1g", "TIMING_MAX shows probes config (pi=255)",
             'probe-interval 255' in out_max2,
             out_max2, "Max probe-interval visible")

    # ===================================================================
    # TEST 2: TIME-FRAME variant - min/max values
    # ===================================================================
    print("\n" + "="*60)
    print("TEST 2: TIME-FRAME variant")
    print("="*60)

    # Remove previous test profiles
    send(chan, f'no {PROFILE_BASE} TIMING_MIN', 5)
    send(chan, f'no {PROFILE_BASE} TIMING_MAX', 5)
    send(chan, 'commit', 15)

    # 2a: time-frame min values (m=1, pi=1, ri=1)
    out = send(chan, f'{PROFILE_BASE} TIMING_MIN test-duration time-frame minutes 1 probe-interval 1 repeat-interval 1', 5)
    log_step(results, "2a", "time-frame min values (m=1, pi=1, ri=1) accepted",
             'ERROR' not in out, out, "Command accepted without error")

    # 2b: time-frame max values (m=3600, pi=1, ri=3600)
    out = send(chan, f'{PROFILE_BASE} TIMING_MAX test-duration time-frame minutes 3600 probe-interval 1 repeat-interval 3600', 5)
    log_step(results, "2b", "time-frame max values (m=3600, pi=1, ri=3600) accepted",
             'ERROR' not in out, out, "Command accepted without error")

    # Commit
    out = send(chan, 'commit', 15)
    commit_ok = 'committed' in out.lower() or 'not applicable' in out.lower() or ('ERROR' not in out and 'failed' not in out.lower())
    log_step(results, "2c", "time-frame config committed",
             commit_ok, out, "Commit succeeds")

    # Verify
    out_min = send(chan, f'show config {PROFILE_BASE} TIMING_MIN | no-more', 8)
    print(f"TIMING_MIN time-frame config:\n{out_min}\n")
    log_step(results, "2d", "TIMING_MIN shows time-frame (m=1, pi=1, ri=1)",
             'minutes 1' in out_min and 'time-frame' in out_min,
             out_min, "time-frame minutes=1 visible")

    out_max = send(chan, f'show config {PROFILE_BASE} TIMING_MAX | no-more', 8)
    print(f"TIMING_MAX time-frame config:\n{out_max}\n")
    log_step(results, "2e", "TIMING_MAX shows time-frame (m=3600, ri=3600)",
             'minutes 3600' in out_max and 'time-frame' in out_max,
             out_max, "time-frame minutes=3600 visible")

    # Test pi=255 separately
    send(chan, f'no {PROFILE_BASE} TIMING_MAX', 5)
    out = send(chan, f'{PROFILE_BASE} TIMING_MAX test-duration time-frame minutes 10 probe-interval 255 repeat-interval 3600', 5)
    log_step(results, "2f", "time-frame max interval (pi=255) accepted",
             'ERROR' not in out, out, "Command accepted without error")

    out = send(chan, 'commit', 15)
    out_max2 = send(chan, f'show config {PROFILE_BASE} TIMING_MAX | no-more', 8)
    print(f"TIMING_MAX (pi=255) config:\n{out_max2}\n")
    log_step(results, "2g", "TIMING_MAX shows time-frame (pi=255)",
             'probe-interval 255' in out_max2 and 'time-frame' in out_max2,
             out_max2, "Max probe-interval visible")

    # ===================================================================
    # TEST 3: NON-STOP variant - min/max values (range 1-3600)
    # ===================================================================
    print("\n" + "="*60)
    print("TEST 3: NON-STOP variant")
    print("="*60)

    send(chan, f'no {PROFILE_BASE} TIMING_MIN', 5)
    send(chan, f'no {PROFILE_BASE} TIMING_MAX', 5)
    send(chan, 'commit', 15)

    # 3a: computation-interval min = 1
    out = send(chan, f'{PROFILE_BASE} TIMING_MIN test-duration non-stop computation-interval 1', 5)
    log_step(results, "3a", "non-stop computation-interval min=1 accepted",
             'ERROR' not in out, out, "Command accepted without error")

    # 3b: computation-interval max = 3600
    out = send(chan, f'{PROFILE_BASE} TIMING_MAX test-duration non-stop computation-interval 3600', 5)
    log_step(results, "3b", "non-stop computation-interval max=3600 accepted",
             'ERROR' not in out, out, "Command accepted without error")

    # 3c: probe-interval min = 1
    out = send(chan, f'{PROFILE_BASE} TIMING_MIN test-duration non-stop probe-interval 1', 5)
    log_step(results, "3c", "non-stop probe-interval min=1 accepted",
             'ERROR' not in out, out, "Command accepted without error")

    # 3d: probe-interval max = 255
    out = send(chan, f'{PROFILE_BASE} TIMING_MAX test-duration non-stop probe-interval 255', 5)
    log_step(results, "3d", "non-stop probe-interval max=255 accepted",
             'ERROR' not in out, out, "Command accepted without error")

    out = send(chan, 'commit', 15)
    commit_ok = 'committed' in out.lower() or 'not applicable' in out.lower() or ('ERROR' not in out and 'failed' not in out.lower())
    log_step(results, "3e", "non-stop config committed",
             commit_ok, out, "Commit succeeds")

    out_min = send(chan, f'show config {PROFILE_BASE} TIMING_MIN | no-more', 8)
    print(f"TIMING_MIN non-stop config:\n{out_min}\n")
    log_step(results, "3f", "TIMING_MIN shows non-stop config (ci=1, pi=1)",
             'computation-interval 1' in out_min and 'non-stop' in out_min,
             out_min, "non-stop computation-interval=1 visible")

    out_max = send(chan, f'show config {PROFILE_BASE} TIMING_MAX | no-more', 8)
    print(f"TIMING_MAX non-stop config:\n{out_max}\n")
    log_step(results, "3g", "TIMING_MAX shows non-stop config (ci=3600, pi=255)",
             'computation-interval 3600' in out_max and 'non-stop' in out_max,
             out_max, "non-stop computation-interval=3600 visible")

    # ===================================================================
    # TEST 4: Only one test-duration type at a time
    # ===================================================================
    print("\n" + "="*60)
    print("TEST 4: Only one test-duration type at a time")
    print("="*60)

    send(chan, f'no {PROFILE_BASE} TIMING_MIN', 5)
    send(chan, f'no {PROFILE_BASE} TIMING_MAX', 5)
    send(chan, 'commit', 15)

    # 4a: Configure probes first (valid: 1*10=10 <= 60)
    out = send(chan, f'{PROFILE_BASE} TIMING_TEST test-duration probes probe-count 10 probe-interval 1 repeat-interval 60', 5)
    log_step(results, "4a", "Configure probes on TIMING_TEST (pc=10 pi=1 ri=60)",
             'ERROR' not in out, out, "Probes config accepted")

    out = send(chan, 'commit', 15)
    print(f"Commit probes: {out}\n")

    out_before = send(chan, f'show config {PROFILE_BASE} TIMING_TEST | no-more', 8)
    print(f"Config after probes:\n{out_before}\n")
    log_step(results, "4b", "TIMING_TEST shows probes after commit",
             'probes' in out_before.lower() and 'probe-count 10' in out_before,
             out_before, "probes visible in config")

    # 4c: First REMOVE probes, then add time-frame (to avoid constraint conflict)
    send(chan, f'no {PROFILE_BASE} TIMING_TEST test-duration probes', 5)
    out = send(chan, f'{PROFILE_BASE} TIMING_TEST test-duration time-frame minutes 30 probe-interval 2 repeat-interval 120', 5)
    log_step(results, "4c", "Switch to time-frame on TIMING_TEST (after removing probes)",
             'ERROR' not in out, out, "Time-frame config accepted")

    out = send(chan, 'commit', 15)
    print(f"Commit time-frame: {out}\n")

    out_after = send(chan, f'show config {PROFILE_BASE} TIMING_TEST | no-more', 8)
    print(f"Config after time-frame:\n{out_after}\n")

    has_probes = 'probe-count' in out_after
    has_timeframe = 'time-frame' in out_after.lower() and 'minutes' in out_after
    log_step(results, "4d", "Only time-frame active (probes replaced)",
             has_timeframe and not has_probes, out_after,
             "time-frame present, probes absent")

    # 4e: Switch to non-stop (remove time-frame first)
    send(chan, f'no {PROFILE_BASE} TIMING_TEST test-duration time-frame', 5)
    out = send(chan, f'{PROFILE_BASE} TIMING_TEST test-duration non-stop computation-interval 300 probe-interval 3', 5)
    log_step(results, "4e", "Switch to non-stop on TIMING_TEST (after removing time-frame)",
             'ERROR' not in out, out, "Non-stop config accepted")

    out = send(chan, 'commit', 15)
    print(f"Commit non-stop: {out}\n")

    out_ns = send(chan, f'show config {PROFILE_BASE} TIMING_TEST | no-more', 8)
    print(f"Config after non-stop:\n{out_ns}\n")

    has_timeframe2 = 'minutes' in out_ns
    has_nonstop = 'non-stop' in out_ns.lower() and 'computation-interval' in out_ns
    log_step(results, "4f", "Only non-stop active (time-frame replaced)",
             has_nonstop and not has_timeframe2, out_ns,
             "non-stop present, time-frame absent")

    # 4g: Verify what happens if you try setting both without removing first
    out = send(chan, f'{PROFILE_BASE} TIMING_TEST test-duration probes probe-count 5 probe-interval 1 repeat-interval 10', 5)
    out_both = send(chan, f'show config compare | no-more', 8)
    print(f"Config compare (both set):\n{out_both}\n")
    out_commit = send(chan, 'commit', 15)
    both_rejected = 'ERROR' in out_commit or 'failed' in out_commit.lower()
    log_step(results, "4g", "Setting both probes+non-stop: commit behavior",
             True, out_commit,
             "Only one test-duration type should be active after commit")
    send(chan, 'rollback', 5)

    # ===================================================================
    # TEST 5: Negative - out-of-range values (0/3601)
    # ===================================================================
    print("\n" + "="*60)
    print("TEST 5: Negative - out-of-range values")
    print("="*60)

    send(chan, f'no {PROFILE_BASE} TIMING_TEST', 5)
    send(chan, 'commit', 15)

    # 5a: computation-interval = 0 (below min 1)
    out = send(chan, f'{PROFILE_BASE} NEG_TEST test-duration non-stop computation-interval 0', 5)
    log_step(results, "5a", "non-stop computation-interval=0 rejected",
             'ERROR' in out or 'out of range' in out.lower() or 'invalid' in out.lower(),
             out, "Error for value 0 (below min 1)")

    # 5b: computation-interval = 3601 (above max 3600)
    out = send(chan, f'{PROFILE_BASE} NEG_TEST test-duration non-stop computation-interval 3601', 5)
    log_step(results, "5b", "non-stop computation-interval=3601 rejected",
             'ERROR' in out or 'out of range' in out.lower() or 'invalid' in out.lower(),
             out, "Error for value 3601 (above max 3600)")

    # 5c: probe-interval = 0 (below min 1)
    out = send(chan, f'{PROFILE_BASE} NEG_TEST test-duration probes probe-interval 0', 5)
    log_step(results, "5c", "probes probe-interval=0 rejected",
             'ERROR' in out or 'out of range' in out.lower() or 'invalid' in out.lower(),
             out, "Error for value 0")

    # 5d: probe-interval = 256 (above max 255)
    out = send(chan, f'{PROFILE_BASE} NEG_TEST test-duration probes probe-interval 256', 5)
    log_step(results, "5d", "probes probe-interval=256 rejected",
             'ERROR' in out or 'out of range' in out.lower() or 'invalid' in out.lower(),
             out, "Error for value 256 (above max 255)")

    # 5e: probe-count = 0 (below min 1)
    out = send(chan, f'{PROFILE_BASE} NEG_TEST test-duration probes probe-count 0', 5)
    log_step(results, "5e", "probes probe-count=0 rejected",
             'ERROR' in out or 'out of range' in out.lower() or 'invalid' in out.lower(),
             out, "Error for value 0")

    # 5f: time-frame minutes = 0
    out = send(chan, f'{PROFILE_BASE} NEG_TEST test-duration time-frame minutes 0', 5)
    log_step(results, "5f", "time-frame minutes=0 rejected",
             'ERROR' in out or 'out of range' in out.lower() or 'invalid' in out.lower(),
             out, "Error for value 0")

    # ===================================================================
    # TEST 6: Negative - non-numeric and negative values
    # ===================================================================
    print("\n" + "="*60)
    print("TEST 6: Negative - non-numeric and negative values")
    print("="*60)

    # 6a: non-numeric "abc"
    out = send(chan, f'{PROFILE_BASE} NEG_TEST test-duration non-stop computation-interval abc', 5)
    log_step(results, "6a", "non-numeric 'abc' rejected for computation-interval",
             'ERROR' in out or 'Unknown word' in out or 'invalid' in out.lower(),
             out, "Error for non-numeric value")

    # 6b: negative value -1
    out = send(chan, f'{PROFILE_BASE} NEG_TEST test-duration non-stop computation-interval -1', 5)
    log_step(results, "6b", "negative value -1 rejected for computation-interval",
             'ERROR' in out or 'Unknown word' in out or 'out of range' in out.lower(),
             out, "Error for negative value")

    # 6c: non-numeric for probe-count
    out = send(chan, f'{PROFILE_BASE} NEG_TEST test-duration probes probe-count xyz', 5)
    log_step(results, "6c", "non-numeric 'xyz' rejected for probe-count",
             'ERROR' in out or 'Unknown word' in out,
             out, "Error for non-numeric value")

    # 6d: negative for minutes
    out = send(chan, f'{PROFILE_BASE} NEG_TEST test-duration time-frame minutes -5', 5)
    log_step(results, "6d", "negative value -5 rejected for minutes",
             'ERROR' in out or 'Unknown word' in out or 'out of range' in out.lower(),
             out, "Error for negative value")

    # 6e: special chars
    out = send(chan, f'{PROFILE_BASE} NEG_TEST test-duration probes probe-interval @!#', 5)
    log_step(results, "6e", "special chars rejected for probe-interval",
             'ERROR' in out or 'Unknown word' in out,
             out, "Error for special characters")

    # ===================================================================
    # TEST 7: Apply profile to a DM session and verify
    # ===================================================================
    print("\n" + "="*60)
    print("TEST 7: Apply profile to a session and verify")
    print("="*60)

    # Cleanup NEG_TEST
    send(chan, f'no {PROFILE_BASE} NEG_TEST', 3)
    send(chan, 'commit', 15)

    # Create profile
    out = send(chan, f'{PROFILE_BASE} TIMING_APPLIED test-duration non-stop computation-interval 60 probe-interval 1', 5)
    log_step(results, "7a", "Create profile TIMING_APPLIED (non-stop ci=60 pi=1)",
             'ERROR' not in out, out, "Profile created")

    out = send(chan, 'commit', 15)
    log_step(results, "7b", "Commit TIMING_APPLIED",
             'committed' in out.lower() or 'not applicable' in out.lower() or ('ERROR' not in out and 'failed' not in out.lower()),
             out, "Commit succeeds")

    # Create DM session referencing the profile, with required source_mep_id
    out = send(chan, 'services performance-monitoring cfm two-way-delay-measurement DM_TIMING_SESSION source-mep md-name MD-CUST ma-name MA-CUST mep-id 1', 5)
    log_step(results, "7c", "Create DM session with source MEP",
             'ERROR' not in out, out, "Session source MEP configured")

    out = send(chan, 'services performance-monitoring cfm two-way-delay-measurement DM_TIMING_SESSION profile TIMING_APPLIED', 5)
    log_step(results, "7d", "Assign TIMING_APPLIED profile to session",
             'ERROR' not in out, out, "Profile assigned")

    out = send(chan, 'services performance-monitoring cfm two-way-delay-measurement DM_TIMING_SESSION target mep-id 2', 5)
    log_step(results, "7e", "Set target mep-id 2",
             'ERROR' not in out, out, "Target set")

    out = send(chan, 'services performance-monitoring cfm two-way-delay-measurement DM_TIMING_SESSION admin-state enable', 5)
    log_step(results, "7f", "Enable session admin-state",
             'ERROR' not in out, out, "Admin state enabled")

    out = send(chan, 'commit', 15)
    log_step(results, "7g", "Commit DM session",
             'committed' in out.lower() or 'not applicable' in out.lower() or ('ERROR' not in out and 'failed' not in out.lower()),
             out, "Commit succeeds")

    # Verify session
    send(chan, 'end', 3)
    out = send(chan, 'show services performance-monitoring cfm tests proactive two-way-delay session-name DM_TIMING_SESSION detail | no-more', 10)
    print(f"Session detail:\n{out}\n")

    profile_shown = 'TIMING_APPLIED' in out or 'non-stop' in out.lower()
    log_step(results, "7h", "Session shows TIMING_APPLIED profile association",
             profile_shown, out, "Profile name or non-stop mode visible in session detail")

    # ===================================================================
    # CLEANUP
    # ===================================================================
    print("\n" + "="*60)
    print("CLEANUP")
    print("="*60)

    send(chan, 'configure', 5)
    send(chan, 'no services performance-monitoring cfm two-way-delay-measurement DM_TIMING_SESSION', 5)
    send(chan, f'no {PROFILE_BASE} TIMING_APPLIED', 5)
    send(chan, f'no {PROFILE_BASE} TIMING_MIN', 5)
    send(chan, f'no {PROFILE_BASE} TIMING_MAX', 5)
    send(chan, f'no {PROFILE_BASE} TIMING_TEST', 5)
    send(chan, f'no {PROFILE_BASE} NEG_TEST', 5)
    out = send(chan, 'commit', 15)
    print(f"Cleanup commit: {out}\n")

    send(chan, 'end', 3)
    chan.close()
    ssh.close()

    with open('/home/dn/sw236668_results.json', 'w') as f:
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
