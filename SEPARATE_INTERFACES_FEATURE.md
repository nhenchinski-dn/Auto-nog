# Separate Ingress/Egress Interface Selection

## Feature Overview
The QoS test script now supports testing ingress and egress policies on **different interfaces**.

## Usage Options

### Option 1: Separate Interfaces (NEW)
```bash
python3 qos_sanity_test.py \
  --ingress-interface ge100-0/0/96 \
  --egress-interface ge100-0/0/97
```
- Ingress policy attached to ge100-0/0/96
- Egress policy attached to ge100-0/0/97

### Option 2: Single Interface (Existing)
```bash
python3 qos_sanity_test.py --interface ge100-0/0/96
```
- Both policies attached to same interface

### Option 3: Auto-Discovery (Default)
```bash
python3 qos_sanity_test.py
```
- Auto-discovers UP interfaces
- Uses different interfaces if 2+ available
- Falls back to same interface if only 1 available

### Option 4: Mixed (One Specified, One Auto)
```bash
# Specify ingress, auto-discover egress
python3 qos_sanity_test.py --ingress-interface ge100-0/0/96

# Specify egress, auto-discover ingress
python3 qos_sanity_test.py --egress-interface ge100-0/0/97
```

## Priority Order

1. **--ingress-interface** / **--egress-interface** (highest)
2. **--interface** (applies to both if not overridden)
3. **Auto-discovery** (lowest)

Examples:
```bash
# ingress=ge100-0/0/96, egress=ge100-0/0/97
--ingress-interface ge100-0/0/96 --egress-interface ge100-0/0/97

# ingress=ge100-0/0/96, egress=ge100-0/0/96
--interface ge100-0/0/96

# ingress=ge100-0/0/96, egress=auto-discovered
--ingress-interface ge100-0/0/96 --interface ge100-0/0/98

# ingress=ge100-0/0/98, egress=ge100-0/0/98 (interface wins)
--interface ge100-0/0/98 --egress-interface ge100-0/0/99 
# (WAIT, THIS IS WRONG - specific wins!)

# Actually: ingress=ge100-0/0/98, egress=ge100-0/0/99
--interface ge100-0/0/98 --egress-interface ge100-0/0/99
```

## Test Output

### With Separate Interfaces
```
TEST 3: Discover interfaces and attach policies
  [PASS] Use forced interfaces -- Ingress: ge100-0/0/96, Egress: ge100-0/0/97

TEST 7: Verify policies on interface
  [PASS] Ingress policy on interface -- Ingress_Child_Classify_Only on ge100-0/0/96
  [PASS] Egress policy on interface -- Egress_Full on ge100-0/0/97
```

### With Single Interface
```
TEST 3: Discover interfaces and attach policies
  [PASS] Use forced interfaces -- Ingress: ge100-0/0/96, Egress: ge100-0/0/96

TEST 7: Verify policies on interface
  [PASS] Ingress policy on interface -- Ingress_Child_Classify_Only on ge100-0/0/96
  [PASS] Egress policy on interface -- Egress_Full on ge100-0/0/96
```

### With Auto-Discovery (Multiple Available)
```
TEST 3: Discover interfaces and attach policies
  [PASS] Discover interfaces -- Ingress: ge100-0/0/96, Egress: ge100-0/0/97 (from 7 up)
```

### With Auto-Discovery (Single Available)
```
TEST 3: Discover interfaces and attach policies
  [PASS] Discover interfaces -- Using ge100-0/0/96 for both ingress and egress (from 1 up)
```

## Use Cases

### When to Use Separate Interfaces

✅ **Testing ingress/egress independently**
- Isolate ingress vs egress behavior
- Test different interface speeds
- Validate per-direction policy application

✅ **Production-like topology**
- Traffic enters on one interface
- Traffic exits on different interface
- Router/L3 switch scenarios

✅ **Troubleshooting specific interface**
- One interface has issues
- Test policies on specific hardware ports

### When to Use Single Interface

✅ **Simple validation**
- Just verify policies work
- Don't care about interface separation

✅ **Loopback testing**
- Using lo0 for both directions

✅ **Limited UP interfaces**
- Only one interface available

## Backend Changes

### Script Behavior
1. **Interface Resolution**: Resolves ingress/egress interfaces based on priority
2. **Policy Attachment**: Attaches ingress policy to ingress interface, egress to egress interface
3. **Validation**: Tests each policy on its respective interface
4. **Counters/Queues**: Uses appropriate interface for each test
5. **Cleanup**: Detaches policies from correct interfaces

### State Tracking
- `self.ingress_iface`: Interface with ingress policy
- `self.egress_iface`: Interface with egress policy
- `self.target_iface`: Primary interface (= ingress_iface, for compatibility)

## Git Status

**Commit**: 96a59b3
**Branch**: 2026-02-04-15by
**Status**: ✅ Committed locally

## Examples

```bash
# Test on two 100G ports
python3 qos_sanity_test.py \
  --ingress-interface ge100-0/0/96 \
  --egress-interface ge100-0/0/97 \
  --no-cleanup

# Ingress on 100G, egress on 10G
python3 qos_sanity_test.py \
  --ingress-interface ge100-0/0/96 \
  --egress-interface ge10-0/0/5

# Ingress on loopback, egress auto-discovered
python3 qos_sanity_test.py --ingress-interface lo0

# Both on same interface (explicit)
python3 qos_sanity_test.py --interface ge100-0/0/96
```

---
*Generated: 2026-02-11*
