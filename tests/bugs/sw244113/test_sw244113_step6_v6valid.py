#!/usr/bin/env python3
"""SW-244113: Step 6a — Check counters with valid-source IPv6 traffic."""

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
    print("STEP 6a: Valid-source IPv6 — checking counters (Stream 3)")
    print("="*70)

    c1 = run(chan, "show interfaces counters ge400-0/0/3.100 | no-more", 10)
    print("Snapshot 1 captured. Waiting 10s...")
    time.sleep(10)
    c2 = run(chan, "show interfaces counters ge400-0/0/3.100 | no-more", 10)

    rx1 = extract(c1, "RX packets:")
    rx2 = extract(c2, "RX packets:")
    urpf_v6_1 = extract(c1, "uRPF Ipv6 drops:")
    urpf_v6_2 = extract(c2, "uRPF Ipv6 drops:")
    urpf_v4_2 = extract(c2, "uRPF Ipv4 drops:")

    rx_delta = rx2 - rx1
    urpf_v6_delta = urpf_v6_2 - urpf_v6_1

    print(f"\n  RX packets delta (10s):      {rx_delta:,}")
    print(f"  uRPF IPv6 drops delta (10s): {urpf_v6_delta:,}")
    print(f"  uRPF IPv6 drops total:       {urpf_v6_2:,}")
    print(f"  uRPF IPv4 drops total:       {urpf_v4_2:,} (from step 5)")

    if rx_delta > 0 and urpf_v6_delta == 0:
        print(f"  RESULT: PASS — Valid IPv6 traffic forwarded, zero uRPF IPv6 drops")
    elif rx_delta == 0:
        print(f"  RESULT: CHECK — No RX delta seen")
    else:
        print(f"  RESULT: FAIL — uRPF IPv6 drops seen for valid-source traffic")

    # Print full snapshot 2
    print(f"\n--- Full counters ---")
    for line in c2.split('\n'):
        print(f"  {line}")

    json.dump({"snapshot1": c1, "snapshot2": c2, "rx_delta": rx_delta,
               "urpf_v6_delta": urpf_v6_delta}, open("/home/dn/output/sw244113_step6a.json","w"), indent=2)

    chan.close(); ssh.close()

if __name__ == "__main__":
    main()
