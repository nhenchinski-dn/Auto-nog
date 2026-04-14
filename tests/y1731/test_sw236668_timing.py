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
    return decoded

def log_step(results, step_id, name, passed, output, expected=""):
    status = "PASS" if passed else "FAIL"
    print(f"[{status}] {step_id}: {name}")
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

    # Enter config mode
    send(chan, 'configure', 5)

    # Clean up any leftover test profiles
    for prof in ['TIMING_MIN', 'TIMING_MAX', 'TIMING_TEST']:
        send(chan, f'no {PROFILE_BASE} {prof}', 3)
    out = send(chan, 'commit', 10)
    if 'not applicable' not in out:
        print(f"Cleanup commit: {out}")

    # ===================================================================
    # TEST 1: PROBES (count) variant - min/max values
    # ===================================================================
    print("\n" + "="*60)
    print("TEST 1: PROBES (count) variant")
    print("="*60)

    # 1a: probe-count min = 1
    out = send(chan, f'{PROFILE_BASE} TIMING_MIN test-duration probes probe-count 1', 5)
    log_step(results, "1a", "probes probe-count min=1 accepted",
             'ERROR' not in out, out, "Command accepted without error")

    # 1b: probe-count = 3600
    out = send(chan, f'{PROFILE_BASE} TIMING_MAX test-duration probes probe-count 3600', 5)
    log_step(results, "1b", "probes probe-count 3600 accepted",
             'ERROR' not in out, out, "Command accepted without error")

    # 1c: probe-interval min = 1
    out = send(chan, f'{PROFILE_BASE} TIMING_MIN test-duration probes probe-interval 1', 5)
    log_step(results, "1c", "probes probe-interval min=1 accepted",
             'ERROR' not in out, out, "Command accepted without error")

    # 1d: probe-interval max = 255
    out = send(chan, f'{PROFILE_BASE} TIMING_MAX test-duration probes probe-interval 255', 5)
    log_step(results, "1d", "probes probe-interval max=255 accepted",
             'ERROR' not in out, out, "Command accepted without error")

    # 1e: repeat-interval min = 1
    out = send(chan, f'{PROFILE_BASE} TIMING_MIN test-duration probes repeat-interval 1', 5)
    log_step(results, "1e", "probes repeat-interval min=1 accepted",
             'ERROR' not in out, out, "Command accepted without error")

    # 1f: repeat-interval = 3600
    out = send(chan, f'{PROFILE_BASE} TIMING_MAX test-duration probes repeat-interval 3600', 5)
    log_step(results, "1f", "probes repeat-interval 3600 accepted",
             'ERROR' not in out, out, "Command accepted without error")

    # Commit and verify
    out = send(chan, 'commit', 15)
    log_step(results, "1g", "probes config committed",
             'ERROR' not in out and ('committed' in out.lower() or 'not applicable' in out.lower()),
             out, "Commit succeeds")

    # Show config compare to verify
    out = send(chan, 'show config compare', 10)
    print(f"Config after probes commit:\n{out}\n")

    # ===================================================================
    # TEST 2: TIME-FRAME variant - min/max values
    # ===================================================================
    print("\n" + "="*60)
    print("TEST 2: TIME-FRAME variant")
    print("="*60)

    # Remove previous test profiles
    send(chan, f'no {PROFILE_BASE} TIMING_MIN', 5)
    send(chan, f'no {PROFILE_BASE} TIMING_MAX', 5)
    send(chan, 'commit', 10)

    # 2a: minutes min = 1
    out = send(chan, f'{PROFILE_BASE} TIMING_MIN test-duration time-frame minutes 1', 5)
    log_step(results, "2a", "time-frame minutes min=1 accepted",
             'ERROR' not in out, out, "Command accepted without error")

    # 2b: minutes = 3600
    out = send(chan, f'{PROFILE_BASE} TIMING_MAX test-duration time-frame minutes 3600', 5)
    log_step(results, "2b", "time-frame minutes 3600 accepted",
             'ERROR' not in out, out, "Command accepted without error")

    # 2c: probe-interval min = 1
    out = send(chan, f'{PROFILE_BASE} TIMING_MIN test-duration time-frame probe-interval 1', 5)
    log_step(results, "2c", "time-frame probe-interval min=1 accepted",
             'ERROR' not in out, out, "Command accepted without error")

    # 2d: probe-interval max = 255
    out = send(chan, f'{PROFILE_BASE} TIMING_MAX test-duration time-frame probe-interval 255', 5)
    log_step(results, "2d", "time-frame probe-interval max=255 accepted",
             'ERROR' not in out, out, "Command accepted without error")

    # 2e: repeat-interval min = 1
    out = send(chan, f'{PROFILE_BASE} TIMING_MIN test-duration time-frame repeat-interval 1', 5)
    log_step(results, "2e", "time-frame repeat-interval min=1 accepted",
             'ERROR' not in out, out, "Command accepted without error")

    # 2f: repeat-interval = 3600
    out = send(chan, f'{PROFILE_BASE} TIMING_MAX test-duration time-frame repeat-interval 3600', 5)
    log_step(results, "2f", "time-frame repeat-interval 3600 accepted",
             'ERROR' not in out, out, "Command accepted without error")

    # Commit
    out = send(chan, 'commit', 15)
    log_step(results, "2g", "time-frame config committed",
             'ERROR' not in out and ('committed' in out.lower() or 'not applicable' in out.lower()),
             out, "Commit succeeds")

    # ===================================================================
    # TEST 3: NON-STOP variant - min/max values (range 1-3600)
    # ===================================================================
    print("\n" + "="*60)
    print("TEST 3: NON-STOP variant")
    print("="*60)

    # Remove previous test profiles
    send(chan, f'no {PROFILE_BASE} TIMING_MIN', 5)
    send(chan, f'no {PROFILE_BASE} TIMING_MAX', 5)
    send(chan, 'commit', 10)

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

    # Commit
    out = send(chan, 'commit', 15)
    log_step(results, "3e", "non-stop config committed",
             'ERROR' not in out and ('committed' in out.lower() or 'not applicable' in out.lower()),
             out, "Commit succeeds")

    # ===================================================================
    # TEST 4: Only one test-duration type at a time
    # ===================================================================
    print("\n" + "="*60)
    print("TEST 4: Only one test-duration type at a time")
    print("="*60)

    # Remove previous test profiles
    send(chan, f'no {PROFILE_BASE} TIMING_MIN', 5)
    send(chan, f'no {PROFILE_BASE} TIMING_MAX', 5)
    send(chan, 'commit', 10)

    # 4a: Configure probes first
    out = send(chan, f'{PROFILE_BASE} TIMING_TEST test-duration probes probe-count 100 probe-interval 5 repeat-interval 60', 5)
    log_step(results, "4a", "Configure probes on TIMING_TEST",
             'ERROR' not in out, out, "Probes config accepted")

    out = send(chan, 'commit', 15)
    print(f"Commit probes: {out}\n")

    # Show config to confirm probes is set
    out_before = send(chan, f'show config running {PROFILE_BASE} TIMING_TEST | no-more', 8)
    if 'ERROR' in out_before:
        out_before = send(chan, f'show config {PROFILE_BASE} TIMING_TEST | no-more', 8)
    print(f"Config after probes:\n{out_before}\n")

    # 4b: Now configure time-frame on same profile
    out = send(chan, f'{PROFILE_BASE} TIMING_TEST test-duration time-frame minutes 30 probe-interval 2 repeat-interval 120', 5)
    log_step(results, "4b", "Configure time-frame on same TIMING_TEST (should replace probes)",
             'ERROR' not in out, out, "Time-frame config accepted")

    out = send(chan, 'commit', 15)
    print(f"Commit time-frame: {out}\n")

    # Show config to verify probes was replaced
    out_after = send(chan, f'show config {PROFILE_BASE} TIMING_TEST | no-more', 8)
    print(f"Config after time-frame (should NOT have probes):\n{out_after}\n")

    has_probes = 'probes' in out_after.lower() or 'probe-count' in out_after.lower()
    has_timeframe = 'time-frame' in out_after.lower() or 'minutes' in out_after.lower()
    log_step(results, "4c", "Only time-frame active (probes replaced)",
             has_timeframe and not has_probes, out_after,
             "time-frame present, probes absent after switch")

    # 4d: Now configure non-stop on same profile
    out = send(chan, f'{PROFILE_BASE} TIMING_TEST test-duration non-stop computation-interval 300 probe-interval 3', 5)
    log_step(results, "4d", "Configure non-stop on same TIMING_TEST (should replace time-frame)",
             'ERROR' not in out, out, "Non-stop config accepted")

    out = send(chan, 'commit', 15)
    print(f"Commit non-stop: {out}\n")

    out_ns = send(chan, f'show config {PROFILE_BASE} TIMING_TEST | no-more', 8)
    print(f"Config after non-stop (should NOT have time-frame):\n{out_ns}\n")

    has_timeframe2 = 'time-frame' in out_ns.lower() or 'minutes' in out_ns.lower()
    has_nonstop = 'non-stop' in out_ns.lower() or 'computation-interval' in out_ns.lower()
    log_step(results, "4e", "Only non-stop active (time-frame replaced)",
             has_nonstop and not has_timeframe2, out_ns,
             "non-stop present, time-frame absent after switch")

    # ===================================================================
    # TEST 5: Negative - out-of-range values (0/3601)
    # ===================================================================
    print("\n" + "="*60)
    print("TEST 5: Negative - out-of-range values")
    print("="*60)

    # Remove TIMING_TEST
    send(chan, f'no {PROFILE_BASE} TIMING_TEST', 5)
    send(chan, 'commit', 10)

    # 5a: computation-interval = 0 (below min 1)
    out = send(chan, f'{PROFILE_BASE} NEG_TEST test-duration non-stop computation-interval 0', 5)
    log_step(results, "5a", "non-stop computation-interval=0 rejected",
             'ERROR' in out or 'out of range' in out.lower() or 'invalid' in out.lower(),
             out, "Error/rejected for value 0 (below min 1)")

    # 5b: computation-interval = 3601 (above max 3600)
    out = send(chan, f'{PROFILE_BASE} NEG_TEST test-duration non-stop computation-interval 3601', 5)
    log_step(results, "5b", "non-stop computation-interval=3601 rejected",
             'ERROR' in out or 'out of range' in out.lower() or 'invalid' in out.lower(),
             out, "Error/rejected for value 3601 (above max 3600)")

    # 5c: probe-interval = 0 (below min 1)
    out = send(chan, f'{PROFILE_BASE} NEG_TEST test-duration probes probe-interval 0', 5)
    log_step(results, "5c", "probes probe-interval=0 rejected",
             'ERROR' in out or 'out of range' in out.lower() or 'invalid' in out.lower(),
             out, "Error/rejected for value 0 (below min 1)")

    # 5d: probe-interval = 256 (above max 255)
    out = send(chan, f'{PROFILE_BASE} NEG_TEST test-duration probes probe-interval 256', 5)
    log_step(results, "5d", "probes probe-interval=256 rejected",
             'ERROR' in out or 'out of range' in out.lower() or 'invalid' in out.lower(),
             out, "Error/rejected for value 256 (above max 255)")

    # 5e: probe-count = 0 (below min 1)
    out = send(chan, f'{PROFILE_BASE} NEG_TEST test-duration probes probe-count 0', 5)
    log_step(results, "5e", "probes probe-count=0 rejected",
             'ERROR' in out or 'out of range' in out.lower() or 'invalid' in out.lower(),
             out, "Error/rejected for value 0 (below min 1)")

    # 5f: time-frame minutes = 0 (below min 1)
    out = send(chan, f'{PROFILE_BASE} NEG_TEST test-duration time-frame minutes 0', 5)
    log_step(results, "5f", "time-frame minutes=0 rejected",
             'ERROR' in out or 'out of range' in out.lower() or 'invalid' in out.lower(),
             out, "Error/rejected for value 0 (below min 1)")

    # ===================================================================
    # TEST 6: Negative - non-numeric and negative values
    # ===================================================================
    print("\n" + "="*60)
    print("TEST 6: Negative - non-numeric and negative values")
    print("="*60)

    # 6a: non-numeric string "abc"
    out = send(chan, f'{PROFILE_BASE} NEG_TEST test-duration non-stop computation-interval abc', 5)
    log_step(results, "6a", "non-numeric 'abc' rejected for computation-interval",
             'ERROR' in out or 'Unknown word' in out or 'invalid' in out.lower(),
             out, "Error/rejected for non-numeric value")

    # 6b: negative value -1
    out = send(chan, f'{PROFILE_BASE} NEG_TEST test-duration non-stop computation-interval -1', 5)
    log_step(results, "6b", "negative value -1 rejected for computation-interval",
             'ERROR' in out or 'Unknown word' in out or 'invalid' in out.lower() or 'out of range' in out.lower(),
             out, "Error/rejected for negative value")

    # 6c: non-numeric for probe-count
    out = send(chan, f'{PROFILE_BASE} NEG_TEST test-duration probes probe-count xyz', 5)
    log_step(results, "6c", "non-numeric 'xyz' rejected for probe-count",
             'ERROR' in out or 'Unknown word' in out or 'invalid' in out.lower(),
             out, "Error/rejected for non-numeric value")

    # 6d: negative for minutes
    out = send(chan, f'{PROFILE_BASE} NEG_TEST test-duration time-frame minutes -5', 5)
    log_step(results, "6d", "negative value -5 rejected for minutes",
             'ERROR' in out or 'Unknown word' in out or 'invalid' in out.lower() or 'out of range' in out.lower(),
             out, "Error/rejected for negative value")

    # 6e: special chars
    out = send(chan, f'{PROFILE_BASE} NEG_TEST test-duration probes probe-interval @!#', 5)
    log_step(results, "6e", "special chars '@!#' rejected for probe-interval",
             'ERROR' in out or 'Unknown word' in out or 'invalid' in out.lower(),
             out, "Error/rejected for special characters")

    # ===================================================================
    # TEST 7: Apply profile to a session and verify
    # ===================================================================
    print("\n" + "="*60)
    print("TEST 7: Apply profile to a session and verify")
    print("="*60)

    # Cleanup NEG_TEST if it got created
    send(chan, f'no {PROFILE_BASE} NEG_TEST', 5)
    send(chan, 'commit', 10)

    # 7a: Create a profile with non-stop timing
    out = send(chan, f'{PROFILE_BASE} TIMING_APPLIED test-duration non-stop computation-interval 60 probe-interval 1', 5)
    log_step(results, "7a", "Create profile TIMING_APPLIED (non-stop ci=60 pi=1)",
             'ERROR' not in out, out, "Profile created successfully")

    out = send(chan, 'commit', 15)
    log_step(results, "7b", "Commit TIMING_APPLIED profile",
             'ERROR' not in out and ('committed' in out.lower() or 'not applicable' in out.lower()),
             out, "Commit succeeds")

    # Check existing sessions to find one to apply profile to
    send(chan, 'end', 3)
    out = send(chan, 'show services performance-monitoring cfm tests proactive | no-more', 10)
    print(f"Existing sessions:\n{out}\n")
    results.append({"step": "7c-info", "name": "Existing proactive sessions", "status": "INFO", "output": out.strip(), "expected": ""})

    # Try to find an existing DM session and apply the profile
    send(chan, 'configure', 5)
    
    # Check existing DM sessions
    out = send(chan, f'show config services performance-monitoring cfm two-way-delay-measurement | no-more', 10)
    print(f"Existing DM sessions config:\n{out}\n")
    results.append({"step": "7d-info", "name": "Existing DM session configs", "status": "INFO", "output": out.strip(), "expected": ""})

    # Create a test session referencing the profile
    out = send(chan, 'services performance-monitoring cfm two-way-delay-measurement DM_TIMING_SESSION profile TIMING_APPLIED', 5)
    log_step(results, "7e", "Create DM session with TIMING_APPLIED profile",
             'ERROR' not in out, out, "Session created with profile reference")

    out = send(chan, 'commit', 15)
    print(f"Commit session: {out}\n")

    # Verify profile association
    send(chan, 'end', 3)
    out = send(chan, 'show services performance-monitoring cfm tests proactive two-way-delay session-name DM_TIMING_SESSION detail | no-more', 10)
    print(f"Session detail:\n{out}\n")
    log_step(results, "7f", "Session shows TIMING_APPLIED profile",
             'TIMING_APPLIED' in out or 'non-stop' in out.lower() or 'computation-interval' in out.lower(),
             out, "Session references TIMING_APPLIED profile")

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

    # Save results
    with open('/home/dn/sw236668_results.json', 'w') as f:
        json.dump(results, f, indent=2)

    # Print summary
    print("\n" + "="*60)
    print("SUMMARY")
    print("="*60)
    passed = sum(1 for r in results if r['status'] == 'PASS')
    failed = sum(1 for r in results if r['status'] == 'FAIL')
    info = sum(1 for r in results if r['status'] == 'INFO')
    total = passed + failed
    print(f"PASSED: {passed}/{total}")
    print(f"FAILED: {failed}/{total}")
    for r in results:
        if r['status'] != 'INFO':
            print(f"  [{r['status']}] {r['step']}: {r['name']}")

if __name__ == '__main__':
    main()
