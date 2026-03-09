# Interface Selection Option Added

## Feature
Added `--interface` option to QoS sanity test script for manual interface selection.

## Usage

### Auto-Discovery (Default)
```bash
python3 qos_sanity_test.py
```
- Automatically finds first UP interface
- Uses ge100-0/0/96, ge100-0/0/97, lo0, etc.

### Manual Selection (NEW)
```bash
# Test on specific 100G port
python3 qos_sanity_test.py --interface ge100-0/0/97

# Test on loopback interface
python3 qos_sanity_test.py --interface lo0

# Test on 10G port
python3 qos_sanity_test.py --interface ge10-0/0/5

# Combine with other options
python3 qos_sanity_test.py --interface ge100-0/0/98 --no-cleanup --host mydevice
```

## When to Use Manual Selection

✅ **Use --interface when:**
- Testing a specific interface
- Auto-discovery picks wrong interface  
- Interface is DOWN but you want to test anyway
- Testing multiple interfaces sequentially
- Scripting tests for specific interfaces

✅ **Use auto-discovery when:**
- Testing any available interface
- Don't care which interface is used
- Quick validation testing

## How It Works

1. **With --interface**: Script uses specified interface immediately, skips discovery
2. **Without --interface**: Script runs `show interfaces` and picks first UP interface

## Test Output

### With Manual Selection
```
TEST 3: Discover interface and attach policies
  [PASS] Use forced interface -- Using specified interface: ge100-0/0/97
```

### With Auto-Discovery
```
TEST 3: Discover interface and attach policies
  [PASS] Discover up interface -- Using ge100-0/0/96 (from 7 up)
```

## Git Status

**Commit**: b4601fe  
**Branch**: 2026-02-04-15by  
**Status**: ✅ Committed locally, ready to push

## Changes Made

**File**: qos_sanity_test.py
- Added `--interface` argument to argparse
- Added `forced_interface` parameter to `__init__`
- Modified `test_attach_policies()` to check for forced interface first
- Updated logic to skip auto-discovery when interface is specified

**Lines Changed**: 20 lines (9 modified, 11 added)

---
*Generated: 2026-02-11*
