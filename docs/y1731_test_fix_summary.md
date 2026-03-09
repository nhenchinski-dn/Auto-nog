# Y.1731 CLI Test Script - Auto-Conflict Resolution Feature

**Date:** February 8, 2026  
**Feature:** Automatic detection and resolution of MEP conflicts  
**Status:** ✅ Implemented

---

## Overview

The script now **automatically detects and resolves** MEP conflicts when running tests. If a MEP already has an active PM session blocking test configuration, the script will:

1. ✅ Detect the "in use with session" error
2. ✅ Extract the conflicting session name
3. ✅ Delete the conflicting session
4. ✅ Retry the configuration automatically
5. ✅ Continue testing seamlessly

---

## How It Works

### Detection Phase
When commit fails with error like:
```
ERROR: Source MD MD-CUST MA MA-CUST LMEP 2 in use with session DM_CLI_TAB.
```

The script automatically:
- Parses the error to extract session name: `DM_CLI_TAB`
- Validates it's a MEP conflict (not another error type)

### Resolution Phase
```
1. auto_delete_conflicting_session: DM_CLI_TAB
   → Tries deleting as both DM and SLM (one succeeds)
   → Commits deletion
   
2. retry_configure_dm_session
   → Retries original configuration
   → Session now succeeds (MEP is free)
```

### Test Output
```
| auto_delete_conflicting_session       | PASS | Session 'DM_CLI_TAB' was blocking MEP 2. Deletion: Deleted existing session 'DM_CLI_TAB' successfully. |
| retry_configure_dm_session            | PASS | DM session configured after deleting conflicting session. |
| commit                                | PASS | Commit OK. |
```

---

## Benefits

### Before Auto-Resolution
```bash
# Test on MEP with existing session
./y1731_cli_tab_test.py --host <DEVICE> --mep-id 2 --all-meps

# Result:
# ❌ configure_dm_session: FAIL (MEP in use)
# ❌ abort: FAIL (Base configuration failed; skipped commit/negative steps)
# 🛑 All tests skipped for this MEP
```

### After Auto-Resolution
```bash
# Same test on MEP with existing session
./y1731_cli_tab_test.py --host <DEVICE> --mep-id 2 --all-meps

# Result:
# ✅ auto_delete_conflicting_session: PASS (Deleted 'DM_CLI_TAB')
# ✅ retry_configure_dm_session: PASS
# ✅ All tests continue normally
```

---

## Implementation Details

### New Functions

#### 1. `extract_conflicting_session_name(error_messages: List[str]) -> Optional[str]`
```python
"""
Extract the existing session name from "in use with session <NAME>" error.
Returns the session name if found, None otherwise.
"""
# Example input: "ERROR: Source MD MD-CUST MA MA-CUST LMEP 2 in use with session DM_CLI_TAB."
# Returns: "DM_CLI_TAB"
```

#### 2. `delete_existing_pm_session(client, session_name, timeout=60) -> Tuple[bool, str]`
```python
"""
Delete an existing PM session (DM or SLM) by name.
- Tries both DM and SLM deletion (one will work, one will get "Unknown word")
- Commits the deletion
- Returns (success, details_message)
"""
```

### Modified Logic

#### DM Session Configuration
```python
# Try initial configuration
config_failed = attempt_config()

if config_failed and "in use with session" in errors:
    conflicting_session = extract_conflicting_session_name(errors)
    if conflicting_session:
        # Auto-delete
        delete_ok = delete_existing_pm_session(client, conflicting_session)
        if delete_ok:
            # Retry configuration
            config_failed = attempt_config()
```

#### SLM Session Configuration
Same auto-resolution logic applied to SLM sessions.

---

## Usage

### Automatic Mode (Default)
```bash
cd ~/Auto-nog && python3 y1731_cli_tab_test.py \
  --host WKY1C7VD00008P2 \
  --user dnroot \
  --all-meps \
  --cleanup
```
**Behavior:** Automatically deletes conflicting sessions and continues testing

### Manual Pre-Cleanup (Optional)
If you want to see which sessions exist first:
```bash
# SSH to device
show services performance-monitoring cfm tests proactive

# Output shows existing sessions:
# Session Name: DM_CLI_TAB, MEP: 2, MD: MD-CUST, MA: MA-CUST

# Then run script - it will auto-delete DM_CLI_TAB if needed
```

---

## Test Output Examples

### Scenario 1: No Conflicts (Clean Device)
```
| configure_dm_session           | PASS | DM session configured. |
| commit                         | PASS | Commit OK. |
| configure_slm_session          | PASS | SLM session configured. |
```

### Scenario 2: DM Conflict Detected & Resolved
```
| configure_dm_session                  | FAIL | Failed command: commit
ERROR: Source MD MD-CUST MA MA-CUST LMEP 2 in use with session DM_OLD. |
| auto_delete_conflicting_session       | PASS | Session 'DM_OLD' was blocking MEP 2. Deletion: Deleted existing session 'DM_OLD' successfully. |
| retry_configure_dm_session            | PASS | DM session configured after deleting conflicting session. |
| commit                                | PASS | Commit OK. |
```

### Scenario 3: SLM Conflict Detected & Resolved
```
| configure_slm_session                 | FAIL | Failed command: commit
ERROR: Source MD MD-CUST MA MA-CUST LMEP 5 in use with session SLM_PROD. |
| auto_delete_conflicting_slm_session   | PASS | Session 'SLM_PROD' was blocking MEP 5. Deletion: Deleted existing session 'SLM_PROD' successfully. |
| retry_configure_slm_session           | PASS | SLM session configured after deleting conflicting session. |
```

### Scenario 4: Multiple MEPs with --all-meps
```
| [MEP 1] configure_dm_session          | PASS | DM session configured. |
| [MEP 2] configure_dm_session          | FAIL | ... in use with session DM_CLI_TAB. |
| [MEP 2] auto_delete_conflicting_...   | PASS | Deleted existing session 'DM_CLI_TAB' successfully. |
| [MEP 2] retry_configure_dm_session    | PASS | DM session configured after deleting conflicting session. |
| [MEP 3] configure_dm_session          | PASS | DM session configured. |
```

---

## Error Handling

### Deletion Failure
If the conflicting session cannot be deleted (e.g., permission error, session locked):
```
| auto_delete_conflicting_session | FAIL | Session 'DM_CLI_TAB' was blocking MEP 2. Deletion: Commit failed during deletion: <error> |
| abort                          | FAIL | Base configuration failed; skipped commit/negative steps. |
```

### Retry Failure
If retry still fails after successful deletion:
```
| auto_delete_conflicting_session    | PASS | Deleted existing session 'DM_CLI_TAB' successfully. |
| retry_configure_dm_session         | FAIL | Failed command: commit
ERROR: <different error> |
| abort                              | FAIL | Base configuration failed; skipped commit/negative steps. |
```

---

## Safety Features

1. **Conflict-Specific**: Only triggers on "in use with session" errors, not general commit failures
2. **Session Name Extraction**: Uses regex to safely parse error messages
3. **Type-Agnostic Deletion**: Tries both DM and SLM deletion (handles unknown session type)
4. **Graceful Unknown Word Handling**: Ignores "Unknown word" when trying wrong session type
5. **Single Retry**: Only retries once (prevents infinite loops)
6. **Detailed Logging**: All deletion and retry attempts logged in test results

---

## Compatibility

### Device Requirements
- Device must support PM session deletion via CLI
- Commit must succeed after deletion (no dependencies blocking it)

### Script Requirements
- No additional CLI arguments needed
- Works with `--all-meps` for multi-MEP testing
- Compatible with `--cleanup` flag

---

## Related Changes

### Also Fixed in This Update
1. **Cleanup Error Handling**: "Unknown word" during cleanup now treated as warning, not failure
2. **Improved Error Messages**: MEP conflict errors now explain device limit (1 session per MEP)

### Files Modified
- `/home/dn/Auto-nog/y1731_cli_tab_test.py`
  - Added: `extract_conflicting_session_name()` function
  - Added: `delete_existing_pm_session()` function
  - Modified: DM session configuration with auto-retry logic
  - Modified: SLM session configuration with auto-retry logic
  - Modified: `cleanup_config()` to handle "Unknown word" gracefully

---

## Verification

### How to Test
1. Create a PM session manually on device:
   ```
   configure
   services performance-monitoring cfm two-way-delay-measurement TEST_SESSION
     admin-state enabled
     source maintenance-domain MD-CUST maintenance-association MA-CUST mep-id 2
     target mep-id 3
     profile DEFAULT_PROFILE
   commit
   ```

2. Run script targeting same MEP:
   ```bash
   cd ~/Auto-nog && python3 y1731_cli_tab_test.py \
     --host WKY1C7VD00008P2 \
     --user dnroot \
     --mep-id 2 \
     --show-progress
   ```

3. Expected: Script auto-deletes `TEST_SESSION` and continues testing

---

## Summary

| Feature | Status |
|---------|--------|
| Auto-detect MEP conflicts | ✅ Implemented |
| Extract conflicting session name | ✅ Implemented |
| Auto-delete conflicting session | ✅ Implemented |
| Retry configuration after deletion | ✅ Implemented |
| Works with --all-meps | ✅ Implemented |
| Detailed logging | ✅ Implemented |
| Error handling | ✅ Implemented |
| Backward compatible | ✅ Yes |

**Result:** Tests now run seamlessly even on devices with existing PM sessions!
