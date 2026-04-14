---
name: dnos-snmp-testing
description: >-
  SNMP testing patterns and workflows on DNOS devices. Use when working
  with SNMP walks, MIB tables, trap testing, or SNMP-based verification
  on DNOS.
---

# SNMP Testing on DNOS

## MIB Setup

MIB files live in `~/.snmp/mibs/`. The `snmpwalk`/`snmpget` tools auto-load them for OID translation.
Enterprise OID base for DriveNets: `1.3.6.1.4.1.49739`

## Core Commands

```bash
# Walk an entire table (use -t for slow agents)
snmpwalk -v2c -c <community> -t 15 <DUT_IP> <OID>

# Get a specific scalar
snmpget -v2c -c <community> <DUT_IP> <OID>

# Numeric output (bypass MIB index range validation issues)
snmpwalk -v2c -c <community> -On <DUT_IP> <OID>
```

## Filtering by Index

SNMP table rows are identified by compound indices (e.g., `MdIndex.MaIndex.MepId.SessionId`). To get results for a single entry:

```bash
snmpwalk -v2c -c <community> <DUT_IP> <TABLE_OID> | grep '\.<idx1>\.<idx2>\.<idx3>\.'
```

**Never hardcode runtime IDs** (session IDs, memory pointers) — they change on restart/recycle. Always filter by the stable portion of the index (e.g., MdIndex.MaIndex.MepId).

## Testing Patterns

### Table Content & Consistency
1. Walk each table, verify all MIB-defined columns are present and populated
2. Cross-check SNMP values against CLI `show` output or operational state
3. Verify enumerated types match MIB definitions (e.g., `mepId(2)` vs `macAddress(1)`)
4. Check timestamps: StartTime != EndTime for completed entries; incomplete entries have EndTime = `0-0-0`

### Statistics Over Time (polling)
1. Poll at T0, wait **60 seconds** (some tables refresh slowly), poll at T1
2. Compare: entry indices should increment, timestamps advance, rolling windows shift
3. For rolling-window tables: oldest entries age out, count of entries stays constant
4. Cumulative counters (e.g., TxfcbTc) should monotonically increase across intervals

### Stop/Start Session Verification
1. Disable session via config (`admin-state disable` + `commit`)
2. Poll — results should freeze (no new entries, last entry may show `invalid` or `incomplete`)
3. Stale entries typically remain in rolling-window tables (by design)
4. Re-enable — new entries appear with a gap in indices; no data during disabled period

### SNMP Trap Testing
1. Start trap receiver: `sudo snmptrapd -f -Lo -n -c /dev/null --disableAuthorization=yes -x '' <port>`
2. Configure trap destination on DUT pointing to local machine IP and port
3. Trigger conditions that generate traps, then verify varbinds against MIB NOTIFICATION-TYPE definitions
4. Correlate traps with DUT system events (`show system event-log` or syslog)
5. Negative test: disable traps on DUT, verify zero traps received

## DUT CLI Reference (DNOS)

### Config mode
```
configure
services <feature> ...
services performance-monitoring cfm two-way-delay-measurement <TAB><TAB>
```

### SNMP config on DUT
Look under `system snmp` or use tab-complete from `configure` mode — hierarchy varies across builds.

### Operational commands
```
run <feature> ...
show system event-log | no-more
```

## Common Gotchas

- **Slow tables**: Some SNMP tables take 30–60s to refresh after changes. Use `-t 30` and poll with sufficient intervals.
- **Partial index walks fail**: Walking a table with a partial compound index containing large numeric values causes "Index out of range" from the MIB parser. Use full table walk + `grep` instead.
- **On-demand results are ephemeral**: Walk immediately after the CLI test completes; data may disappear.
- **Only one on-demand test at a time** per entity — concurrent attempts return "already in progress".
- **Config CLI path names differ from show/operational paths**: e.g., `connectivity-fault-management` vs `cfm`, `local-mep` vs `mep`. Always tab-complete.
- **Community string**: Verify with `snmpget -v2c -c <community> <DUT_IP> sysName.0` before running long walks.

## Jira Test Results Template

When posting SNMP test results to Jira, include:
- DUT hostname/IP and software version
- SNMP version and community
- OIDs tested (table format)
- Step-by-step results with PASS/FAIL per step
- Timestamps from SNMP output as evidence
