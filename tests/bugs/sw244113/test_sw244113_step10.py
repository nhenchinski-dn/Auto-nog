#!/usr/bin/env python3
"""SW-244113: Step 10 — Reverse per-AFI: IPv4 loose + IPv6 strict."""

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

def extract(text, label):
    for line in text.split('\n'):
        if label in line:
            val = line.split(':')[-1].strip().split('(')[0].strip()
            return int(val) if val.isdigit() else 0
    return 0

def main():
    print("Connecting...")
    ssh, chan = connect()

    # Reconfigure: IPv4 loose + IPv6 strict
    print("\n" + "="*70)
    print("STEP 10: Reverse per-AFI — IPv4 loose + IPv6 strict")
    print("="*70)

    cmds = [
        ("configure", 5),
        ("interfaces ge400-0/0/3.100 urpf address-family ipv4 mode loose", 5),
        ("top", 3),
        ("interfaces ge400-0/0/3.100 urpf address-family ipv6 mode strict", 5),
        ("top", 3),
        ("commit", 15),
    ]

    commit_ok = True
    for cmd, wait in cmds:
        output = rp(chan, cmd, wait)
        if "ERROR" in output:
            print(f"  *** ERROR ***")
            if cmd == "commit": commit_ok = False

    print("\n--- Step 10 Config Verification ---")
    rp(chan, "end", 3)
    v_detail = rp(chan, "show interfaces detail ge400-0/0/3.100 | no-more", 12)
    v_cfg = rp(chan, "show config interfaces ge400-0/0/3.100 urpf | no-more", 10)

    print(f"\n>>> STEP 10 CONFIG: {'PASS' if commit_ok else 'FAIL'}")
    print(f"    Now need traffic: invalid-src IPv4 (route via diff IF) → FORWARDED (loose)")
    print(f"                      invalid-src IPv6 (route via diff IF) → DROPPED (strict)")

    # Record baseline
    v_baseline = rp(chan, "show interfaces counters ge400-0/0/3.100 | no-more", 10)

    json.dump({"detail": v_detail, "config": v_cfg, "baseline": v_baseline,
               "config_result": "PASS" if commit_ok else "FAIL"},
              open("/home/dn/output/sw244113_step10_config.json","w"), indent=2)

    chan.close(); ssh.close()

if __name__ == "__main__":
    main()
