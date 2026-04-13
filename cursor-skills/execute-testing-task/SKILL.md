---
name: execute-testing-task
description: >-
  Execute a Jira testing task on a DNOS device and append Test Results to the
  ticket. Use when the user asks to run, execute, or perform a testing task
  on a device and record the results back to Jira.
---

# Execute Jira Testing Task

When the user provides a Jira testing task ticket (e.g. ART-XXXX or SW-XXXXXX) and a target device (IP address or device name), follow this workflow to execute the test and record results.

## Required Inputs

1. **Jira ticket key** (e.g. `ART-8025`, `SW-241841`)
2. **Device** — an IP address (e.g. `100.64.3.239`) or a device name from the network mapper

## Step 1 — Fetch the Jira Ticket

Use `getJiraIssue` (or `atlassian_jira_get_issue`) to fetch the full ticket. Extract:
- **Summary** (title)
- **Status**
- **Description** — the full description body
- **Test Steps** — from `customfield_11772` if present, otherwise parse description

## Step 2 — Parse Test Steps

Identify every test step. Steps typically follow:
- Ordered `#` list items (Jira wiki)
- Sections with headers: `h3. Step 1`
- Tables with step/action/expected columns
- CLI commands in `{code}`, `{noformat}`, or backtick-fenced blocks

For each step, extract:
- **Step number/name**
- **Action description**
- **CLI command(s)** to execute
- **Expected result**

Present the parsed steps to the user for confirmation before executing.

## Step 3 — Resolve Device Connectivity

If the user provided a **device name** (not an IP):
1. Use `get_device_management_interfaces` MCP tool to find management IPs
2. Pick the `100.64.x.x` management IP
3. Confirm connectivity

If the user provided an **IP address**, use it directly.

Default credentials: `dnroot` / `dnroot`.

## Step 4 — Execute Test Steps on the Device

Use MCP device tools (`run_show_commands`, `shell_run_commands`) when available. If unavailable, fall back to paramiko:

```python
import paramiko, time

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('<IP>', username='dnroot', password='dnroot',
            timeout=30, look_for_keys=False, allow_agent=False)
chan = ssh.invoke_shell(width=300)
time.sleep(5)
chan.recv(65535)

def run(cmd, wait=8):
    chan.send(cmd + '\n')
    time.sleep(wait)
    out = b''
    while chan.recv_ready():
        out += chan.recv(65535)
    return out.decode(errors='replace')
```

For each step:
1. Print which step is being executed
2. Run the CLI command(s)
3. Capture the full output
4. Compare against expected result if specified
5. Determine PASS/FAIL

Append `| no-more` to show commands to prevent paging.

## Step 5 — Build the Test Results Section

Construct **Test Results** in Jira wiki markup:

```
h2. Test Results

*Execution Date:* <YYYY-MM-DD HH:MM>
*Device:* <device name or IP>
*Tester:* AI-assisted execution
*Software Version:* <from "show system version" output>
*Overall Result:* PASS / FAIL / PARTIAL

h3. Step 1: <step name>
*Result:* PASS / FAIL
*Command:* {{<cli command>}}
{code:title=Output}
<actual CLI output, cleaned of ANSI escape codes>
{code}
*Expected:* <expected result from test step>
*Analysis:* <brief comparison of actual vs expected>

----

h3. Summary
|| Step || Result || Notes ||
| Step 1 — <name> | (/) PASS | <one-line note> |
| Step 2 — <name> | (x) FAIL | <one-line note> |
```

Jira macros: `(/)` PASS, `(x)` FAIL, `(!)` WARNING, `{code}...{code}` CLI output, `{{monospace}}` inline commands.

## Step 6 — Present Results and Update the Ticket

1. **Show the full Test Results** to the user for review
2. **Ask for explicit approval** before updating the Jira ticket (required by workspace rules)
3. After approval, update using `editJiraIssue` / `atlassian_jira_update_issue`, appending (never overwriting) description

If the user prefers, add results as a **comment** instead.

## Important Rules

- **Always run `show system version | no-more` first** to capture software version
- **Never modify device configuration** without explicit user approval — only execute show commands autonomously
- **Clean ANSI escape codes** from all captured output
- **If a step fails, continue** with remaining steps unless blocking dependency
- **If a command hangs** (no output for 30+ seconds), note as TIMEOUT and move on
- **Preserve the original ticket description** — only append Test Results
- **Never update the Jira ticket without user approval** (per workspace rules)
