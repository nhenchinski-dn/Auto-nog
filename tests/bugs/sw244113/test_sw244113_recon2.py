#!/usr/bin/env python3
"""SW-244113: Additional recon — interfaces, bundles, link states."""

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

def run(chan, cmd, wait=12):
    chan.send(cmd + '\n')
    time.sleep(wait)
    out = b''
    while chan.recv_ready():
        out += chan.recv(65535)
        time.sleep(0.5)
    return clean(out.decode(errors='replace'))

def main():
    print(f"Connecting to {HOST}...")
    ssh, chan = connect()

    commands = [
        ("All interfaces brief", "show interfaces | no-more", 15),
        ("Bundle interfaces config", "show config interfaces bundle-1 | no-more", 10),
        ("Bundle-2 config", "show config interfaces bundle-2 | no-more", 10),
        ("ge400-0/0/3 config", "show config interfaces ge400-0/0/3 | no-more", 10),
        ("ge400-0/0/5 config+detail", "show interfaces detail ge400-0/0/5 | no-more", 12),
        ("VRF instance detail", "show network-services vrf | no-more", 10),
        ("Existing routes in testrpf", "show route vrf testrpf | no-more", 10),
        ("ge400-0/0/5 counters", "show interfaces counters ge400-0/0/5 | no-more", 10),
    ]

    results = {}
    for label, cmd, wait in commands:
        print(f"\n{'='*60}")
        print(f">>> {label}: {cmd}")
        print('='*60)
        output = run(chan, cmd, wait)
        print(output)
        results[label] = output

    chan.close()
    ssh.close()

    with open("/home/dn/output/sw244113_recon2.json", "w") as f:
        json.dump(results, f, indent=2)
    print("\nRecon2 saved.")

if __name__ == "__main__":
    main()
