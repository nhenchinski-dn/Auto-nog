#!/usr/bin/env python3
"""SW-244113: Steps 11-12 — allow-default enabled/disabled within VRF.

Step 11: Enable allow-default, remove specific reverse-path route so source
         matches only the default route in VRF via ingress interface → traffic passes.
Step 12: Disable allow-default → same traffic (matching only default route) is dropped.
"""

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

    # =========================================================================
    # STEP 11: Enable allow-default, remove specific route
    # =========================================================================
    print("\n" + "="*70)
    print("STEP 11: Enable allow-default + remove specific reverse-path route")
    print("="*70)

    # First revert per-AFI overrides back to global strict mode
    # Then enable allow-default, remove specific route 10.100.10.0/24
    step11_cmds = [
        ("configure", 5),
        # Remove per-AFI overrides
        ("interfaces ge400-0/0/3.100 urpf no address-family ipv4", 5),
        ("top", 3),
        ("interfaces ge400-0/0/3.100 urpf no address-family ipv6", 5),
        ("top", 3),
        # Enable allow-default on ge400-0/0/3.100
        ("interfaces ge400-0/0/3.100 urpf allow-default enabled", 5),
        ("top", 3),
        # Also enable allow-default on bundle-10.100 (must be identical per RST docs)
        ("interfaces bundle-10.100 urpf allow-default enabled", 5),
        ("top", 3),
        # Also enable allow-default on ge400-0/0/5 if it has urpf (from testrpf VRF)
        ("interfaces ge400-0/0/5 urpf allow-default enabled", 5),
        ("top", 3),
        # Remove the specific reverse-path route for 10.100.10.0/24
        ("network-services vrf instance urpf-vrf protocols static address-family ipv4-unicast", 5),
        ("no route 10.100.10.0/24 next-hop 10.100.1.2 interface ge400-0/0/3.100", 5),
        ("top", 3),
        ("commit", 20),
    ]

    commit_ok11 = True
    for cmd, wait in step11_cmds:
        output = rp(chan, cmd, wait)
        if "ERROR" in output:
            print(f"  *** ERROR in: {cmd} ***")
            if cmd == "commit": commit_ok11 = False

    print("\n--- Step 11 Verification ---")
    rp(chan, "end", 3)
    v11_detail = rp(chan, "show interfaces detail ge400-0/0/3.100 | no-more", 12)
    v11_routes = rp(chan, "show route vrf urpf-vrf | no-more", 12)
    v11_cfg = rp(chan, "show config interfaces ge400-0/0/3.100 urpf | no-more", 10)

    # Verify: allow-default enabled, specific route removed, default route still present
    allow_default_on = "Allow-default: enabled" in v11_detail
    specific_route_gone = "10.100.10.0/24" not in v11_routes
    default_route_present = "0.0.0.0/0" in v11_routes

    print(f"\n  allow-default enabled: {'YES' if allow_default_on else 'NO'}")
    print(f"  specific route 10.100.10.0/24 removed: {'YES' if specific_route_gone else 'NO'}")
    print(f"  default route 0.0.0.0/0 present: {'YES' if default_route_present else 'NO'}")
    print(f"\n>>> STEP 11 CONFIG: {'PASS' if (commit_ok11 and allow_default_on and specific_route_gone) else 'FAIL'}")
    print(f"    Traffic needed: Src IP 10.100.10.100 → matches only default route via ingress IF → should PASS uRPF")

    RESULTS = {"step11": {
        "detail": v11_detail, "routes": v11_routes, "config": v11_cfg,
        "config_result": "PASS" if commit_ok11 else "FAIL",
    }}

    json.dump(RESULTS, open("/home/dn/output/sw244113_step11_config.json","w"), indent=2)

    chan.close(); ssh.close()

if __name__ == "__main__":
    main()
