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
| **Jenkins build URL** | `https://jenkins.dev.drivenets.net/job/drivenets/job/cheetah/job/feature%252Furpf_strict%252Fv26_2/28/artifact/` | Must end at the build's artifact path |
| **Jenkins credentials** | user + API token | Only ask once per session; reuse if already provided |
| **Device** | IP address or hostname | The DNOS machine to operate on |
| **Operation** | `deploy` or `upgrade` | |
| **With config?** (deploy only) | yes / no | Whether to save and restore config across the fresh deploy |

## Artifact URLs

Three artifact text files are needed. Derive them from the Jenkins build URL:

| Artifact | File |
|---|---|
| BaseOS | `<build-url>/gi_base_os_artifact.txt` |
| DNOS | `<build-url>/gi_DNOS_artifact.txt` |
| GI | `<build-url>/gi_GI_artifact.txt` |

Each text file contains a URL to the actual package. Fetch each file (HTTP GET with Basic auth using the Jenkins credentials) and extract the package URL from its content.

## Connecting to the Device

Use paramiko with `invoke_shell` (see `dnos-ssh-connection` skill for full details):

```python
client.connect(host, username='dnroot', password='dnroot',
               look_for_keys=False, allow_agent=False, timeout=30)
shell = client.invoke_shell(width=250, height=5000)
time.sleep(6)
```

For all commands that prompt `(Yes/No)`, first run `set cli-no-confirm` in the session to auto-accept confirmation prompts. This is session-scoped and does not persist.

## Deploy Workflow (Fresh Install)

### Step 1 — Save system info

```
show system | no-more
```

Parse and save **System Type** (e.g. `SA-40C`, `CL-96`) and **System Name** from the output. You will need both for the deploy command later.

### Step 2 — Save config (if requested)

If the user wants to preserve config:

```
configure
save pre_deploy_backup.txt
exit
```

### Step 3 — Delete the system

```
set cli-no-confirm
request system delete
```

Wait for output to indicate deletion is complete. The device will drop into **GI mode** (prompt changes to `gi#`). The SSH session will likely drop — reconnect after ~2-3 minutes.

After reconnecting, confirm you see the `gi#` prompt.

### Step 4 — Load the 3 packages (in GI mode)

Run each load command one at a time, waiting for each to complete before the next:

```
request system target-stack load <baseos-package-url>
```

Wait for `Added BaseOS version ... to target stack.` or similar completion message.

```
request system target-stack load <dnos-package-url>
```

Wait for completion.

```
request system target-stack load <gi-package-url>
```

Wait for completion.

If any load fails, report the error to the user and stop.

### Step 5 — Deploy

Using the system-type and name saved in Step 1:

```
request system deploy system-type <sys-type> name <sys-name> ncc-id 0
```

Wait for the deployment to complete. The device will reboot and come up in DNOS mode. This can take 5-15 minutes. Reconnect via SSH after the reboot.

### Step 6 — Restore config (if saved)

After the device is back up in DNOS mode:

```
configure
load override pre_deploy_backup.txt
commit
exit
```

Inform the user that deploy is complete and config has been restored.

## Upgrade Workflow (In-Place)

### Step 1 — Load the 3 packages (in DNOS operational mode)

Connect to the device in DNOS mode (normal `dnroot` login). Run `set cli-no-confirm` first.

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

- If SSH disconnects during `request system delete`, wait 2-3 minutes and reconnect. The device should be in GI mode.
- If SSH disconnects during `request system target-stack install`, wait up to 20 minutes and reconnect. The upgrade continues in the background.
- If a `target-stack load` times out or fails with a connection error, retry once. If it fails again, stop and report.
- If the device does not come back after 20 minutes, inform the user and suggest manual console access.

## Fetching Jenkins Artifact URLs

Use `curl` with Basic auth to download artifact text files:

```bash
curl -u '<jenkins-user>:<api-token>' '<artifact-txt-url>'
```

The response body is the package URL to pass to `request system target-stack load`.
