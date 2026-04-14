---
name: dnos-techsupport
description: >-
  Collect a tech-support archive from a DNOS device, upload it to MinIO, and
  return the download link. Use when the user asks to collect a techsupport,
  tech-support, TSR, or diagnostic bundle from a DNOS device.
---

# DNOS Tech-Support Collection

Collect a tech-support tar from a DNOS device, SCP it to the MinIO staging
server, and return the MinIO download link.

## Required Inputs

| Input | Example | Notes |
|---|---|---|
| **Device** | hostname or IP | The DNOS device to collect from |
| **Tech-support name** | `my_ts_name` | Passed to `request system tech-support <name>` |

If either is missing, ask the user before proceeding.

## Step 1 — Connect to the Device

Use paramiko (see `dnos-ssh-connection` skill):

```python
import paramiko, time, re

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(host, username='dnroot', password='dnroot',
            look_for_keys=False, allow_agent=False, timeout=30)
shell = ssh.invoke_shell(width=250, height=5000)
time.sleep(6)
shell.recv(65535)
```

If the user provided a device name instead of an IP, resolve it first via
`get_device_management_interfaces` MCP tool and pick the `100.64.x.x` IP.

## Step 2 — Request the Tech-Support

```
request system tech-support <name>
```

### Handle prompts and errors

**Replace existing prompt**: If a previous tech-support file exists, the device
asks whether to replace it. Detect `yes/no` or `replace` in the output and
send `yes\n`.

**Already running**: If another tech-support generation is already in progress,
the device returns `Cannot produce a new techsupport file, another process is
already running`. In this case, skip directly to Step 3 (polling) — the
running generation will complete and you can pick up its tar file.

```python
shell.send(f"request system tech-support {ts_name}\n")
output = ""
yes_sent = False
for _ in range(20):
    time.sleep(3)
    while shell.recv_ready():
        output += shell.recv(65535).decode("utf-8", errors="replace")
    if not yes_sent and re.search(r'(yes/no|replace)', output, re.I):
        shell.send("yes\n")
        yes_sent = True
    if re.search(r'#\s*$', output.split('\n')[-1]):
        break
```

After the command returns, generation runs in the background.

## Step 3 — Poll Until Complete

Run `show system tech-support status` in a loop until the output indicates
completion (look for `completed` or `Available` or a line showing `8/8`
archives collected, or the prompt returning without "running for" text).

```python
for attempt in range(60):
    time.sleep(15)
    shell.send("show system tech-support status\n")
    time.sleep(5)
    out = ""
    while shell.recv_ready():
        out += shell.recv(65535).decode("utf-8", errors="replace")
    out = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', out)
    if 'completed' in out.lower() or 'available' in out.lower():
        break
    if '8/8' in out:
        break
```

**Extract the tar filename** from the status output. It appears on a line like:

```
File name ts_name_05_04_06_14-04-2026.tar
```

Parse it:

```python
m = re.search(r'File name\s+(ts_\S+\.tar)', out)
tar_filename = m.group(1) if m else None
```

If after ~15 minutes it is still not done, inform the user and keep polling.

## Step 4 — Enter Linux Shell and SCP the File

From the DNOS CLI, enter the underlying Linux shell:

```
run start shell
```

The device will prompt for a password. Send `dnroot`.

```python
shell.send("run start shell\n")
time.sleep(3)
out = ""
while shell.recv_ready():
    out += shell.recv(65535).decode("utf-8", errors="replace")
if "Password" in out or "password" in out:
    shell.send("dnroot\n")
    time.sleep(3)
    while shell.recv_ready():
        shell.recv(65535)
```

Now send the SCP command to upload the file to the MinIO staging area:

```python
scp_cmd = (
    'ip netns exec oob_ncc_ns bash -c '
    '"sshpass -p drive1234! scp -o StrictHostKeyChecking=no '
    '-o UserKnownHostsFile=/dev/null '
    '/techsupport/ts_* '
    'dn@100.64.15.247:/ftpdisk/dn/auto_upload_to_minio/" &'
)
shell.send(scp_cmd + "\n")
time.sleep(10)
shell.send("\n")
time.sleep(5)
out = ""
while shell.recv_ready():
    out += shell.recv(65535).decode("utf-8", errors="replace")
```

Wait a few seconds, then press Enter to confirm the background job finishes.
Look for the shell prompt returning cleanly (e.g. `root@` or `$`).

After the SCP finishes, exit back to the DNOS CLI:

```python
shell.send("exit\n")
time.sleep(2)
```

## Step 5 — Return the MinIO Link

The file lands in the MinIO `techsupport` bucket. The download link is:

```
http://minioio.dev.drivenets.net:9000/minio/techsupport/
```

Construct the direct link to the uploaded file:

```
http://minioio.dev.drivenets.net:9000/minio/techsupport/<tar_filename>
```

Present both the bucket listing URL and the direct file link to the user.

If you cannot determine the exact filename, give the user the bucket listing
URL and tell them to look for the most recent `ts_*` file matching their
tech-support name.

## Error Handling

- **SSH disconnects**: Reconnect and re-check `show system tech-support status`.
  The generation continues in the background.
- **SCP fails**: Verify the device has OOB connectivity (`ip netns exec oob_ncc_ns ping 100.64.15.247`).
  Retry once. If it fails again, report to the user.
- **Disk space**: If the status output shows the techsupport partition is nearly full,
  warn the user before proceeding.
- **Timeout**: If generation takes longer than 20 minutes, inform the user — some
  large devices can take up to 30 minutes.

## Quick Reference

| Phase | Command | Where |
|---|---|---|
| Generate | `request system tech-support <name>` | DNOS CLI |
| Poll | `show system tech-support status` | DNOS CLI |
| Enter shell | `run start shell` (password: `dnroot`) | DNOS CLI |
| SCP upload | `ip netns exec oob_ncc_ns bash -c "sshpass -p drive1234! scp ..."` | Linux shell |
| MinIO URL | `http://minioio.dev.drivenets.net:9000/minio/techsupport/` | Browser |
