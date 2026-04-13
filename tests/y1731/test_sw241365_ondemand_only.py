#!/usr/bin/env python3
"""Quick test of on-demand SLM commands for SW-241365, Steps 8-10 only."""

import paramiko
import time
import re
import sys

DEVICE_IP = "100.64.4.93"

def connect():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(DEVICE_IP, username='dnroot', password='dnroot',
                timeout=30, look_for_keys=False, allow_agent=False)
    chan = ssh.invoke_shell(width=300)
    time.sleep(5)
    chan.recv(65535)
    return ssh, chan

def run(chan, cmd, wait=10):
    chan.send(cmd + '\n')
    time.sleep(wait)
    out = b''
    while chan.recv_ready():
        out += chan.recv(65535)
    text = out.decode(errors='replace')
    text = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)
    text = re.sub(r'\x1b\[[\?]?[0-9;]*[a-zA-Z]', '', text)
    return text

ssh, chan = connect()
print("[OK] Connected\n")

# Step 8: On-demand SLM targeting mep-id
print("=" * 60)
print("STEP 8: On-demand SLM two-way targeting mep-id 2")
print("=" * 60)
cmd = "run ethernet-oam cfm on-demand synthetic-loss-measurement two-way maintenance-domain MD-CUST maintenance-association MA-CUST target mep-id 2"
print(f"CMD: {cmd}\n")
out = run(chan, cmd, wait=30)
print(f"OUTPUT:\n{out}\n")

time.sleep(5)

# Show on-demand detail
print("--- On-demand detail ---")
detail = run(chan, "show services performance-monitoring cfm tests on-demand two-way-synthetic-loss detail | no-more", wait=12)
print(f"{detail}\n")

# Step 9: On-demand SLM targeting mac-address
print("=" * 60)
print("STEP 9: On-demand SLM two-way targeting mac-address")
print("=" * 60)
cmd = "run ethernet-oam cfm on-demand synthetic-loss-measurement two-way maintenance-domain MD-CUST maintenance-association MA-CUST target mac-address 84:40:76:90:cd:f6"
print(f"CMD: {cmd}\n")
out = run(chan, cmd, wait=30)
print(f"OUTPUT:\n{out}\n")

time.sleep(5)

detail = run(chan, "show services performance-monitoring cfm tests on-demand two-way-synthetic-loss detail | no-more", wait=12)
print(f"--- On-demand detail ---\n{detail}\n")

# Step 10: On-demand SLM with PCP override
print("=" * 60)
print("STEP 10: On-demand SLM with PCP 3 override")
print("=" * 60)
cmd = "run ethernet-oam cfm on-demand synthetic-loss-measurement two-way maintenance-domain MD-CUST maintenance-association MA-CUST target mep-id 2 pcp 3"
print(f"CMD: {cmd}\n")
out = run(chan, cmd, wait=30)
print(f"OUTPUT:\n{out}\n")

time.sleep(5)

detail = run(chan, "show services performance-monitoring cfm tests on-demand two-way-synthetic-loss detail | no-more", wait=12)
print(f"--- On-demand detail ---\n{detail}\n")

ssh.close()
print("Done.")
