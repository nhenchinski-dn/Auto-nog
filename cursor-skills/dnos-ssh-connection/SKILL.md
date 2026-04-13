---
name: dnos-ssh-connection
description: >-
  How to SSH into DNOS devices and run CLI commands via paramiko.
  Use when connecting to a DNOS device, writing a script that sends CLI
  commands over SSH, or debugging SSH connection issues to DNOS.
---

# DNOS Device SSH Connection

## Credentials

- **Username**: `dnroot`
- **Password**: `dnroot`
- SSH lands directly into the **DNOS CLI** (not a bash shell). There is no bash/linux shell available through this login.

## Connection Method

Use **paramiko** with an interactive shell (`invoke_shell`). Direct `exec_command` does NOT work because the DNOS CLI is an interactive application, not a POSIX shell.

### Required paramiko settings

```python
client.connect(host, username='dnroot', password='dnroot',
               look_for_keys=False, allow_agent=False, timeout=15)
shell = client.invoke_shell(width=250, height=5000)
time.sleep(6)  # Wait for CLI to load ("DRIVENETS CLI Loading...")
```

- `look_for_keys=False` and `allow_agent=False` are **mandatory** to avoid "Too many authentication failures".
- Initial sleep of **6 seconds** is needed for the CLI to fully load and show the prompt (`<hostname>#`).
- Use `width=250, height=5000` to reduce paging. A large height value minimizes `-- More --` prompts.

## Sending Commands

- Send each command with `shell.send(cmd + "\n")`, then `time.sleep(5)` and read with `shell.recv()`.
- After sending, poll `shell.recv_ready()` in a loop with retries to capture full output.
- Pipe `| no-more` after show commands to disable paging (e.g., `show interfaces | no-more`, `show route | no-more`).
- The `set cli screen-length 0` command does NOT work on DNOS.
- `paginate false` and `environment cli no-more` do NOT work either.

## ANSI Escape Cleanup

Always strip ANSI escape sequences and carriage returns from output:

```python
import re
output = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', output)
output = re.sub(r'\r', '', output)
output = re.sub(r'-- More -- \(Press q to quit\)\s*', '', output)
```

## CLI Navigation

- `configure` enters config mode. Prompt changes to `<hostname>(cfg)#`.
- Sub-modes deepen the prompt: `<hostname>(cfg-if-ge400-0/0/3-urpf)#`.
- `top` returns to top config level. `exit` exits current mode.
- `commit` applies pending config. `rollback` discards.
- `show config <path> | no-more` shows running configuration.
- `show interfaces detail <name>` shows full interface info.
- `show interfaces counters <name>` shows traffic/drop counters.

## Reusable Helper Script

A helper script exists at `/home/dn/dnos_cmd.py`:

```
python3 /home/dn/dnos_cmd.py <hostname> "command1" "command2" ...
```

It handles connection, CLI load wait, command execution, output cleanup, and exit.

## Common Gotchas

- `show running-config` is NOT a valid command. Use `show config <section> | no-more`.
- `show system information` is NOT valid. Use `show system`.
- The `?` suffix shows help but also navigates into the sub-mode on the CLI; avoid using it in automated scripts.
- If SSH fails with "Too many authentication failures", ensure `look_for_keys=False` is set.
- If you see "Warning: CLI is not running", the device may still be booting. Wait and retry.
