# Y.1731 Test Script - Shell Session Context Fix (CRITICAL)

## Issue

The system event test was still failing with:
```
ERROR: Unknown word: 'commit'.
```

Even though the logs showed we were in config mode `(cfg)#`, the commit command was being sent at the root prompt `#`.

## Root Cause Discovery

The code was calling `run_shell_sequence_detailed()` **multiple times**:

```python
# First call - sends pre-commit commands in shell session 1
pre_outputs = run_shell_sequence_detailed(client, event_setup_cmds_pre_commit, timeout=60)

# Second call - sends commit in NEW shell session 2 (starts at root prompt!)
commit_outputs = run_shell_sequence_detailed(client, event_commit_cmds, timeout=60)
```

### The Problem

**Each call to `run_shell_sequence_detailed` opens a BRAND NEW shell session!**

- First call: Opens shell #1, enters config mode, sends setup commands, **shell closes**
- Second call: Opens shell #2, starts at root prompt `#`, tries to send `commit` → **ERROR**

The shell context does NOT persist between separate `run_shell_sequence_detailed` calls!

## Solution

Combined ALL commands into a **single command sequence** sent in **one shell session**:

### Before (Wrong - 2 separate shells)
```python
event_setup_cmds_pre_commit = [
    "configure",
    # ... profile and session config ...
    "exit",  # Back to cfg
]
event_commit_cmds = ["commit"]  # ❌ NEW SHELL! Starts at #

# Call 1: Shell session 1
pre_outputs = run_shell_sequence_detailed(client, event_setup_cmds_pre_commit, ...)
# Call 2: Shell session 2 (fresh start!)
commit_outputs = run_shell_sequence_detailed(client, event_commit_cmds, ...)
```

### After (Correct - 1 shell session)
```python
event_setup_and_commit_cmds = [
    "configure",
    # ... profile config ...
    # ... session config ...
    "exit", "exit", "exit", "exit",  # Back to cfg
    "commit",  # ✓ Same shell, in (cfg)# mode!
    "exit",  # Exit config mode
]

# Single call: Everything in ONE shell session
all_outputs = run_shell_sequence_detailed(client, event_setup_and_commit_cmds, ...)
```

### Additional Improvements

1. **Simplified error handling** - Since the command sequence includes the final `exit`, config changes are automatically discarded if commit fails. No need for complex rollback logic.

2. **Auto-conflict resolution** - If commit fails with "in use with session" error, the script deletes the conflicting session and retries the entire sequence (up to 2 attempts).

## Expected Result

The system event test should now:
1. ✓ Create profile and session configuration in ONE shell session
2. ✓ Commit while still in the same shell (in config mode)
3. ✓ Detect MEP conflicts and auto-delete conflicting sessions
4. ✓ Wait for the threshold violation event
5. ✓ Verify `CFM_PROACTIVE_TEST_FAILURE` in syslog
6. ✓ **PASS**

## Key Lesson

**Shell context persistence:**
- Commands sent in the **same** `run_shell_sequence_detailed` call execute in the **same shell session**
- Commands sent in **separate** `run_shell_sequence_detailed` calls execute in **different shell sessions**
- To maintain context (like being in config mode), **all related commands must be in one sequence**!

## Run the Fixed Script

```bash
cd ~/Auto-nog
python3 y1731_cli_tab_test.py \
  --host WKY1C7VD00008P2 \
  --user dnroot \
  --all-meps \
  --show-progress \
  --output-format table \
  --cleanup
```

This fix ensures the commit command executes in the correct shell context!
