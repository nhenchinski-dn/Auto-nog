#!/usr/bin/env python3
"""SW-244113: Step 9 — Verify per-AFI: IPv4 strict drops, IPv6 loose forwards."""

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

def run(chan, cmd, wait=10):
    chan.send(cmd + '\n'); time.sleep(wait)
    out = b''
    while chan.recv_ready(): out += chan.recv(65535); time.sleep(0.5)
    return clean(out.decode(errors='replace'))

def extract(text, label):
    for line in text.split('\n'):
        if label in line:
            val = line.split(':')[-1].strip().split('(')[0].strip()
            return int(val) if val.isdigit() else 0
    return 0

def main():
    print("Connecting...")
    ssh, chan = connect()

    print("\n" + "="*70)
    print("STEP 9 TRAFFIC VERIFY: IPv4=strict, IPv6=loose")
    print("  Invalid-src IPv4 → expect DROPPED (uRPF v4 drops incrementing)")
    print("  Invalid-src IPv6 → expect FORWARDED (uRPF v6 drops NOT incrementing)")
    print("="*70)

    c1 = run(chan, "show interfaces counters ge400-0/0/3.100 | no-more", 10)
    print("Snapshot 1 captured. Waiting 10s...")
    time.sleep(10)
    c2 = run(chan, "show interfaces counters ge400-0/0/3.100 | no-more", 10)

    rx1 = extract(c1, "RX packets:")
    rx2 = extract(c2, "RX packets:")
    urpf_v4_1 = extract(c1, "uRPF Ipv4 drops:")
    urpf_v4_2 = extract(c2, "uRPF Ipv4 drops:")
    urpf_v6_1 = extract(c1, "uRPF Ipv6 drops:")
    urpf_v6_2 = extract(c2, "uRPF Ipv6 drops:")

    rx_delta = rx2 - rx1
    v4_delta = urpf_v4_2 - urpf_v4_1
    v6_delta = urpf_v6_2 - urpf_v6_1

    print(f"\n  RX packets delta (10s):      {rx_delta:,}")
    print(f"  uRPF IPv4 drops delta (10s): {v4_delta:,}")
    print(f"  uRPF IPv6 drops delta (10s): {v6_delta:,}")
    print(f"  uRPF IPv4 drops total:       {urpf_v4_2:,}")
    print(f"  uRPF IPv6 drops total:       {urpf_v6_2:,}")

    # Per-AFI check:
    # IPv4 strict → invalid-src IPv4 should increment uRPF v4 drops
    # IPv6 loose  → invalid-src IPv6 should NOT increment uRPF v6 drops (loose permits if route exists in VRF)
    ipv4_strict_ok = v4_delta > 0
    ipv6_loose_ok = v6_delta == 0

    print(f"\n  IPv4 strict (invalid-src dropped?):    {'YES — drops incrementing' if ipv4_strict_ok else 'NO — not dropping'}")
    print(f"  IPv6 loose  (invalid-src forwarded?):  {'YES — no new drops' if ipv6_loose_ok else 'NO — still dropping (v6 drops incrementing)'}")

    if ipv4_strict_ok and ipv6_loose_ok:
        result = "PASS"
    elif ipv4_strict_ok and not ipv6_loose_ok:
        result = "PARTIAL — IPv4 strict works but IPv6 loose still dropping"
    else:
        result = "FAIL"

    print(f"\n  STEP 9 OVERALL: {result}")

    print(f"\n--- Full counters snapshot 2 ---")
    for line in c2.split('\n'):
        print(f"  {line}")

    json.dump({"snapshot1": c1, "snapshot2": c2, "rx_delta": rx_delta,
               "v4_delta": v4_delta, "v6_delta": v6_delta, "result": result},
              open("/home/dn/output/sw244113_step9_traffic.json","w"), indent=2)

    chan.close(); ssh.close()

if __name__ == "__main__":
    main()
