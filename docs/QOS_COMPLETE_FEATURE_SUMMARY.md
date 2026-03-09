# QoS Test Script - Complete Feature Summary

## Overview
Enhanced QoS sanity test script with separate interface support and automatic summary generation.

---

## Features Added

### 1. Separate Ingress/Egress Interface Selection ✅

Test ingress and egress policies on **different interfaces**.

#### Usage Examples

```bash
# Separate interfaces (ingress on 96, egress on 97)
python3 qos_sanity_test.py \
  --ingress-interface ge100-0/0/96 \
  --egress-interface ge100-0/0/97

# Single interface for both (backward compatible)
python3 qos_sanity_test.py --interface ge100-0/0/96

# Auto-discovery (uses different if available)
python3 qos_sanity_test.py

# Mixed (specify one, auto-discover other)
python3 qos_sanity_test.py --ingress-interface ge100-0/0/96
```

#### Test Output
```
TEST 3: Discover interfaces and attach policies
  [PASS] Use forced interfaces -- Ingress: ge100-0/0/96, Egress: ge100-0/0/97

TEST 7: Verify policies on interface
  [PASS] Ingress policy on interface -- Ingress_Child_Classify_Only on ge100-0/0/96
  [PASS] Egress policy on interface -- Egress_Full on ge100-0/0/97

TEST 8: Verify QoS counters
  [PASS] Ingress counter rules -- Rules: 1, 2, 3, 4, 5, 6, 7, default
  [PASS] Egress counter rules -- Rules: 1, 2, 3, 4, 5, 6, 7, default
```

### 2. Automatic Summary File Generation ✅

Generates detailed markdown summary after each test run.

#### Summary File Contents
- **Statistics**: Pass/fail counts, pass rate, execution time
- **Test Results**: All tests grouped by Setup/Validation/Cleanup
- **Interface Info**: Shows ingress and egress interfaces used
- **Failed Tests**: Detailed error information (if any)
- **Configuration**: Policies, TCMs, rules tested

#### Example Summary
```markdown
# QoS Sanity Test Summary

**Device**: xgu1f7v900009p2
**Date**: 2026-02-11 12:51:26
**Duration**: 22.8s
**Result**: ALL TESTS PASSED

## Statistics

| Metric | Value |
|--------|-------|
| Total Tests | 55 |
| Passed | 55 |
| Failed | 0 |
| Pass Rate | 100.0% |

## Configuration Details

- **Ingress Interface**: ge100-0/0/96
- **Egress Interface**: ge100-0/0/97
- **Ingress Policy**: Ingress_Child_Classify_Only
- **Egress Policy**: Egress_Full
```

---

## Complete Usage Guide

### All Available Options

```bash
python3 qos_sanity_test.py \
  --host <device>                      # Device hostname or IP
  --user <username>                    # SSH username (default: dnroot)
  --password <password>                # SSH password (default: dnroot)
  --interface <name>                   # Single interface for both directions
  --ingress-interface <name>           # Specific ingress interface
  --egress-interface <name>            # Specific egress interface
  --no-cleanup                         # Skip cleanup phase
```

### Common Usage Patterns

#### 1. Quick Validation (Auto-Everything)
```bash
python3 qos_sanity_test.py
```
- Auto-discovers device and interfaces
- Uses different interfaces if 2+ available
- Runs full test with cleanup

#### 2. Test Specific Interfaces
```bash
python3 qos_sanity_test.py \
  --ingress-interface ge100-0/0/96 \
  --egress-interface ge100-0/0/97
```
- Tests ingress on port 96
- Tests egress on port 97
- Full validation and cleanup

#### 3. Same Interface, No Cleanup
```bash
python3 qos_sanity_test.py \
  --interface ge100-0/0/96 \
  --no-cleanup
```
- Both policies on same interface
- Leaves config in place after tests

#### 4. Production Test on Different Device
```bash
python3 qos_sanity_test.py \
  --host prod-router-01 \
  --ingress-interface ge100-0/0/48 \
  --egress-interface ge100-0/0/49 \
  --no-cleanup
```

---

## Test Results

### With Separate Interfaces
```
Device: xgu1f7v900009p2
Ingress: ge100-0/0/96
Egress: ge100-0/0/97

Total : 55
Passed: 55 ✅
Failed: 0
Time  : 22.8s

ALL TESTS PASSED ✅
```

### With Single Interface
```
Device: xgu1f7v900009p2
Ingress: ge100-0/0/96
Egress: ge100-0/0/96

Total : 55
Passed: 55 ✅
Failed: 0
Time  : 23.0s

ALL TESTS PASSED ✅
```

---

## Technical Details

### Interface Resolution Priority
1. **`--ingress-interface`** / **`--egress-interface`** (highest)
2. **`--interface`** (applies to both if not overridden)
3. **Auto-discovery** (picks different if 2+ UP, same if 1 UP)

### Test Flow with Separate Interfaces

**Phase 1: Setup**
1. Snapshot existing config
2. Apply missing TCMs/policies/hw-mapping
3. Discover or use specified interfaces
4. Attach ingress policy to ingress interface
5. Attach egress policy to egress interface

**Phase 2: Validation**
6. Verify TCMs (config-level, not interface-specific)
7. Verify ingress policy rules (config-level)
8. Verify egress policy rules (config-level)
9. Verify ingress policy on ingress interface
10. Verify egress policy on egress interface
11. Verify ingress counters on ingress interface
12. Verify egress counters on egress interface
13. Clear counters (ingress interface)
14. Verify egress queues on egress interface
15. Modify bandwidth (egress policy)
16. Verify bandwidth change on egress interface
17. Revert bandwidth
18. Verify bandwidth revert on egress interface

**Phase 3: Cleanup**
19. Detach ingress policy from ingress interface
20. Detach egress policy from egress interface
21. Remove created config
22. Verify baseline restored

---

## Git History

| Commit | Description | Status |
|--------|-------------|--------|
| 2235e02 | Fix interface discovery parser | ✅ Pushed |
| 6b28465 | Add automatic summary generation | ✅ Pushed |
| b4601fe | Add --interface option | ✅ Pushed |
| 96a59b3 | Add separate ingress/egress interfaces | ✅ Pushed |
| 258b71f | Fix all tests for separate interfaces | ✅ Pushed |

**Repository**: https://github.com/nhenchinski-dn/Auto-nog  
**Branch**: 2026-02-04-15by  
**Status**: ✅ All changes pushed

---

## Files Generated

### Summary Files (Auto-Generated Per Run)
- `qos_test_summary_<device>_<timestamp>.md`
- Example: `qos_test_summary_xgu1f7v900009p2_20260211_125126.md`

### Documentation Files (This Session)
- `TEST_SESSION_SUMMARY_2026-02-11.md` - Detailed session log
- `SUMMARY_FILE_FEATURE_ADDED.md` - Summary feature docs
- `INTERFACE_OPTION_ADDED.md` - Interface option docs
- `SEPARATE_INTERFACES_FEATURE.md` - Separate interfaces guide
- `FINAL_SUMMARY.md` - Quick reference
- `QOS_COMPLETE_FEATURE_SUMMARY.md` - This file

---

## Complete Command Reference

### Basic Tests
```bash
# Auto-discover everything
python3 qos_sanity_test.py

# Specify device
python3 qos_sanity_test.py --host router-lab-01

# No cleanup (leave config)
python3 qos_sanity_test.py --no-cleanup
```

### Interface Selection
```bash
# Same interface for both
python3 qos_sanity_test.py --interface ge100-0/0/96

# Separate interfaces
python3 qos_sanity_test.py \
  --ingress-interface ge100-0/0/96 \
  --egress-interface ge100-0/0/97

# One specified, one auto
python3 qos_sanity_test.py --ingress-interface ge100-0/0/96

# Test on loopback
python3 qos_sanity_test.py --interface lo0
```

### Combined Options
```bash
# Full custom test
python3 qos_sanity_test.py \
  --host prod-router \
  --user admin \
  --password secret \
  --ingress-interface ge100-0/0/48 \
  --egress-interface ge100-0/0/49 \
  --no-cleanup
```

---

## Summary

✅ **All Features Working**
- 55/55 tests passing
- Separate interface support
- Automatic summary generation
- Flexible interface selection
- Robust cleanup logic

✅ **Fully Tested**
- Tested with separate interfaces (ge100-0/0/96, ge100-0/0/97)
- Tested with single interface (ge100-0/0/96)
- Tested with auto-discovery
- All test scenarios pass

✅ **Production Ready**
- Committed to git
- Pushed to GitHub
- Documented
- Validated

---

**Script Location**: https://github.com/nhenchinski-dn/Auto-nog/blob/2026-02-04-15by/qos_sanity_test.py

*Generated: February 11, 2026*
