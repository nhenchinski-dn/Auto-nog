# Y.1731 CLI Test Script - Quick Start with Auto-Conflict Resolution

## 🎯 What's New

The script now **automatically handles MEP conflicts**! No more manual cleanup needed.

---

## 🚀 Quick Start

### Test All MEPs (Recommended)
```bash
cd ~/Auto-nog && python3 y1731_cli_tab_test.py \
  --host WKY1C7VD00008P2 \
  --user dnroot \
  --all-meps \
  --show-progress \
  --cleanup
```

**What happens:**
- ✅ Tests run on all discovered MEPs
- ✅ Existing sessions automatically deleted and recreated
- ✅ Comprehensive test coverage
- ✅ Full cleanup at end

### Test with Full Details & Logging
```bash
cd ~/Auto-nog && python3 y1731_cli_tab_test.py \
  --host WKY1C7VD00008P2 \
  --user dnroot \
  --all-meps \
  --show-progress \
  --show-details \
  --output-format table \
  --output-file results_$(date +%Y%m%d_%H%M%S).txt \
  --wait-for-results 40 \
  --low-threshold-wait 25 \
  --cleanup
```

---

## 🔄 Auto-Conflict Resolution in Action

### Before (Old Behavior)
```
[MEP 2] configure_dm_session | FAIL | MEP in use with session DM_CLI_TAB
[MEP 2] abort                | FAIL | Base configuration failed
🛑 All tests skipped for MEP 2
```

### After (New Behavior)
```
[MEP 2] configure_dm_session                | FAIL | MEP in use with session DM_CLI_TAB
[MEP 2] auto_delete_conflicting_session     | PASS | Deleted 'DM_CLI_TAB' successfully
[MEP 2] retry_configure_dm_session          | PASS | DM session configured
✅ All tests continue normally for MEP 2
```

---

## 📋 What the Script Tests

### 1. Discovery & Setup (4 tests)
- Discovers all MEPs from device config
- Auto-discovers MD/MA/MEP-ID context
- Validates MEP-IDs via TAB completion

### 2. TAB Completion (14+ tests)
- Session-level TAB (DM/SLM)
- Profile-level TAB (thresholds, test-duration)

### 3. Session Configuration (4+ tests)
- DM session creation with all parameters
- SLM session creation with PCP
- Profile creation with thresholds
- Commit verification

### 4. Profile Variants (25+ tests)
- test-duration: count, time-frame, non-stop
- thresholds: delay, jitter, loss, success-rate
- inform-test-results: enabled/disabled
- PCP values: 0-7 (valid) + 8 (invalid)

### 5. Show Commands (10+ tests)
- Summary, proactive, detail views
- Filters by session/MD/MA/MEP

### 6. On-Demand Tests (13 tests)
- Start + stop matrix (all/MD/MA/type)
- Dual SSH session testing

### 7. Operational State (3 tests)
- Session lifecycle verification
- Parameter change testing
- Historic results validation

### 8. System Events (2 tests)
- Threshold violation detection
- Syslog event verification

### 9. Negative Tests (7 tests)
- Invalid parameters
- Boundary violations
- Dependency deletion rejection

### 10. Cleanup
- Removes all test artifacts

**Total:** 80+ test steps per MEP

---

## 🎓 Common Scenarios

### Scenario 1: First Time Testing
```bash
cd ~/Auto-nog && python3 y1731_cli_tab_test.py \
  --host <YOUR_DEVICE> \
  --user <YOUR_USER> \
  --all-meps \
  --cleanup
```
**Expected:** Clean run, all tests pass

### Scenario 2: Device Has Existing Sessions
```bash
# Same command as above
cd ~/Auto-nog && python3 y1731_cli_tab_test.py \
  --host <YOUR_DEVICE> \
  --user <YOUR_USER> \
  --all-meps \
  --cleanup
```
**Expected:** 
- Script auto-deletes existing sessions
- Tests continue normally
- No manual cleanup needed

### Scenario 3: Test Single MEP
```bash
cd ~/Auto-nog && python3 y1731_cli_tab_test.py \
  --host <YOUR_DEVICE> \
  --user <YOUR_USER> \
  --mep-id 5 \
  --cleanup
```
**Expected:** Focused testing on MEP 5 only

### Scenario 4: Skip Specific Test Categories
```bash
cd ~/Auto-nog && python3 y1731_cli_tab_test.py \
  --host <YOUR_DEVICE> \
  --user <YOUR_USER> \
  --skip-on-demand-stop \
  --skip-event-test \
  --cleanup
```
**Expected:** Faster test run (skips long-running tests)

---

## 🔍 Troubleshooting

### Issue: "Unknown word: DM_CLI_TAB_mep2"
**Status:** ✅ Fixed in latest version  
**Solution:** Already handled - cleanup now gracefully skips non-existent sessions

### Issue: "Source ... in use with session"
**Status:** ✅ Fixed in latest version  
**Solution:** Script auto-deletes conflicting session and retries

### Issue: Tests taking too long
**Solution:** Use skip flags
```bash
--skip-on-demand-stop    # Skips 13 on-demand tests (~2 min)
--skip-event-test        # Skips threshold event test (~25 sec)
--skip-show-proactive    # Skips show command verification
```

### Issue: Need to preserve existing sessions
**Solution:** Don't use `--all-meps`, specify exact MEP without conflicts
```bash
--mep-id <UNUSED_MEP>
```

---

## 📊 Understanding Test Results

### Table Format Output
```
+-------------------------------------------+--------+----------+
| Test                                      | Status | Details  |
+-------------------------------------------+--------+----------+
| configure_dm_session                      | FAIL   | MEP 2 in use |
| auto_delete_conflicting_session           | PASS   | Deleted 'DM_CLI_TAB' |
| retry_configure_dm_session                | PASS   | DM configured |
+-------------------------------------------+--------+----------+

Category: DM Session Configuration & Commit
PASS: 3, FAIL: 0, SKIP: 0
```

### Progress Output (--show-progress)
```
[PROGRESS] discover_cfm_context
[PROGRESS] configure_dm_session
[PROGRESS] auto_delete_conflicting_session: DM_CLI_TAB
[PROGRESS] retry_configure_dm_session
```

---

## 🎯 Best Practices

### ✅ DO:
- Use `--all-meps` for comprehensive testing
- Use `--cleanup` to remove test artifacts
- Use `--output-file` to save results
- Use `--show-progress` to monitor execution
- Let script auto-delete conflicting sessions

### ❌ DON'T:
- Manually delete sessions before running (script handles it)
- Run without `--cleanup` in production (leaves test sessions)
- Use same session names as production sessions
- Interrupt script mid-test (may leave partial config)

---

## 📚 Documentation

- **Full Test Coverage:** `/home/dn/y1731_test_summary.md`
- **Auto-Conflict Resolution Details:** `/home/dn/y1731_test_fix_summary.md`
- **Script Location:** `/home/dn/Auto-nog/y1731_cli_tab_test.py`

---

## 🔗 Quick Reference

| Flag | Purpose |
|------|---------|
| `--host` | Device hostname/IP |
| `--user` | SSH username |
| `--all-meps` | Test all discovered MEPs |
| `--cleanup` | Remove test artifacts at end |
| `--show-progress` | Show real-time progress |
| `--show-details` | Show detailed results |
| `--output-format table` | Pretty table output |
| `--output-file FILE` | Save results to file |
| `--wait-for-results N` | Wait N seconds for probes (default: 30) |
| `--skip-on-demand-stop` | Skip on-demand tests (faster) |
| `--skip-event-test` | Skip system event test (faster) |

---

**Version:** Latest (with auto-conflict resolution)  
**Status:** ✅ Ready to use  
**Date:** February 8, 2026
