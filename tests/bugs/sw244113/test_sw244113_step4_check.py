#!/usr/bin/env python3
"""SW-244113: Step 4 — Check counters with valid-source IPv4 traffic running."""

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

def main():
    print("Connecting...")
    ssh, chan = connect()

    print("\n" + "="*70)
    print("STEP 4: Valid-source IPv4 — checking counters (Stream 1 running)")
    print("="*70)

    # First snapshot
    print("\n--- Snapshot 1 ---")
    c1 = run_and_print(chan, "show interfaces counters ge400-0/0/3.100 | no-more", 10)

    # Wait 10 seconds for traffic accumulation
    print("\n... waiting 10 seconds ...")
    time.sleep(10)

    # Second snapshot
    print("\n--- Snapshot 2 (after 10s) ---")
    c2 = run_and_print(chan, "show interfaces counters ge400-0/0/3.100 | no-more", 10)

    # Also check parent interface counters
    print("\n--- Parent interface counters ---")
    c3 = run_and_print(chan, "show interfaces counters ge400-0/0/3 | no-more", 10)

    # Parse uRPF drops from snapshot 2
    urpf_v4 = "0"
    urpf_v6 = "0"
    rx_packets = "0"
    for line in c2.split('\n'):
        if "uRPF Ipv4 drops:" in line:
            urpf_v4 = line.split(':')[-1].strip()
        if "uRPF Ipv6 drops:" in line:
            urpf_v6 = line.split(':')[-1].strip()
        if "RX packets:" in line:
            rx_packets = line.split(':')[1].strip().split('(')[0].strip()

    print(f"\n{'='*70}")
    print(f"STEP 4 ANALYSIS:")
    print(f"  RX packets on ge400-0/0/3.100: {rx_packets}")
    print(f"  uRPF IPv4 drops: {urpf_v4}")
    print(f"  uRPF IPv6 drops: {urpf_v6}")

    rx_val = int(rx_packets) if rx_packets.isdigit() else 0
    urpf_v4_val = int(urpf_v4) if urpf_v4.isdigit() else 0

    if rx_val > 0 and urpf_v4_val == 0:
        print(f"  RESULT: PASS — Traffic is being received with ZERO uRPF IPv4 drops")
    elif rx_val == 0:
        print(f"  RESULT: CHECK — No RX packets seen on sub-interface (traffic may not be reaching)")
    else:
        print(f"  RESULT: FAIL — uRPF IPv4 drops detected for valid-source traffic")
    print(f"{'='*70}")

    results = {
        "snapshot1": c1,
        "snapshot2": c2,
        "parent_counters": c3,
        "rx_packets": rx_packets,
        "urpf_v4_drops": urpf_v4,
        "urpf_v6_drops": urpf_v6,
    }

    with open("/home/dn/output/sw244113_step4.json", "w") as f:
        json.dump(results, f, indent=2)

    chan.close()
    ssh.close()

if __name__ == "__main__":
    main()
