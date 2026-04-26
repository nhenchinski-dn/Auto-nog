#!/usr/bin/env python3
"""Collect tech-support from wky1c7vd00008p2 for uRPF allow-default bug."""

import paramiko, time, re

DUT_IP = '100.64.8.59'
TS_NAME = 'urpf_allow_default_bug'

def clean(text):
    text = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)
    text = re.sub(r'\r', '', text)
    return text.strip()

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(DUT_IP, username='dnroot', password='dnroot', timeout=30,
            look_for_keys=False, allow_agent=False)
shell = ssh.invoke_shell(width=250, height=5000)
time.sleep(6)
shell.recv(65535)

# Step 1: Request tech-support
print(f"Requesting tech-support: {TS_NAME}")
shell.send(f"request system tech-support {TS_NAME}\n")
output = ""
yes_sent = False
for _ in range(20):
    time.sleep(3)
    while shell.recv_ready():
        output += shell.recv(65535).decode("utf-8", errors="replace")
    if not yes_sent and re.search(r'(yes/no|replace)', output, re.I):
        shell.send("yes\n")
        yes_sent = True
        print("  Sent 'yes' to replace prompt")
    if re.search(r'#\s*$', output.split('\n')[-1]):
        break
print("  Request sent, generation running in background...")

# Step 2: Poll until complete
print("Polling for completion...")
tar_filename = None
for attempt in range(60):
    time.sleep(15)
    shell.send("show system tech-support status\n")
    time.sleep(5)
    out = ""
    while shell.recv_ready():
        out += shell.recv(65535).decode("utf-8", errors="replace")
    out = clean(out)
    
    m = re.search(r'File name\s+(ts_\S+\.tar)', out)
    if m:
        tar_filename = m.group(1)

    if 'completed' in out.lower() or 'available' in out.lower() or '8/8' in out:
        print(f"  Tech-support completed! File: {tar_filename}")
        break
    
    progress = re.search(r'(\d+/\d+)', out)
    if progress:
        print(f"  Progress: {progress.group(1)} (attempt {attempt+1})")
    else:
        print(f"  Waiting... (attempt {attempt+1})")
else:
    print("  WARNING: Timed out after 15 minutes")

# Step 3: SCP to MinIO staging
print("\nEntering Linux shell for SCP upload...")
shell.send("run start shell\n")
time.sleep(3)
out = ""
while shell.recv_ready():
    out += shell.recv(65535).decode("utf-8", errors="replace")
if "assword" in out:
    shell.send("dnroot\n")
    time.sleep(3)
    while shell.recv_ready():
        shell.recv(65535)

scp_cmd = (
    'ip netns exec oob_ncc_ns bash -c '
    '"sshpass -p drive1234! scp -o StrictHostKeyChecking=no '
    '-o UserKnownHostsFile=/dev/null '
    '/techsupport/ts_* '
    'dn@100.64.15.247:/ftpdisk/dn/auto_upload_to_minio/" &'
)
print(f"Uploading via SCP...")
shell.send(scp_cmd + "\n")
time.sleep(15)
shell.send("\n")
time.sleep(10)
out = ""
while shell.recv_ready():
    out += shell.recv(65535).decode("utf-8", errors="replace")
print(f"  SCP output: {clean(out)[-200:]}")

shell.send("exit\n")
time.sleep(2)

ssh.close()

minio_url = f"http://minioio.dev.drivenets.net:9000/minio/techsupport/{tar_filename}" if tar_filename else "http://minioio.dev.drivenets.net:9000/minio/techsupport/"
print(f"\nTech-support link: {minio_url}")
print("Done.")
