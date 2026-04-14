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

    send(chan, 'end', 3)
    send(chan, 'configure', 5)
    send(chan, 'rollback', 5)

    # Clean up
    send(chan, 'no services performance-monitoring cfm two-way-delay-measurement DM_TIMING_SESSION', 3)
    send(chan, f'no {PROFILE_BASE} TIMING_APPLIED', 3)
    send(chan, 'commit', 15)

    # ===================================================================
    # TEST 7: Apply profile to a DM session
    # ===================================================================
    print("\n" + "="*60)
    print("TEST 7: Apply profile to a session")
    print("="*60)

    # 7a: Create profile TIMING_APPLIED
    out = send(chan, f'{PROFILE_BASE} TIMING_APPLIED test-duration non-stop computation-interval 60 probe-interval 1', 5)
    log_step(results, "7a", "Create profile TIMING_APPLIED (non-stop ci=60 pi=1)",
             'ERROR' not in out, out, "Profile created")

    out = send(chan, 'commit', 15)
    log_step(results, "7b", "Commit TIMING_APPLIED",
             'committed' in out.lower() or 'not applicable' in out.lower() or ('ERROR' not in out and 'failed' not in out.lower()),
             out, "Commit succeeds")

    # Explore DM session 'source' sub-options
    out = send(chan, 'services performance-monitoring cfm two-way-delay-measurement DM_TIMING_SESSION source ?', 5)
    print(f"source ?:\n{out}\n")

    # Try source md-name / ma-name / mep-id as separate sub-commands
    out1 = send(chan, 'services performance-monitoring cfm two-way-delay-measurement DM_TIMING_SESSION source md-name MD-CUST', 5)
    print(f"source md-name MD-CUST:\n{out1}\n")

    out2 = send(chan, 'services performance-monitoring cfm two-way-delay-measurement DM_TIMING_SESSION source ma-name MA-CUST', 5)
    print(f"source ma-name MA-CUST:\n{out2}\n")

    out3 = send(chan, 'services performance-monitoring cfm two-way-delay-measurement DM_TIMING_SESSION source mep-id 1', 5)
    print(f"source mep-id 1:\n{out3}\n")

    source_ok = 'ERROR' not in out1 and 'ERROR' not in out2 and 'ERROR' not in out3
    log_step(results, "7c", "Configure source (md=MD-CUST, ma=MA-CUST, mep=1)",
             source_ok, f"md-name: {out1}\nma-name: {out2}\nmep-id: {out3}",
             "Source configured without errors")

    # Assign profile
    out = send(chan, 'services performance-monitoring cfm two-way-delay-measurement DM_TIMING_SESSION profile TIMING_APPLIED', 5)
    log_step(results, "7d", "Assign profile TIMING_APPLIED to session",
             'ERROR' not in out, out, "Profile assigned")

    # Set target
    out = send(chan, 'services performance-monitoring cfm two-way-delay-measurement DM_TIMING_SESSION target mep-id 2', 5)
    log_step(results, "7e", "Set target mep-id 2",
             'ERROR' not in out, out, "Target set")

    # Enable admin-state
    out = send(chan, 'services performance-monitoring cfm two-way-delay-measurement DM_TIMING_SESSION admin-state enable', 5)
    log_step(results, "7f", "Enable admin-state",
             'ERROR' not in out, out, "Enabled")

    # Commit
    out = send(chan, 'commit', 15)
    print(f"Commit session: {out}\n")
    commit_ok = 'committed' in out.lower() or 'not applicable' in out.lower() or ('ERROR' not in out and 'failed' not in out.lower())
    log_step(results, "7g", "Commit DM session with profile",
             commit_ok, out, "Commit succeeds")

    # Show config
    out_cfg = send(chan, f'show config services performance-monitoring cfm two-way-delay-measurement DM_TIMING_SESSION | no-more', 10)
    print(f"Session config:\n{out_cfg}\n")
    log_step(results, "7h", "Session config shows TIMING_APPLIED",
             'TIMING_APPLIED' in out_cfg, out_cfg,
             "Profile name in config")

    # Show operational detail
    send(chan, 'end', 3)
    out_detail = send(chan, 'show services performance-monitoring cfm tests proactive two-way-delay session-name DM_TIMING_SESSION detail | no-more', 10)
    print(f"Session detail:\n{out_detail}\n")
    log_step(results, "7i", "Session operational state visible",
             'DM_TIMING_SESSION' in out_detail or 'TIMING_APPLIED' in out_detail,
             out_detail, "Session visible in operational state")

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

    with open('/home/dn/sw236668_fix3_results.json', 'w') as f:
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
