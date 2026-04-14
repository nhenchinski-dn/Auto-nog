#!/usr/bin/env python3
"""DNOS in-place upgrade script."""
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


def connect(host):
    remove_host_key(host)
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, username=USER, password=PASS,
                   look_for_keys=False, allow_agent=False, timeout=30)
    shell = client.invoke_shell(width=250, height=5000)
    time.sleep(8)
    if shell.recv_ready():
        shell.recv(65535)
    return client, shell


def send_cmd(shell, cmd, wait=5, max_wait=None, expect=None):
    shell.send(cmd + "\n")
    output = ""
    elapsed = 0
    interval = 3
    deadline = max_wait if max_wait else wait
    while elapsed < deadline:
        time.sleep(min(interval, deadline - elapsed))
        elapsed += min(interval, deadline - elapsed)
        while shell.recv_ready():
            output += shell.recv(65535).decode("utf-8", errors="replace")
        if expect and re.search(expect, clean(output)):
            break
    return clean(output)


if __name__ == "__main__":
    if len(sys.argv) < 5:
        print("Usage: python3 dnos_upgrade.py <hostname> <baseos_url> <dnos_url> <gi_url>")
        sys.exit(1)

    host = sys.argv[1]
    baseos_url = sys.argv[2]
    dnos_url = sys.argv[3]
    gi_url = sys.argv[4]

    print(f"=== DNOS Upgrade: {host} ===", flush=True)
    print(f"Packages: BaseOS={baseos_url.split('/')[-1]}", flush=True)
    print(f"          DNOS={dnos_url.split('/')[-1]}", flush=True)
    print(f"          GI={gi_url.split('/')[-1]}", flush=True)

    # Step 1: Connect and set cli-no-confirm
    print(f"\n=== Connecting to {host} ===", flush=True)
    client, shell = connect(host)
    send_cmd(shell, "set cli-no-confirm", wait=3)
    print("cli-no-confirm set.", flush=True)

    # Step 2: Load packages
    packages = [
        ("BaseOS", baseos_url),
        ("DNOS", dnos_url),
        ("GI", gi_url),
    ]
    for name, url in packages:
        print(f"\n--- Loading {name}: {url.split('/')[-1]} ---", flush=True)
        out = send_cmd(shell, f"request system target-stack load {url}",
                       wait=30, max_wait=300, expect=r"(#|Error|error|failed)")
        print(out[-800:], flush=True)
        if re.search(r"(Error|error|failed|Failed)", clean(out)):
            print(f"!!! {name} load FAILED !!!", flush=True)
            client.close()
            sys.exit(1)
        print(f"--- {name} loaded ---", flush=True)

    print("\n=== All packages loaded ===", flush=True)

    # Step 3: Install (requires explicit yes — cli-no-confirm does NOT suppress this prompt)
    print("\n=== Running target-stack install ===", flush=True)
    shell.send("request system target-stack install\n")
    output = ""
    yes_sent = False
    install_confirmed = False
    for i in range(200):
        time.sleep(3)
        while shell.recv_ready():
            output += shell.recv(65535).decode("utf-8", errors="replace")
        cleaned = clean(output)
        if not yes_sent and ("yes/no" in cleaned or "Yes/No" in cleaned):
            time.sleep(1)
            shell.send("yes\n")
            yes_sent = True
            print("  -> Sent 'yes' to confirmation prompt", flush=True)
        if yes_sent and "Started target stack installation" in cleaned:
            install_confirmed = True
            print("  -> Install started", flush=True)
            break
        if i % 20 == 0 and i > 0:
            print(f"  ...waiting ({i*3}s)", flush=True)

    print(clean(output)[-500:], flush=True)

    if not install_confirmed:
        print("WARNING: Did not see install confirmation.", flush=True)

    print("Install issued. Device will reboot. Waiting 15 minutes...", flush=True)
    try:
        client.close()
    except:
        pass

    time.sleep(900)

    # Step 4: Reconnect and verify
    for attempt in range(10):
        print(f"\nReconnect attempt {attempt+1}...", flush=True)
        try:
            client, shell = connect(host)
            out = send_cmd(shell, "show system version | no-more", wait=10,
                           max_wait=20, expect=r"(Version|version)")
            print(out, flush=True)
            print(f"\n=== [{host}] UPGRADE COMPLETE ===", flush=True)
            client.close()
            sys.exit(0)
        except Exception as e:
            print(f"Connection failed: {e}. Waiting 2 minutes...", flush=True)
            time.sleep(120)

    print("WARNING: Device did not come back after 30+ min.", flush=True)
    sys.exit(1)
