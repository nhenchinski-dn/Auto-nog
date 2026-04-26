import paramiko
import time
import re
import sys
import functools

print = functools.partial(print, flush=True)

HOST = "100.64.8.59"
TS_NAME = "test"
USERNAME = "dnroot"
PASSWORD = "dnroot"

def clean(text):
    text = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)
    text = re.sub(r'\r', '', text)
    text = re.sub(r'-- More -- \(Press q to quit\)\s*', '', text)
    return text

def recv_all(shell, timeout=5):
    output = ""
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.5)
        while shell.recv_ready():
            output += shell.recv(65535).decode("utf-8", errors="replace")
    return clean(output)

def send_cmd(shell, cmd, wait=8):
    shell.send(cmd + "\n")
    return recv_all(shell, timeout=wait)

print(f"[1/5] Connecting to {HOST}...")
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USERNAME, password=PASSWORD,
            look_for_keys=False, allow_agent=False, timeout=30)
shell = ssh.invoke_shell(width=250, height=5000)
time.sleep(6)
shell.recv(65535)
print("Connected.")

print(f"\n[2/5] Requesting tech-support '{TS_NAME}'...")
shell.send(f"request system tech-support {TS_NAME}\n")
output = ""
yes_sent = False
for _ in range(30):
    time.sleep(3)
    while shell.recv_ready():
        output += shell.recv(65535).decode("utf-8", errors="replace")
    cleaned = clean(output)
    if not yes_sent and re.search(r'(yes/no|replace|Yes/No)', cleaned):
        print("  -> Existing tech-support found, sending 'yes' to replace...")
        shell.send("yes\n")
        yes_sent = True
    if re.search(r'#\s*$', cleaned.rstrip()):
        if 'tech-support' in cleaned.lower() or yes_sent or 'started' in cleaned.lower():
            break
print("  Tech-support request submitted.")
print("  Output:", clean(output).strip()[-300:])

print(f"\n[3/5] Polling tech-support status...")
tar_filename = None
for attempt in range(80):
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

    progress_m = re.search(r'(\d+)/(\d+)\s+archives collected', out)
    if progress_m:
        print(f"  Progress: {progress_m.group(1)}/{progress_m.group(2)} archives collected (attempt {attempt+1})")

    if 'completed' in out.lower() or 'available' in out.lower():
        print("  Tech-support generation COMPLETE.")
        break
    if re.search(r'8/8', out):
        print("  All 8/8 archives collected.")
        break
    if 'no tech-support' in out.lower() and tar_filename:
        print("  Tech-support generation finished.")
        break
else:
    print("  WARNING: Timed out after 20 minutes. Check manually.")
    sys.exit(1)

if tar_filename:
    print(f"  Tar filename: {tar_filename}")
else:
    shell.send("show system tech-support status\n")
    time.sleep(5)
    final = ""
    while shell.recv_ready():
        final += shell.recv(65535).decode("utf-8", errors="replace")
    final = clean(final)
    m = re.search(r'File name\s+(ts_\S+\.tar)', final)
    if m:
        tar_filename = m.group(1)
        print(f"  Tar filename: {tar_filename}")
    else:
        m2 = re.search(r'(ts_\S+\.tar)', final)
        if m2:
            tar_filename = m2.group(1)
            print(f"  Tar filename (fallback): {tar_filename}")
        else:
            print("  WARNING: Could not extract tar filename from status output.")
            print("  Last output:", final[-500:])

print(f"\n[4/5] Entering shell and uploading via SCP...")
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

time.sleep(2)
scp_cmd = (
    'ip netns exec oob_ncc_ns bash -c '
    '"sshpass -p drive1234! scp -o StrictHostKeyChecking=no '
    '-o UserKnownHostsFile=/dev/null '
    '/techsupport/ts_* '
    'dn@100.64.15.247:/ftpdisk/dn/auto_upload_to_minio/" &'
)
print(f"  Running SCP...")
shell.send(scp_cmd + "\n")
time.sleep(15)
shell.send("\n")
time.sleep(10)
out = ""
while shell.recv_ready():
    out += shell.recv(65535).decode("utf-8", errors="replace")
print("  SCP output:", clean(out).strip()[-300:])

shell.send("exit\n")
time.sleep(2)

print(f"\n[5/5] Done!")
print(f"  MinIO bucket: http://minioio.dev.drivenets.net:9000/minio/techsupport/")
if tar_filename:
    print(f"  Direct link:  http://minioio.dev.drivenets.net:9000/minio/techsupport/{tar_filename}")

ssh.close()
print("\nTech-support collection complete.")
