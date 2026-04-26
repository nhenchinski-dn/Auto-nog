#!/usr/bin/env python3
import paramiko
import time
import re
import sys
import json
import os

HOST = "ybw1f7vb00010p1"
USER = "dnroot"
PASS = "dnroot"

BASEOS_URL = "http://minio-ssd-il.dev.drivenets.net:9000/dnpkg-48hrs/drivenets_baseos_2.2620006080.tar"
DNOS_URL = "http://minio-ssd-il.dev.drivenets.net:9000/dnpkg-48hrs/drivenets_dnos_26.2.0.284_dev.dev_v26_2_777.tar"
GI_URL = "http://minio-ssd-il.dev.drivenets.net:9000/dnpkg-48hrs/drivenets_gi_26.2.0.284_dev.dev_v26_2_777.tar"

LOCAL_BACKUP = "/home/dn/pre_deploy_backup.txt"
SYS_INFO_FILE = "/home/dn/deploy_sys_info.json"

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

def send_cmd_with_yes(shell, cmd, timeout=300):
    """Send a command in GI mode, wait for (yes/no) prompt, send yes, wait for GI# prompt."""
    shell.send(cmd + "\n")
    output = ""
    yes_sent = False
    for i in range(timeout // 3):
        time.sleep(3)
        while shell.recv_ready():
            output += shell.recv(65535).decode("utf-8", errors="replace")
        cleaned = clean(output)
        if not yes_sent and ("yes/no" in cleaned or "Yes/No" in cleaned):
            time.sleep(1)
            shell.send("yes\n")
            yes_sent = True
            print("  -> Sent yes", flush=True)
        if yes_sent:
            after_yes = cleaned.split("yes")[-1] if "yes" in cleaned else cleaned
            if "GI#" in after_yes or "GI(" in after_yes:
                break
        if i % 10 == 0 and i > 0:
            print(f"  ...waiting ({i*3}s)", flush=True)
    return clean(output)


def step_info():
    print("=== Step 1: Connecting and getting system info ===", flush=True)
    client, shell = connect(HOST)

    out = send_cmd(shell, "show system | no-more", wait=10)
    print(out, flush=True)

    sys_type = None
    sys_name = None
    for line in out.split("\n"):
        if "System Type" in line:
            sys_type = line.split(":")[1].split(",")[0].strip()
        if "System Name" in line:
            sys_name = line.split(":")[1].split(",")[0].strip()

    print(f"\n>>> System Type: {sys_type}", flush=True)
    print(f">>> System Name: {sys_name}", flush=True)

    with open(SYS_INFO_FILE, "w") as f:
        json.dump({"system_type": sys_type, "system_name": sys_name}, f)

    client.close()
    return sys_type, sys_name


def step_save_config():
    print("\n=== Step 2: Saving config and exporting to local machine ===", flush=True)
    client, shell = connect(HOST)

    out = send_cmd(shell, "configure", wait=5, expect=r"cfg")
    print(out, flush=True)

    out = send_cmd(shell, "save pre_deploy_backup.txt", wait=10, max_wait=30, expect=r"(Saved|saved|written|cfg)")
    print(out, flush=True)

    out = send_cmd(shell, "exit", wait=3)
    print(out, flush=True)

    print("Downloading config via SFTP ...", flush=True)
    try:
        sftp = client.open_sftp()
        sftp.get('/config/pre_deploy_backup.txt', LOCAL_BACKUP)
        sftp.close()
        file_size = os.path.getsize(LOCAL_BACKUP)
        print(f"Config saved locally to {LOCAL_BACKUP} ({file_size} bytes via SFTP)", flush=True)
    except Exception as e:
        print(f"SFTP download failed: {e}", flush=True)
        print("Falling back to show config capture...", flush=True)
        out = send_cmd(shell, "show config | no-more", wait=10, max_wait=60, expect=r"#\s*$")
        lines = out.strip().split("\n")
        config_lines = []
        capture = False
        for line in lines:
            if line.strip().startswith("system") or capture:
                capture = True
                if line.strip().endswith("#"):
                    break
                config_lines.append(line)
        with open(LOCAL_BACKUP, "w") as f:
            f.write("\n".join(config_lines) + "\n")
        print(f"Config saved locally to {LOCAL_BACKUP} ({len(config_lines)} lines via show config)", flush=True)

    client.close()


def step_delete():
    print("\n=== Step 3: Deleting system ===", flush=True)
    client, shell = connect(HOST)

    send_cmd(shell, "set cli-no-confirm", wait=2)
    out = send_cmd(shell, "request system delete", wait=10)
    print(out, flush=True)

    print("System delete issued. Waiting 3 minutes for GI mode...", flush=True)
    try:
        client.close()
    except:
        pass

    time.sleep(180)

    print("Reconnecting to verify GI mode...", flush=True)
    client, shell = connect(HOST)
    drain = send_cmd(shell, "", wait=3)
    prompt = drain.strip()[-60:]
    print(f"Prompt: {prompt}", flush=True)

    if "GI" in prompt:
        print("Device is in GI mode.", flush=True)
    else:
        print(f"WARNING: Unexpected prompt: {prompt}", flush=True)

    client.close()


def step_load():
    print("\n=== Step 4: Loading packages in GI mode ===", flush=True)
    client, shell = connect(HOST)

    packages = [
        ("BaseOS", BASEOS_URL),
        ("DNOS", DNOS_URL),
        ("GI", GI_URL),
    ]
    for name, url in packages:
        print(f"\n--- Loading {name} ---", flush=True)
        out = send_cmd_with_yes(shell, f"request system target-stack load {url}", timeout=300)
        print(out[-500:], flush=True)
        if re.search(r"(Error|error|failed|Failed)", clean(out)):
            print(f"!!! {name} load FAILED !!!", flush=True)
            client.close()
            sys.exit(1)
        print(f"--- {name} loaded ---", flush=True)

    print("\n=== All packages loaded ===", flush=True)
    client.close()


def step_deploy():
    with open(SYS_INFO_FILE) as f:
        info = json.load(f)
    sys_type = info["system_type"]
    sys_name = info["system_name"]

    print(f"\n=== Step 5: Deploying type={sys_type} name={sys_name} ===", flush=True)
    client, shell = connect(HOST)

    cmd = f"request system deploy system-type {sys_type} name {sys_name} ncc-id 0"
    print(f"Running: {cmd}", flush=True)
    out = send_cmd_with_yes(shell, cmd, timeout=120)
    print(out, flush=True)

    if "Started deployment" in out:
        print("Deploy started successfully.", flush=True)
    else:
        print("WARNING: Did not see 'Started deployment' confirmation.", flush=True)

    print("Device will reboot. Waiting 12 minutes...", flush=True)
    try:
        client.close()
    except:
        pass

    time.sleep(720)

    for attempt in range(6):
        print(f"Reconnect attempt {attempt+1}...", flush=True)
        try:
            client, shell = connect(HOST)
            drain = send_cmd(shell, "", wait=5)
            prompt = drain.strip()[-80:]
            print(f"Prompt: {prompt}", flush=True)
            if "GI" not in prompt:
                print("Device is in DNOS mode.", flush=True)
                client.close()
                return
            else:
                print("Still in GI mode, waiting 2 more minutes...", flush=True)
                client.close()
                time.sleep(120)
        except Exception as e:
            print(f"Connection failed: {e}. Waiting 2 minutes...", flush=True)
            time.sleep(120)

    print("WARNING: Device did not come back in DNOS mode after 20+ min.", flush=True)


def step_restore():
    print("\n=== Step 6: Restoring config ===", flush=True)

    if not os.path.exists(LOCAL_BACKUP):
        print(f"ERROR: Local backup not found at {LOCAL_BACKUP}", flush=True)
        sys.exit(1)

    with open(LOCAL_BACKUP, "r") as f:
        config_text = f.read()

    print(f"Config backup: {len(config_text)} bytes, uploading via SFTP...", flush=True)

    client, shell = connect(HOST)

    try:
        sftp = client.open_sftp()
        sftp.put(LOCAL_BACKUP, '/config/pre_deploy_backup.txt')
        sftp.close()
        print("Config uploaded via SFTP.", flush=True)
    except Exception as e:
        print(f"SFTP upload failed: {e}, falling back to line-by-line...", flush=True)
        out = send_cmd(shell, "configure", wait=5, expect=r"cfg")
        print(out, flush=True)
        for line in config_text.strip().split("\n"):
            line = line.rstrip()
            if line:
                shell.send(line + "\n")
                time.sleep(0.3)
        time.sleep(5)
        while shell.recv_ready():
            shell.recv(65535)
        out = send_cmd(shell, "commit", wait=15, max_wait=300, expect=r"(Commit complete|committed|Error|error)")
        print(out, flush=True)
        out = send_cmd(shell, "exit", wait=3)
        print(out, flush=True)
        print("\nVerifying version...", flush=True)
        out = send_cmd(shell, "show system version | no-more", wait=10, expect=r"Version")
        print(out, flush=True)
        print("\n=== DEPLOY COMPLETE. CONFIG RESTORED. ===", flush=True)
        client.close()
        return

    out = send_cmd(shell, "configure", wait=5, expect=r"cfg")
    print(out, flush=True)

    out = send_cmd(shell, "load override pre_deploy_backup.txt", wait=15, max_wait=120, expect=r"(cfg|error|Error)")
    print(out, flush=True)

    out = send_cmd(shell, "commit", wait=15, max_wait=300, expect=r"(Commit complete|committed|Error|error)")
    print(out, flush=True)

    out = send_cmd(shell, "exit", wait=3)
    print(out, flush=True)

    print("\nVerifying version...", flush=True)
    out = send_cmd(shell, "show system version | no-more", wait=10, expect=r"Version")
    print(out, flush=True)

    print("\n=== DEPLOY COMPLETE. CONFIG RESTORED. ===", flush=True)
    client.close()


if __name__ == "__main__":
    step = sys.argv[1] if len(sys.argv) > 1 else "all"

    if step == "all":
        step_info()
        step_save_config()
        step_delete()
        step_load()
        step_deploy()
        step_restore()
    elif step == "info":
        step_info()
    elif step == "save":
        step_save_config()
    elif step == "delete":
        step_delete()
    elif step == "load":
        step_load()
    elif step == "deploy":
        step_deploy()
    elif step == "restore":
        step_restore()
    else:
        print(f"Unknown step: {step}")
        sys.exit(1)
