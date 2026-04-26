#!/usr/bin/env python3
"""SW-244113: Strict uRPF | Non-default VRF — Initial device reconnaissance."""

import paramiko
import time
import re
import json
import sys

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

def main():
    print(f"Connecting to {HOST} (wky1c7vd00008p2)...")
    ssh, chan = connect()

    commands = [
        ("System Version", "show system version | no-more"),
        ("Interfaces (ge400-0/0/3)", "show interfaces ge400-0/0/3 | no-more"),
        ("Interfaces detail (ge400-0/0/3)", "show interfaces detail ge400-0/0/3 | no-more"),
        ("All sub-interfaces on ge400-0/0/3", "show interfaces ge400-0/0/3.* | no-more"),
        ("Bundle interfaces", "show interfaces bundle* | no-more"),
        ("Current VRFs", "show vrfs | no-more"),
        ("Current uRPF config", "show config interfaces ge400-0/0/3 urpf | no-more"),
        ("Current VRF config (network-services)", "show config network-services vrf | no-more"),
    ]

    results = {}
    for label, cmd in commands:
        print(f"\n{'='*60}")
        print(f">>> {label}: {cmd}")
        print('='*60)
        output = run(chan, cmd)
        print(output)
        results[label] = output

    chan.close()
    ssh.close()

    with open("/home/dn/output/sw244113_recon.json", "w") as f:
        json.dump(results, f, indent=2)
    print("\nRecon saved to /home/dn/output/sw244113_recon.json")

if __name__ == "__main__":
    main()
