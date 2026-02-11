# Y.1731 Multi-MEP Discovery - FIXED! ✅

## Problem Solved

The script was only discovering 1 MEP instead of 2 due to DNOS CLI paging.

## Root Cause

Device config output was being **paginated** at 29 lines with:
```
[7m-- More -- (Press q to quit)[0m
```

The script stopped reading when it hit the pager prompt, truncating the output before reaching the second maintenance-domain (MD-CUST1).

## Solution: Use `| no-more`

Added `| no-more` pipe to all discovery commands to **disable pagination entirely**.

### Before Fix:
```
Command: show config services ethernet-oam connectivity-fault-management
Output: 29 lines, 763 chars
Found MDs: ['MD-CUST', 'MD-CUST1']  ← Both found in partial output
Found MAs: ['MA-CUST']               ← Only first MA before truncation
Found MEPs: 1                        ← MEP 2 only
```

### After Fix:
```
Command: show config services ethernet-oam connectivity-fault-management | no-more
Output: 46 lines, 1081 chars         ← Complete output!
Found MDs: ['MD-CUST', 'MD-CUST1']   ← Both MDs
Found MAs: ['MA-CUST', 'MA-CUST1']   ← Both MAs ✅
Found MEPs: 2                        ← Both MEPs ✅
```

## Discovered MEPs

```
======================================================================
DISCOVERED 2 LOCAL MEP(S):
======================================================================
  1. MEP 2
     MD/MA: MD-CUST/MA-CUST
     Direction: up
     Target: mep-id 1
     
  2. MEP 4
     MD/MA: MD-CUST1/MA-CUST1
     Direction: down
     Target: mep-id 3
======================================================================
```

## Expected Final Result

The script will now create **4 PM sessions** (one DM + one SLM for each MEP):

1. `DM_CLI_TAB_mep2` - Delay Measurement for MEP 2
2. `SLM_CLI_TAB_mep2` - Synthetic Loss Measurement for MEP 2
3. `DM_CLI_TAB_mep4` - Delay Measurement for MEP 4
4. `SLM_CLI_TAB_mep4` - Synthetic Loss Measurement for MEP 4

All sessions will be preserved after testing completes (when using `--no-cleanup`).

## Code Changes

### 1. Discovery Commands (prioritized):
```python
show_cmds = [
    "show config services ethernet-oam connectivity-fault-management | display-set | no-more",
    "show config services ethernet-oam connectivity-fault-management | no-more",
    # ... fallbacks without no-more for older DNOS versions
]
```

### 2. Simplified read function:
```python
def run_shell_with_prompt_long(client, command, timeout=60):
    """Simple read with 5s quiet timeout - no pager handling needed with no-more"""
    # Send command, read with generous quiet threshold
    # No need for complex pager detection anymore!
```

### 3. Added discovery output display:
```python
# After discovery, show user what was found
print(f"\n{'='*70}")
print(f"DISCOVERED {len(list_all)} LOCAL MEP(S):")
for idx, (md, ma, mep_id, direction, target) in enumerate(list_all, 1):
    print(f"  {idx}. MEP {mep_id}")
    print(f"     MD/MA: {md}/{ma}")
    # ...
```

## Git Commits

1. **908a0eb** - Use '| no-more' to disable paging for discovery commands
2. **81a4d48** - Add discovered MEPs display after discovery phase
3. **d58671c** - Add extensive MEP discovery debugging and display-set fallback

## Testing

Run with:
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

Monitor for:
- ✅ "DISCOVERED 2 LOCAL MEP(S)" message
- ✅ Both `[MEP 2]` and `[MEP 4]` test sections
- ✅ All 4 sessions in final `show config`

## Lessons Learned

1. **Always check for CLI pagination** when reading large config outputs
2. **Use `| no-more`** instead of complex pager detection
3. **Save raw output to file** for debugging truncation issues
4. **Add comprehensive debug logging** to understand discovery process

## Next Steps

The remaining issue is the **event test auto-disable** not working. The verbose debug output added will help diagnose why the auto-conflict resolution isn't triggering the retry.
