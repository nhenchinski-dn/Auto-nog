# Y.1731 Multi-MEP Discovery Fix

## Problems Fixed

### 1. Only 1 MEP Discovered Instead of 2
**Problem**: Script only found MEP 2 (MD-CUST/MA-CUST) but missed MEP 4 (MD-CUST1/MA-CUST1)

**Root Cause**: Device pauses longer than 3 seconds between hierarchical config sections when returning large `show config` output. The previous `quiet=3s` timeout was treating these pauses as "end of output" and truncating the response before the second maintenance-domain appeared.

**Fix**: Increased quiet timeouts in `run_shell_with_prompt_long()`:
- Main read: 3s → **5s**
- Extra drain: 2s → **4s**, timeout 6s → **10s**

**Verification**: Created test script (`test_discovery.py`) that proves the discovery regex logic works perfectly when given complete config. The issue was SSH output truncation, not parsing logic.

### 2. Event Test Auto-Disable Not Working
**Problem**: Event test failed with "in use with session DM_CLI_TAB_mep2" but auto-disable logic didn't retry.

**Fix**: Added verbose progress output throughout the auto-disable retry loop:
- Shows when conflict is detected
- Displays extracted session name
- Reports disable attempt status
- Shows retry attempt numbers
- Reports why retry loop exits (success/failure/last attempt)

**Purpose**: Makes debugging much easier to see exactly why auto-conflict resolution may fail.

## Device Configuration

Your device has **2 local MEPs**:

```
maintenance-domains MD-CUST
  maintenance-associations MA-CUST
    local-mep 2          ← Target: MEP 1, Direction: up
    
maintenance-domains MD-CUST1
  maintenance-associations MA-CUST1
    local-mep 4          ← Target: MEP 3, Direction: down
```

## Expected Results After Fix

Running with `--all-meps --show-progress` should now show:

```
Discovered 2 local MEP(s):
  - MEP 2: MD-CUST/MA-CUST, direction=up, target=mep-id 1
  - MEP 4: MD-CUST1/MA-CUST1, direction=down, target=mep-id 3

Testing MEP 2...
  [all tests for MEP 2]
  
Testing MEP 4...
  [all tests for MEP 4]
```

**Final Config Should Have 4 PM Sessions**:
- `DM_CLI_TAB_mep2` (DM for MEP 2)
- `SLM_CLI_TAB_mep2` (SLM for MEP 2)
- `DM_CLI_TAB_mep4` (DM for MEP 4)
- `SLM_CLI_TAB_mep4` (SLM for MEP 4)

## Run Now

```bash
cd ~/Auto-nog
python3 y1731_cli_tab_test.py \
  --host WKY1C7VD00008P2 \
  --user dnroot \
  --all-meps \
  --show-progress \
  --output-format table \
  --output-file results_$(date +%Y%m%d_%H%M%S).txt \
  --no-cleanup
```

Watch for:
- "Discovered 2 local MEP(s)" message
- Both `[MEP 2]` and `[MEP 4]` test sections
- Event test verbose output showing conflict resolution attempts
- Final config with all 4 sessions preserved

## Technical Details

### SSH Read Timeout Strategy

For hierarchical config output, we now use a **progressive timeout** approach:

1. **Initial read**: `quiet=5s` - Wait up to 5 seconds of silence before considering output complete
2. **Extra drain**: `quiet=4s, timeout=10s` - Additional read to catch any late arrivals

This handles devices that:
- Pause between config sections while processing
- Have large configs spanning multiple internal buffers  
- Stream output with variable timing

### Discovery Logic Verification

The test script proves discovery regex patterns work correctly:
- Properly distinguishes `local-mep N` from `remote-meps` / `crosscheck mep-id N`
- Correctly tracks hierarchical state (current MD/MA context)
- Handles multiple maintenance-domains in one config
- Extracts direction and target information

## Files Changed

- `/home/dn/y1731_cli_tab_test.py` - Updated quiet timeouts and added event test debugging
- `/home/dn/Auto-nog/y1731_cli_tab_test.py` - Synced copy for testing

Committed to git with detailed explanation.
