# Y.1731 Test Script - Event Test Commit Context Fix

## Issue

The system event test was failing with:
```
| [MEP 2] system_event_cfm_proactive_test_failure | FAIL | Failed to set up low-threshold session for event testing. |
```

When examining the raw output, the error was:
```
## CMD: commit
ERROR: Unknown word: 'commit'.
```

## Root Cause

The shell session was **not in configuration mode** when trying to commit the low-threshold session.

### Context Hierarchy

After configuring the event test session, the shell context is:
```
configure                                       # cfg
  services performance-monitoring cfm           # cfg-srv-pm-cfm
    two-way-delay-measurement DM_LOW_THRESH_SESS  # cfg-pm-cfm-dm-*
      [session config]
      exit                                      # → Back to cfg-srv-pm-cfm
```

The code was sending `commit` as a **separate command in a new shell**, which started at the root prompt `#`, not in config mode `(cfg)#`. The `commit` command only works in config mode, so it failed with "Unknown word: 'commit'".

## Solution

Fixed the command sequence to properly exit back to config mode **before** sending commit:

### Before (Wrong)
```python
event_setup_cmds_pre_commit = [
    "configure",
    # ... profile config ...
    # ... session config ...
    "exit",  # Exit from session context → cfg-srv-pm-cfm
]
event_commit_cmds = ["commit"]  # ❌ Sent in new shell, not in config mode!
event_cleanup_cmds = ["exit", "exit", "exit", "exit"]
```

### After (Correct)
```python
event_setup_cmds_pre_commit = [
    "configure",
    # ... profile config ...
    # ... session config ...
    "exit",  # Exit from session → cfg-srv-pm-cfm
    "exit",  # → cfg-srv-pm
    "exit",  # → cfg-srv
    "exit",  # → cfg
]
event_commit_cmds = ["commit"]  # ✓ Now in config mode!
event_cleanup_cmds = ["exit"]  # Just exit from config mode
```

Now the commit command is sent when the shell is in `(cfg)#` mode, where it's valid.

## Expected Result

The system event test should now:
1. Successfully create the low-threshold DM profile and session
2. Commit the configuration cleanly
3. Wait for the threshold violation event
4. Verify the `CFM_PROACTIVE_TEST_FAILURE` event in syslog
5. **PASS** ✓

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

The system event test should now pass!
