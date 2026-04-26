#!/usr/bin/env python3
"""SW-244113: Steps 9-10 — Per-AFI mode overrides on ge400-0/0/3.100."""

import paramiko, time, re, json

HOST = "100.64.8.59"

def clean(text):
    text = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)
    text = re.sub(r'\r', '', text)
    text = re.sub(r'-- More -- \(Press q to quit\)\s*', '', text)
    return text.strip()

def connect():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username='dnroot', password='dnroot', timeout=30,
                look_for_keys=False, allow_agent=False)
    chan = ssh.invoke_shell(width=300, height=5000)
    time.sleep(6); chan.recv(65535)
    return ssh, chan

def run(chan, cmd, wait=8):
    chan.send(cmd + '\n'); time.sleep(wait)
    out = b''
    while chan.recv_ready(): out += chan.recv(65535); time.sleep(0.5)
    return clean(out.decode(errors='replace'))

def rp(chan, cmd, wait=8):
    output = run(chan, cmd, wait)
    print(f"  [{cmd}]")
    for line in output.split('\n'):
        print(f"    {line}")
    return output

def main():
    print("Connecting...")
    ssh, chan = connect()

    # =========================================================================
    # STEP 9: Per-AFI — IPv4 strict + IPv6 loose on ge400-0/0/3.100
    # =========================================================================
    print("\n" + "="*70)
    print("STEP 9: Per-AFI — IPv4 strict + IPv6 loose on ge400-0/0/3.100")
    print("="*70)

    step9_cmds = [
        ("configure", 5),
        ("interfaces ge400-0/0/3.100 urpf address-family ipv4 admin-state enabled", 5),
        ("top", 3),
        ("interfaces ge400-0/0/3.100 urpf address-family ipv4 mode strict", 5),
        ("top", 3),
        ("interfaces ge400-0/0/3.100 urpf address-family ipv6 admin-state enabled", 5),
        ("top", 3),
        ("interfaces ge400-0/0/3.100 urpf address-family ipv6 mode loose", 5),
        ("top", 3),
        ("commit", 15),
    ]

    commit_ok9 = True
    for cmd, wait in step9_cmds:
        output = rp(chan, cmd, wait)
        if "ERROR" in output:
            print(f"  *** ERROR ***")
            if cmd == "commit": commit_ok9 = False

    print("\n--- Step 9 Verification ---")
    rp(chan, "end", 3)
    v9_detail = rp(chan, "show interfaces detail ge400-0/0/3.100 | no-more", 12)
    v9_cfg = rp(chan, "show config interfaces ge400-0/0/3.100 urpf | no-more", 10)

    step9_pass = (commit_ok9 and
                  "uRPF IPv4 check: enabled, Mode: strict" in v9_detail and
                  "uRPF IPv6 check: enabled, Mode: loose" in v9_detail)
    print(f"\n>>> STEP 9 CONFIG RESULT: {'PASS' if step9_pass else 'FAIL'}")
    print(f"    (Traffic verification needed: invalid-src IPv4 should drop, invalid-src IPv6 should forward)")

    RESULTS = {"step9": {"detail": v9_detail, "config": v9_cfg, "config_result": "PASS" if step9_pass else "FAIL"}}

    # Now we need traffic to verify the per-AFI behavior.
    # Record baseline counters before traffic
    print("\n--- Step 9 baseline counters (before traffic) ---")
    v9_baseline = rp(chan, "show interfaces counters ge400-0/0/3.100 | no-more", 10)
    RESULTS["step9"]["baseline_counters"] = v9_baseline

    # We'll pause here — user needs to send traffic for verification
    print("\n" + "="*70)
    print("STEP 9 CONFIG DONE. Per-AFI is: IPv4=strict, IPv6=loose")
    print("Now need traffic verification:")
    print("  Stream 2 (invalid IPv4 src 10.100.99.100) → should be DROPPED (strict)")
    print("  Stream 4 (invalid IPv6 src 2001:db8:99::100) → should be FORWARDED (loose)")
    print("="*70)

    json.dump(RESULTS, open("/home/dn/output/sw244113_step9_config.json","w"), indent=2)

    chan.close(); ssh.close()

if __name__ == "__main__":
    main()
