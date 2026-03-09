# Y.1731 Test Script - Multi-MEP Discovery Fix

## Issue

When using `--all-meps`, the script was only discovering and testing MEP 2, even though the device has two local MEPs configured:

- **MD-CUST / MA-CUST / MEP 2** (direction up, ge400-0/0/24.100)
- **MD-CUST1 / MA-CUST1 / MEP 4** (direction down, ge400-0/0/33.1)

The script only created PM sessions for MEP 2.

## Root Cause

The MEP discovery logic in `discover_all_local_meps()` was using overly broad regex patterns:

```python
mep_re = re.compile(r"\bmep\s+(\d+)\b")
```

This pattern would match ANY line containing "mep N", including lines from PM session configurations like:
- `source maintenance-domain MD-CUST maintenance-association MA-CUST mep-id 2`

The logic was supposed to only extract local MEPs from CFM configuration lines like:
- `local-mep 2`
- `local-mep 4`

But it wasn't prioritizing the `local-mep` pattern, so it would sometimes miss MEPs or get confused by other "mep" references in the config.

## Solution

Enhanced the MEP discovery logic with a specific `local-mep` regex pattern and proper precedence:

### 1. Added dedicated local-mep regex

```python
# Match "local-mep N" specifically (not just "mep N")
local_mep_re = re.compile(r"\blocal[-_]mep\s+(\d+)\b", flags=re.IGNORECASE)
```

### 2. Prioritized local-mep pattern in extraction logic

```python
# For local MEPs: prioritize "local-mep N" pattern
for m in local_mep_re.finditer(line):
    candidates[key]["meps"].add(int(m.group(1)))

# Also check for "mep-id N" in non-remote lines (PM session source)
for m in mep_id_re.finditer(line):
    candidates[key]["meps"].add(int(m.group(1)))

# Only use generic "mep N" if not in a remote context
for m in mep_re.finditer(line):
    # Skip if this looks like it's part of "local-mep" or "remote-mep"
    if "local-mep" not in line.lower() and "remote-mep" not in line.lower():
        candidates[key]["meps"].add(int(m.group(1)))
```

## Expected Behavior

With `--all-meps`, the script should now discover and test **both** MEPs:

```
Discovered 2 local MEP(s):
  - MD-CUST / MA-CUST / MEP 2 (direction: up, target: mep-id 1)
  - MD-CUST1 / MA-CUST1 / MEP 4 (direction: down, target: mep-id 3)
```

For each MEP, the script will create:
- DM profile and session (e.g., `DM_CLI_TAB_mep2`, `DM_CLI_TAB_mep4`)
- SLM profile and session (e.g., `SLM_CLI_TAB_mep2`, `SLM_CLI_TAB_mep4`)
- Run all test cases (TAB completion, show commands, operational state, etc.)

## Result

The script will now properly discover and test **all local MEPs** configured on the device, not just the first one.

## Run the Fixed Script

```bash
cd ~/Auto-nog
python3 y1731_cli_tab_test.py \
  --host WKY1C7VD00008P2 \
  --user dnroot \
  --all-meps \
  --show-progress \
  --show-details \
  --output-format table \
  --output-file results_$(date +%Y%m%d_%H%M%S).txt \
  --cleanup
```

The script should now test PM sessions on both MEP 2 and MEP 4!
