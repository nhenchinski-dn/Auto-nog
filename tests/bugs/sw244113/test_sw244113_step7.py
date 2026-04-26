#!/usr/bin/env python3
"""SW-244113: Step 7 — Configure uRPF strict on bundle-10.100 within VRF."""

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

    print("\n" + "="*70)
    print("STEP 7: Configure uRPF strict on bundle-10.100 within urpf-vrf")
    print("="*70)

    cmds = [
        ("configure", 5),
        ("interfaces bundle-10.100 urpf admin-state enabled", 5),
        ("top", 3),
        ("interfaces bundle-10.100 urpf mode strict", 5),
        ("top", 3),
        ("commit", 15),
    ]

    commit_ok = True
    for cmd, wait in cmds:
        output = rp(chan, cmd, wait)
        if "ERROR" in output:
            print(f"  *** ERROR ***")
            if cmd == "commit": commit_ok = False

    print("\n--- Step 7 Verification ---")
    rp(chan, "end", 3)
    v_detail = rp(chan, "show interfaces detail bundle-10.100 | no-more", 12)
    v_cfg = rp(chan, "show config interfaces bundle-10.100 urpf | no-more", 10)
    v_counters = rp(chan, "show interfaces counters bundle-10.100 | no-more", 10)

    # Also verify ge400-0/0/3.100 still has uRPF strict
    v_ge = rp(chan, "show interfaces detail ge400-0/0/3.100 | no-more", 12)

    step7_pass = (commit_ok and
                  "uRPF IPv4 check: enabled" in v_detail and
                  "Mode: strict" in v_detail)
    print(f"\n>>> STEP 7 RESULT: {'PASS' if step7_pass else 'FAIL'}")

    # Check that both interfaces show uRPF independently
    ge_urpf = "uRPF IPv4 check: enabled" in v_ge and "Mode: strict" in v_ge
    bun_urpf = "uRPF IPv4 check: enabled" in v_detail and "Mode: strict" in v_detail
    print(f"  ge400-0/0/3.100 uRPF strict: {'YES' if ge_urpf else 'NO'}")
    print(f"  bundle-10.100 uRPF strict:   {'YES' if bun_urpf else 'NO'}")

    results = {
        "bundle_detail": v_detail, "bundle_config": v_cfg,
        "bundle_counters": v_counters, "ge_detail": v_ge,
        "result": "PASS" if step7_pass else "FAIL"
    }
    json.dump(results, open("/home/dn/output/sw244113_step7.json","w"), indent=2)

    chan.close(); ssh.close()

if __name__ == "__main__":
    main()
