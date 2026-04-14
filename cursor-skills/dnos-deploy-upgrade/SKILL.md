---
name: dnos-deploy-upgrade
description: >-
  Deploy or upgrade a DNOS device using Jenkins build artifacts. Use when the
  user asks to deploy, fresh-deploy, redeploy, upgrade, install, or load
  software on a DNOS machine from a Jenkins build.
---

# DNOS Deploy / Upgrade from Jenkins

## Required Inputs

Collect all of these before starting. If any are missing, ask the user.

| Input | Example | Notes |
|---|---|---|
| **Jenkins build URL** | `https://jenkins.dev.drivenets.net/job/drivenets/job/cheetah/job/feature%252Furpf_strict%252Fv26_2/30/` | The build page URL (with or without trailing slash) |
| **Device** | IP address or hostname | The DNOS machine to operate on |
| **Operation** | `deploy` or `upgrade` | |
| **With config?** (deploy only) | yes / no | Whether to save and restore config across the fresh deploy |

Jenkins artifacts are publicly accessible — no credentials are needed to fetch them.

## Artifact URLs

Three artifact text files are needed. Derive them from the Jenkins build URL by appending `artifact/`:

| Artifact | File |
|---|---|
| BaseOS | `<build-url>/artifact/gi_base_os_artifact.txt` |
| DNOS | `<build-url>/artifact/gi_DNOS_artifact.txt` |
| GI | `<build-url>/artifact/gi_GI_artifact.txt` |

Each text file contains a URL to the actual package. Fetch each file with a simple HTTP GET (no auth needed) and extract the package URL from its content.

## Per-Machine State Isolation (CRITICAL)

All intermediate state files (system info, config backups) **must** be stored in a per-machine directory to prevent parallel deploys from overwriting each other's data:

```
/home/dn/deploy_state/<hostname>/sys_info.json
/home/dn/deploy_state/<hostname>/config_backup.txt
```

**Never** use shared/global file paths like `/home/dn/deploy_sys_info.json`. If two agents deploy different machines concurrently and share a file path, one will overwrite the other's system info, causing a deploy with the wrong system-type and name.

A reusable deploy script exists at `/home/dn/dnos_deploy.py` that handles this automatically:

```bash
python3 /home/dn/dnos_deploy.py <hostname> <baseos_url> <dnos_url> <gi_url> [step]
# Steps: all (default), info, save, delete, load, deploy, restore
```

If writing your own automation instead of using the script, always namespace state files under `deploy_state/<hostname>/`.

## Connecting to the Device

Use paramiko with `invoke_shell` (see `dnos-ssh-connection` skill for full details):

```python
client.connect(host, username='dnroot', password='dnroot',
               look_for_keys=False, allow_agent=False, timeout=30)
shell = client.invoke_shell(width=250, height=5000)
time.sleep(6)
```

### DNOS mode vs GI mode confirmation handling

- **DNOS mode**: `set cli-no-confirm` works to auto-accept `(yes/no)` prompts. Run it once per session.
- **GI mode**: `set cli-no-confirm` does **NOT** work. All commands that prompt `(yes/no)` require you to **explicitly send `yes\n`** after detecting the prompt. Poll the shell output for `yes/no` or `Yes/No` and then send `yes\n`.

## Deploy Workflow (Fresh Install)

### Step 1 — Save system info

```
show system | no-more
```

Parse and save **System Type** and **System Name** from the output. The output format uses comma-separated fields on the same line:

```
System Name: ncpl-nog, System-Id: ce0980c6-...
System Type: SA-64X8C-S, Family: NCR
```

Parse carefully: split the line on `,` first, then extract the value after `:` from the first segment. For example, System Type is `SA-64X8C-S` (not `SA-64X8C-S, Family`).

### Step 2 — Save config (if requested)

**CRITICAL**: `request system delete` wipes the entire device filesystem. Any config file saved on-device will be destroyed. You **must** export the config to the local machine before deleting.

1. Save config on-device:

```
configure
save pre_deploy_backup.txt
exit
```

2. Upload config off the device to the local machine:

```
request file upload config file <local-user>@<local-ip>:<path>/pre_deploy_backup.txt
```

Where `<local-user>` is the SSH user on the machine running the script, `<local-ip>` is its IP reachable from the device, and `<path>` is the destination directory (e.g. `/home/dn/pre_deploy_backup.txt`).

Alternatively, use paramiko SFTP to download the file directly after saving:

```python
sftp = client.open_sftp()
sftp.get('/config/pre_deploy_backup.txt', '/home/dn/pre_deploy_backup.txt')
sftp.close()
```

Only proceed to system delete after confirming the config backup exists locally.

### Step 3 — Delete the system

```
set cli-no-confirm
request system delete
```

Wait for output to indicate deletion is complete. The device will drop into **GI mode** (prompt changes to `GI#`). The SSH session will likely drop — reconnect after ~2-3 minutes.

**Important**: After system delete, the SSH host key changes. Remove the old key before reconnecting:

```bash
ssh-keygen -f ~/.ssh/known_hosts -R <hostname>
```

After reconnecting, confirm you see the `GI#` prompt.

### Step 4 — Load the 3 packages (in GI mode)

**GI mode requires explicit `yes` confirmation** for every `request system target-stack load` command. After sending the command, poll the shell output for `(yes/no)`, then send `yes\n`.

Run each load command one at a time, waiting for each to complete before the next:

```
request system target-stack load <baseos-package-url>
```

→ Wait for `(yes/no)` prompt → send `yes` → wait for `GI#` prompt to return.

```
request system target-stack load <dnos-package-url>
```

→ Wait for `(yes/no)` prompt → send `yes` → wait for `GI#` prompt.

```
request system target-stack load <gi-package-url>
```

→ Wait for `(yes/no)` prompt → send `yes` → wait for `GI#` prompt.

If any load fails, report the error to the user and stop.

Example paramiko pattern for GI mode commands with confirmation:

```python
shell.send(f"request system target-stack load {url}\n")
output = ""
yes_sent = False
for i in range(100):
    time.sleep(3)
    while shell.recv_ready():
        output += shell.recv(65535).decode("utf-8", errors="replace")
    if not yes_sent and "yes/no" in output:
        shell.send("yes\n")
        yes_sent = True
    if yes_sent and "GI#" in output.split("yes")[-1]:
        break
```

### Step 5 — Deploy

Using the system-type and name saved in Step 1:

```
request system deploy system-type <sys-type> name <sys-name> ncc-id 0
```

This also prompts `(yes/no)` in GI mode — send `yes` after detecting the prompt (same pattern as Step 4). On success you will see:

```
Started deployment on NCC 0, task ID = <id>
```

The device will reboot and come up in DNOS mode. This can take 10-15 minutes. The SSH host key will change again — remove the old key before reconnecting:

```bash
ssh-keygen -f ~/.ssh/known_hosts -R <hostname>
```

Reconnect via SSH after the reboot and confirm you see the DNOS prompt (`<hostname>#`).

### Step 6 — Restore config (if saved)

After the device is back up in DNOS mode, upload the config backup from the local machine to the device, then load it:

```
request file download config file <local-user>@<local-ip>:<path>/pre_deploy_backup.txt
```

Or use paramiko SFTP to upload:

```python
sftp = client.open_sftp()
sftp.put('/home/dn/pre_deploy_backup.txt', '/config/pre_deploy_backup.txt')
sftp.close()
```

Then apply it:

```
configure
load override pre_deploy_backup.txt
commit
exit
```

Inform the user that deploy is complete and config has been restored.

## Upgrade Workflow (In-Place)

### Step 1 — Load the 3 packages (in DNOS operational mode)

Connect to the device in DNOS mode (normal `dnroot` login). Run `set cli-no-confirm` first (this works in DNOS mode).

Load each package one at a time:

```
request system target-stack load <baseos-package-url>
```

Wait for completion.

```
request system target-stack load <dnos-package-url>
```

Wait for completion.

```
request system target-stack load <gi-package-url>
```

Wait for completion.

### Step 2 — Install

```
request system target-stack install
```

Wait for the install to proceed. The device will show progress and may reboot. This can take 10-20 minutes. After completion, the device comes back up with the new software.

Reconnect and verify the version:

```
show system version | no-more
```

Inform the user that upgrade is complete.

## Error Handling

- If SSH disconnects during `request system delete`, wait 2-3 minutes and reconnect. The device should be in GI mode. Remember to remove the old SSH host key first.
- If SSH disconnects during `request system deploy`, wait 10-15 minutes and reconnect. The deploy continues in the background. Remove old SSH host key before reconnecting.
- If SSH disconnects during `request system target-stack install`, wait up to 20 minutes and reconnect. The upgrade continues in the background.
- If a `target-stack load` times out or fails with a connection error, retry once. If it fails again, stop and report.
- If the device does not come back after 20 minutes, inform the user and suggest manual console access.
- **SSH host key changes**: After `request system delete` and after `request system deploy`, the device generates new SSH host keys. Always run `ssh-keygen -R <hostname>` before reconnecting.

## Fetching Jenkins Artifact URLs

Use `curl` to download artifact text files (no auth required):

```bash
curl -s '<artifact-txt-url>'
```

The response body is the package URL to pass to `request system target-stack load`.
