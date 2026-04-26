#!/usr/bin/env python3
"""Run target-stack install with yes/no prompt handling."""
import paramiko
import time
import re
import sys
import os

USER = "dnroot"
PASS = "dnroot"


def clean(text):
    text = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)
    text = re.sub(r'\r', '', text)
    text = re.sub(r'-- More -- \(Press q to quit\)\s*', '', text)
    return text


def remove_host_key(host):
    os.system(f'ssh-keygen -f /home/dn/.ssh/known_hosts -R {host} 2>/dev/null')


host = sys.argv[1]

print(f"=== Connecting to {host} ===", flush=True)
remove_host_key(host)
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(host, username=USER, password=PASS,
               look_for_keys=False, allow_agent=False, timeout=30)
shell = client.invoke_shell(width=250, height=5000)
time.sleep(8)
if shell.recv_ready():
    shell.recv(65535)
print("Connected.", flush=True)

print("\n=== Running target-stack install ===", flush=True)
shell.send("request system target-stack install\n")
output = ""
yes_sent = False

for i in range(600):
    time.sleep(3)
    while shell.recv_ready():
        output += shell.recv(65535).decode("utf-8", errors="replace")
    cleaned = clean(output)

    if not yes_sent and ("yes/no" in cleaned or "Yes/No" in cleaned):
        time.sleep(1)
        shell.send("yes\n")
        yes_sent = True
        print("  -> Sent 'yes' to confirmation prompt", flush=True)

    if yes_sent:
        after_yes = cleaned.split("yes")[-1] if "yes" in cleaned else cleaned
        if re.search(r"(Install completed|install completed|100%.*#)", after_yes):
            print("Install completed on device.", flush=True)
            break

    if i % 20 == 0 and i > 0:
        last_bit = cleaned[-200:] if len(cleaned) > 200 else cleaned
        print(f"  ...waiting ({i*3}s) - last output: {last_bit.strip()[-100:]}", flush=True)

print("\nFinal output (last 1000 chars):", flush=True)
print(clean(output)[-1000:], flush=True)

print("\nInstall issued and confirmed. Device will reboot.", flush=True)
print("Waiting 15 minutes for reboot...", flush=True)
try:
    client.close()
except:
    pass

time.sleep(900)

for attempt in range(10):
    print(f"\nReconnect attempt {attempt+1}...", flush=True)
    try:
        remove_host_key(host)
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(host, username=USER, password=PASS,
                       look_for_keys=False, allow_agent=False, timeout=30)
        shell = client.invoke_shell(width=250, height=5000)
        time.sleep(8)
        if shell.recv_ready():
            shell.recv(65535)

        shell.send("show system version | no-more\n")
        time.sleep(10)
        out = ""
        while shell.recv_ready():
            out += shell.recv(65535).decode("utf-8", errors="replace")
        print(clean(out), flush=True)
        print(f"\n=== [{host}] UPGRADE COMPLETE ===", flush=True)
        client.close()
        sys.exit(0)
    except Exception as e:
        print(f"Connection failed: {e}. Waiting 2 minutes...", flush=True)
        time.sleep(120)

print("WARNING: Device did not come back after 30+ min.", flush=True)
sys.exit(1)
