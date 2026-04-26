#!/usr/bin/env python3
"""Upgrade wky1c7vd00008p2: load 3 packages then install."""
import paramiko
import time
import re
import sys
import os

USER = "dnroot"
PASS = "dnroot"
HOST = "wky1c7vd00008p2"

BASEOS_URL = "http://minio-ssd-il.dev.drivenets.net:9000/dnpkg-48hrs/drivenets_baseos_2.2620267025.tar"
DNOS_URL = "http://minio-ssd-il.dev.drivenets.net:9000/dnpkg-48hrs/drivenets_dnos_26.2.0.32_priv.feature_urpf_strict_v26_2_32.tar"
GI_URL = "http://minio-ssd-il.dev.drivenets.net:9000/dnpkg-48hrs/drivenets_gi_26.2.0.32_priv.feature_urpf_strict_v26_2_32.tar"

PACKAGES = [
    ("BaseOS", BASEOS_URL),
    ("DNOS", DNOS_URL),
    ("GI", GI_URL),
]


def clean(text):
    text = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)
    text = re.sub(r'\r', '', text)
    text = re.sub(r'-- More -- \(Press q to quit\)\s*', '', text)
    return text


def remove_host_key(host):
    os.system(f'ssh-keygen -f /home/dn/.ssh/known_hosts -R {host} 2>/dev/null')


def read_shell(shell, timeout=5):
    output = ""
    end = time.time() + timeout
    while time.time() < end:
        time.sleep(0.5)
        while shell.recv_ready():
            output += shell.recv(65535).decode("utf-8", errors="replace")
    return output


def send_and_wait(shell, cmd, wait_for, timeout=600, label=""):
    """Send a command and wait for a pattern in the output."""
    print(f"  >> {cmd.strip()}", flush=True)
    shell.send(cmd)
    output = ""
    start = time.time()
    yes_sent = False

    for i in range(timeout // 3):
        time.sleep(3)
        while shell.recv_ready():
            output += shell.recv(65535).decode("utf-8", errors="replace")
        cleaned = clean(output)

        if not yes_sent and ("yes/no" in cleaned.lower()):
            time.sleep(1)
            shell.send("yes\n")
            yes_sent = True
            print(f"  -> Sent 'yes' to confirmation prompt", flush=True)

        if re.search(wait_for, cleaned):
            elapsed = time.time() - start
            print(f"  -> Done ({elapsed:.0f}s)", flush=True)
            return cleaned

        if i > 0 and i % 20 == 0:
            elapsed = time.time() - start
            tail = cleaned[-200:].strip()[-100:]
            print(f"  ...waiting ({elapsed:.0f}s) - {tail}", flush=True)

    elapsed = time.time() - start
    print(f"  -> Timed out after {elapsed:.0f}s", flush=True)
    print(f"  Last output: {clean(output)[-500:]}", flush=True)
    return clean(output)


print(f"=== Upgrading {HOST} ===", flush=True)
print(f"  BaseOS: {BASEOS_URL}", flush=True)
print(f"  DNOS:   {DNOS_URL}", flush=True)
print(f"  GI:     {GI_URL}", flush=True)

# --- Connect ---
remove_host_key(HOST)
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(HOST, username=USER, password=PASS,
               look_for_keys=False, allow_agent=False, timeout=30)
shell = client.invoke_shell(width=250, height=5000)
time.sleep(8)
if shell.recv_ready():
    boot_output = shell.recv(65535).decode("utf-8", errors="replace")
    print(f"Connected. Prompt: {clean(boot_output).strip()[-80:]}", flush=True)
else:
    print("Connected.", flush=True)

shell.send("set cli-no-confirm\n")
time.sleep(2)
if shell.recv_ready():
    shell.recv(65535)

# --- Step 1: Load packages ---
print("\n=== Step 1: Loading packages ===", flush=True)
for name, url in PACKAGES:
    print(f"\n--- Loading {name} ---", flush=True)
    result = send_and_wait(
        shell,
        f"request system target-stack load {url}\n",
        r"(#\s*$|Load completed|load completed|Successfully)",
        timeout=600,
        label=name,
    )
    if "error" in result.lower() or "fail" in result.lower():
        print(f"ERROR loading {name}! Output:\n{result[-500:]}", flush=True)
        client.close()
        sys.exit(1)
    print(f"  {name} loaded successfully.", flush=True)

# --- Step 2: Install ---
print("\n=== Step 2: Installing target-stack ===", flush=True)
shell.send("request system target-stack install\n")
output = ""
yes_sent = False

for i in range(600):
    time.sleep(3)
    while shell.recv_ready():
        output += shell.recv(65535).decode("utf-8", errors="replace")
    cleaned = clean(output)

    if not yes_sent and ("yes/no" in cleaned.lower()):
        time.sleep(1)
        shell.send("yes\n")
        yes_sent = True
        print("  -> Sent 'yes' to confirmation prompt", flush=True)

    if yes_sent and "Started target stack installation" in cleaned:
        print("  -> Install started!", flush=True)
        break

    if i % 20 == 0 and i > 0:
        tail = cleaned[-200:].strip()[-100:]
        print(f"  ...waiting ({i*3}s) - {tail}", flush=True)

print("\nInstall output (last 1000 chars):", flush=True)
print(clean(output)[-1000:], flush=True)

print("\nClosing SSH. Device will reboot.", flush=True)
try:
    client.close()
except Exception:
    pass

# --- Step 3: Wait for reboot ---
print("\nWaiting 15 minutes for reboot...", flush=True)
time.sleep(900)

# --- Step 4: Reconnect and verify ---
print("\n=== Step 3: Verifying upgrade ===", flush=True)
for attempt in range(10):
    print(f"\nReconnect attempt {attempt + 1}/10...", flush=True)
    try:
        remove_host_key(HOST)
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(HOST, username=USER, password=PASS,
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
        print(f"\n=== [{HOST}] UPGRADE COMPLETE ===", flush=True)
        client.close()
        sys.exit(0)
    except Exception as e:
        print(f"Connection failed: {e}. Waiting 2 minutes...", flush=True)
        time.sleep(120)

print(f"WARNING: {HOST} did not come back after 30+ min.", flush=True)
sys.exit(1)
