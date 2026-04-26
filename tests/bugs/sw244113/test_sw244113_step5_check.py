#!/usr/bin/env python3
"""SW-244113: Step 5 — Check counters with invalid-source IPv4 traffic running."""

import paramiko
import time
import re
import json

HOST = "100.64.8.59"
USER = "dnroot"
PASS = "dnroot"

def clean(text):
    text = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)
    text = re.sub(r'\r', '', text)
    text = re.sub(r'-- More -- \(Press q to quit\)\s*', '', text)
    return text.strip()

def connect():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASS, timeout=30,
                look_for_keys=False, allow_agent=False)
    chan = ssh.invoke_shell(width=300, height=5000)
    time.sleep(6)
    chan.recv(65535)
    return ssh, chan

def run(chan, cmd, wait=10):
    chan.send(cmd + '\n')
    time.sleep(wait)
    out = b''
    while chan.recv_ready():
        out += chan.recv(65535)
        time.sleep(0.5)
    return clean(out.decode(errors='replace'))

def run_and_print(chan, cmd, wait=10):
    output = run(chan, cmd, wait)
    print(f"\n>>> {cmd}")
    for line in output.split('\n'):
        print(f"  {line}")
    return output

def extract_counter(text, label):
    for line in text.split('\n'):
        if label in line:
            val = line.split(':')[-1].strip().split('(')[0].strip()
            return int(val) if val.isdigit() else 0
    return 0

def main():
    print("Connecting...")
    ssh, chan = connect()

    print("\n" + "="*70)
    print("STEP 5: Invalid-source IPv4 — checking uRPF drops (Stream 2 running)")
    print("="*70)

    print("\n--- Snapshot 1 ---")
    c1 = run_and_print(chan, "show interfaces counters ge400-0/0/3.100 | no-more", 10)

    print("\n... waiting 10 seconds ...")
    time.sleep(10)

    print("\n--- Snapshot 2 (after 10s) ---")
    c2 = run_and_print(chan, "show interfaces counters ge400-0/0/3.100 | no-more", 10)

    rx1 = extract_counter(c1, "RX packets:")
    rx2 = extract_counter(c2, "RX packets:")
    urpf_v4_1 = extract_counter(c1, "uRPF Ipv4 drops:")
    urpf_v4_2 = extract_counter(c2, "uRPF Ipv4 drops:")
    tx1 = extract_counter(c1, "TX packets:")
    tx2 = extract_counter(c2, "TX packets:")

    rx_delta = rx2 - rx1
    urpf_delta = urpf_v4_2 - urpf_v4_1
    tx_delta = tx2 - tx1

    print(f"\n{'='*70}")
    print(f"STEP 5 ANALYSIS:")
    print(f"  RX packets delta (10s):     {rx_delta:,}")
    print(f"  uRPF IPv4 drops delta (10s): {urpf_delta:,}")
    print(f"  TX packets delta (10s):      {tx_delta:,}")
    print(f"  uRPF IPv4 drops total:       {urpf_v4_2:,}")

    if urpf_delta > 0 and tx_delta == 0:
        print(f"  RESULT: PASS — Invalid-source traffic is being DROPPED by uRPF strict")
        print(f"         {urpf_delta:,} drops in 10 seconds, zero TX (no forwarding)")
    elif urpf_delta > 0 and tx_delta > 0:
        print(f"  RESULT: PARTIAL — uRPF drops incrementing but some TX also seen")
    elif urpf_delta == 0 and rx_delta > 0:
        print(f"  RESULT: FAIL — Traffic received but NO uRPF drops (not being checked?)")
    else:
        print(f"  RESULT: CHECK — No traffic delta seen")
    print(f"{'='*70}")

    results = {
        "snapshot1": c1, "snapshot2": c2,
        "rx_delta": rx_delta, "urpf_v4_delta": urpf_delta,
        "tx_delta": tx_delta, "urpf_v4_total": urpf_v4_2,
    }
    with open("/home/dn/output/sw244113_step5.json", "w") as f:
        json.dump(results, f, indent=2)

    chan.close()
    ssh.close()

if __name__ == "__main__":
    main()
