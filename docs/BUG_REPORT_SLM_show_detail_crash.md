# BUG REPORT: Proactive SLM `show detail` crash due to concurrent commits + scheduler race condition

**Severity:** Critical  
**Component:** Ethernet OAM Y.1731 - Performance Monitoring (Proactive SLM)  
**Image:** 26.2.0.29  
**Device:** NCPL-CFM-nog (XEC1E3VR00008 / 100.64.5.56)  
**Epic:** SW-141523  
**Date:** 2025-07-04 (device time) / 2026-02-15 (test time)

---

## Summary

The `show services performance-monitoring cfm tests proactive two-way-synthetic-loss session-name <name> detail` command crashes with `"ERROR: Command failed due to unexpected reason"` when the SLM session's historical test results buffer contains entries with `None` start_time values. These entries are generated when `Proactive.cpp:219 StartSession()` returns **status 2** (failure/busy), which creates an OperDB entry with a result-idx but null start_time, and `DoneSession()` is never called.

The root trigger is **concurrent commits from multiple sources** (CLI + RESTCONF/NETCONF), which cause session DEL+ADD restarts and put the scheduler into a failure state.

---

## Two Related Bugs

### BUG A: Orphaned OperDB entries from StartSession() status 2

**Condition:** When concurrent CLI and RESTCONF/NETCONF commits cause a proactive SLM session to be deleted and re-added (`Proactive.cpp:164 DelSession` / `Proactive.cpp:145 AddSession`), the scheduler can enter a state where `StartSession()` returns **status 2** repeatedly. Despite the failure status, the scheduler still allocates a new result-idx in OperDB. Since `DoneSession()` is never called for these entries, they end up with null `start_time` and no End time.

**Datapath trace evidence (`/core/traces/datapath/wb_agent.cfm`):**
```
# Normal cycle - status 1, START has matching DONE:
2025-07-04T03:48:11.058600Z CFM: PROACTIVE START: SLM session SLM_CLI_TAB_mep1 id 100000002 result-idx 2649 status 1
2025-07-04T03:48:11.060609Z CFM: PROACTIVE DONE:  SLM session SLM_CLI_TAB_mep1 id 100000002

# Broken window - status 2, START but NO DONE:
2025-07-04T03:48:21.058794Z CFM: PROACTIVE START: SLM session SLM_CLI_TAB_mep1 id 100000002 result-idx 2651 status 2  ← NO DONE
2025-07-04T03:48:31.058288Z CFM: PROACTIVE START: SLM session SLM_CLI_TAB_mep1 id 100000002 result-idx 2653 status 2  ← NO DONE
2025-07-04T03:48:41.058778Z CFM: PROACTIVE START: SLM session SLM_CLI_TAB_mep1 id 100000002 result-idx 2655 status 2  ← NO DONE
2025-07-04T03:48:51.058764Z CFM: PROACTIVE START: SLM session SLM_CLI_TAB_mep1 id 100000002 result-idx 2657 status 2  ← NO DONE
2025-07-04T03:49:01.057857Z CFM: PROACTIVE START: SLM session SLM_CLI_TAB_mep1 id 100000002 result-idx 2659 status 2  ← NO DONE

# Recovery - status 1 again:
2025-07-04T03:49:11.057958Z CFM: PROACTIVE START: SLM session SLM_CLI_TAB_mep1 id 100000002 result-idx 2661 status 1
2025-07-04T03:50:02.057102Z CFM: PROACTIVE DONE:  SLM session SLM_CLI_TAB_mep1 id 100000002
```

**Session restart trace (trigger):**
```
2025-07-04T03:41:22.580950Z CFM: PROACTIVE SESSION DEL { name: "SLM_CLI_TAB_mep1" type: SLM }
2025-07-04T03:41:22.582998Z CFM: PROACTIVE SESSION ADD { name: "SLM_CLI_TAB_mep1" id: 2 config { ... interval_ms: 1000 pkt_count: 5 ... repeat_interval: 10 } }
```

**Observed `show detail` output for these entries:**
```
Historical Test Results (Last 10):

| Index   | Start Time                | End Time                  | Status     |
|---------+---------------------------+---------------------------+------------|
| 2651    | 2025-07-04 03:48:21 +0000 |                           | invalid    |
| 2653    | 2025-07-04 03:48:31 +0000 |                           | invalid    |
| 2655    | 2025-07-04 03:48:41 +0000 |                           | invalid    |
| 2657    | 2025-07-04 03:48:51 +0000 |                           | invalid    |
| 2659    | 2025-07-04 04:00:01 +0000 |                           | invalid    |
```

### BUG B: `show detail` Command Crash (TypeError in Python sort)

**Condition:** When the historical results buffer contains entries where `start_time` is `None` (from BUG A's orphaned OperDB entries), the CLI Python handler crashes with a TypeError.

**CLI traceback (`/var/log/dn/cli` via `run start shell` > `traces`):**
```
2025-07-04 06:38:54,358 [ ERROR  ] [MainThread     :967739] cli.dn_cmd_handler command 'sh services performance-monitoring cfm tests proactive two-way-synthetic-loss session-name SLM_CLI_TAB_mep1 detail' failed
Traceback (most recent call last):
  File "/dn/python/cli/dn_cmd_handler.py", line 143, in _safe_do_command
    return self._do_command(
  File "/dn/python/cli/dn_cmd_handler.py", line 746, in _do_command
    self.app.component.output.print_show_command(matching_command,
  File "/dn/python/libcli/utils/output.py", line 411, in print_show_command
    res = self._call_show_command(show_action=show_action,
  File "/dn/python/libcli/utils/output.py", line 620, in _call_show_command
    inspect_utils.call_func_with_args_that_exist(
  File "/dn/python/dn_common/utils/inspect_utils.py", line 12, in call_func_with_args_that_exist
    return func(**params_that_exist)
  File "/dn/python/cli/commands/show_commands/show_services_commands.py", line 1066, in action
    output.add_table(rows=rows_gen(), headers=hdr, title='\nHistorical Test Results (Last 10):')
  File "/dn/python/libcli/utils/output.py", line 1016, in add_table
    table_list = tabulate.tabulate(rows,
  File "/dn/venv/lib/python3.12/site-packages/tabulate/__init__.py", line 2048, in tabulate
    list_of_lists, headers = _normalize_tabular_data(
  File "/dn/venv/lib/python3.12/site-packages/tabulate/__init__.py", line 1379, in _normalize_tabular_data
    rows = list(tabular_data)
  File "/dn/python/cli/commands/show_commands/show_services_commands.py", line 1060, in rows_gen
    measurements.sort(key=lambda el: el[1].start_time)
TypeError: '<' not supported between instances of 'str' and 'NoneType'
```

**Root cause in code:** `show_services_commands.py:1060` does `measurements.sort(key=lambda el: el[1].start_time)` -- when `start_time` is `None` for orphaned entries, Python 3 cannot compare `str < NoneType` and raises `TypeError`.

**User-facing error:**
```
NCPL-CFM-nog# show services performance-monitoring cfm tests proactive two-way-synthetic-loss session-name SLM_CLI_TAB_mep1 detail
ERROR: Command failed due to unexpected reason.
```

**Characteristics:**
- Affects ALL SLM sessions sharing the same profile
- DM sessions are NOT affected
- Summary command (`show ... proactive`) still works
- Crash **persists** even after fixing the profile or disabling/re-enabling the session
- Crash **recovers** only when enough new valid entries push the corrupt entries out of the Last 10 buffer

---

## Exact Reproduction Steps (any device)

### Prerequisites

You need two devices with CFM MEPs configured between them on a VLAN sub-interface.

**Minimal CFM + PM baseline configuration (adjust interfaces to your setup):**

```
! === On Device A (the device under test) ===

! 1. Sub-interface with VLAN encapsulation
interfaces
  ethernet-X/Y/Z     ! <-- replace with your physical interface
    admin-state enabled
  !
  ethernet-X/Y/Z.100
    encapsulation dot1q
      outer-tag
        vlan-id 100
      !
    !
    admin-state enabled
  !
!

! 2. CFM Maintenance Domain + Association + MEP
services
  ethernet-oam
    cfm
      maintenance-domain MD-TEST
        md-level 7
        name-type character-string
        md-name test-md
        maintenance-association MA-TEST
          short-ma-name-type character-string
          short-ma-name test-ma
          continuity-check-interval 1s
          mep 1
            direction down
            interface ethernet-X/Y/Z.100
            admin-state enabled
            remote-mep 2
            !
          !
        !
      !
    !
  !
!

! 3. SLM Profile
services
  performance-monitoring
    profiles
      cfm
        two-way-synthetic-loss-measurement SLM_REPRO_PROF
          test-duration
            probes
              probe-count 25
              probe-interval 2
              repeat-interval 60
            !
          !
        !
      !
    !
  !
!

! 4. Proactive SLM Session
services
  performance-monitoring
    cfm-tests
      proactive-monitoring
        two-way-synthetic-loss-measurements
          test-session SLM_REPRO_TEST
            admin-state enabled
            profile SLM_REPRO_PROF
            source
              maintenance-domain MD-TEST
              maintenance-association MA-TEST
              mep-id 1
            !
            destination
              mep-id 2
            !
          !
        !
      !
    !
  !
!
```

**On Device B (remote end):** Configure the same MD/MA with MEP 2, direction down, remote-mep 1, on the matching sub-interface.

### Step 1: Verify the SLM session is running normally

Wait ~2 minutes for valid results to accumulate, then confirm:

```
show services performance-monitoring cfm tests proactive two-way-synthetic-loss session-name SLM_REPRO_TEST detail
```

You should see valid entries with both Start and End times.

### Step 2: Mount device on ODL for RESTCONF access

From your test machine (not the device), mount the device to OpenDaylight:

```bash
curl -s -u admin:admin -X PUT \
  -H "Content-Type: application/json" \
  "http://<ODL_IP>:8181/rests/data/network-topology:network-topology/topology=topology-netconf/node=REPRO_DEVICE" \
  -d '{
    "node": [{
      "node-id": "REPRO_DEVICE",
      "netconf-node-topology:host": "<DEVICE_MGMT_IP>",
      "netconf-node-topology:port": 830,
      "netconf-node-topology:username": "dnroot",
      "netconf-node-topology:password": "dnroot",
      "netconf-node-topology:tcp-only": false,
      "netconf-node-topology:keepalive-delay": 0
    }]
  }'
```

Wait ~30 seconds, then verify mount:
```bash
curl -s -u admin:admin \
  "http://<ODL_IP>:8181/rests/data/network-topology:network-topology/topology=topology-netconf/node=REPRO_DEVICE?fields=netconf-node-topology:connection-status"
```

Should return `"netconf-node-topology:connection-status": "connected"`.

### Step 3: Create the overlap profile + fire concurrent commits

Set the SLM profile to create an overlap condition (repeat-interval < test-duration), while simultaneously bombarding with RESTCONF PATCH commits:

```bash
# Variables - CHANGE THESE to match your setup
DEVICE_IP="<DEVICE_MGMT_IP>"       # e.g., 100.64.5.56
ODL_IP="<ODL_IP>"                  # e.g., 10.10.75.34
ODL_NODE="REPRO_DEVICE"            # node-id used in mount
DEVICE_USER="dnroot"
DEVICE_PASS="dnroot"
SLM_PROF="SLM_REPRO_PROF"         # your SLM profile name
SLM_SESSION="SLM_REPRO_TEST"      # your SLM session name

# RESTCONF payload: creates/modifies a dummy DM profile (forces a NETCONF commit)
PATCH_XML='<drivenets-top xmlns="http://drivenets.com/ns/yang/dn-top"><services xmlns="http://drivenets.com/ns/yang/dn-services"><performance-monitoring xmlns="http://drivenets.com/ns/yang/dn-performance-monitoring"><profiles><cfm><two-way-delay-measurement><profile><profile-name>RACE_TRIGGER_PROF</profile-name><config-items><profile-name>RACE_TRIGGER_PROF</profile-name><inform-test-results>enabled</inform-test-results><test-duration-probes><probe-count>5</probe-count><probe-interval>1</probe-interval><repeat-interval>10</repeat-interval></test-duration-probes></config-items></profile></two-way-delay-measurement></cfm></profiles></performance-monitoring></services></drivenets-top>'

echo "=== Firing 10 rounds of concurrent CLI + RESTCONF commits ==="

for i in $(seq 1 10); do
  # --- RESTCONF PATCH in background (triggers a NETCONF commit on the device) ---
  curl -s -u admin:admin -X PATCH \
    -H "Content-Type: application/xml" -H "Accept: application/xml" \
    "http://${ODL_IP}:8181/rests/data/network-topology:network-topology/topology=topology-netconf/node=${ODL_NODE}/yang-ext:mount/dn-top:drivenets-top" \
    -d "$PATCH_XML" -o /dev/null -w "RESTCONF[$i]: HTTP %{http_code}\n" &

  # --- CLI commit in background (alternates probe-count to force SLM session restart) ---
  PC=$((25 + (i % 2) * 35))   # alternates between 25 and 60
  (sleep 3; echo "config"; sleep 1; \
   echo "services performance-monitoring profiles cfm two-way-synthetic-loss-measurement ${SLM_PROF} test-duration probes probe-count $PC probe-interval 2 repeat-interval 10"; \
   sleep 1; echo "commit"; sleep 5; echo "exit"; sleep 1; echo "exit") \
   | sshpass -p "${DEVICE_PASS}" ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
     -tt ${DEVICE_USER}@${DEVICE_IP} 2>/dev/null &

  sleep 2
done

wait
echo "=== Done. Wait 30 seconds then check for crash. ==="
```

### Step 4: Check for missing End times + crash

Wait ~30 seconds after the script finishes, then on the device CLI:

```
show services performance-monitoring cfm tests proactive two-way-synthetic-loss session-name SLM_REPRO_TEST detail
```

**Expected result:** Either:
- You see entries with Start time but **blank End time** and status `invalid` (BUG A confirmed), OR
- The command crashes with `ERROR: Command failed due to unexpected reason.` (BUG B confirmed)

If you see missing End times but no crash yet, wait for the scheduler to fill the Last 10 buffer with more orphaned entries and retry.

### Step 5: Verify the crash via traces

```
run start shell
Password: dnroot
traces
cd /core/traces/datapath
grep "status 2" wb_agent.cfm | tail -20
```

You should see `PROACTIVE START: SLM session ... status 2` entries with NO matching `PROACTIVE DONE` -- these are the orphaned entries.

For the CLI crash traceback:
```
cd /core/traces/routing_engine
grep -h "TypeError\|start_time\|show_services_commands" dbclient-cli_* | tail -20
```

You should see:
```
measurements.sort(key=lambda el: el[1].start_time)
TypeError: '<' not supported between instances of 'str' and 'NoneType'
```

### Step 6: Cleanup

```
! Remove the dummy RESTCONF profile
config
no services performance-monitoring profiles cfm two-way-delay-measurement RACE_TRIGGER_PROF
commit

! Restore a healthy SLM profile (repeat > duration)
services performance-monitoring profiles cfm two-way-synthetic-loss-measurement SLM_REPRO_PROF test-duration probes probe-count 25 probe-interval 2 repeat-interval 60
commit
exit
```

Unmount from ODL:
```bash
curl -s -u admin:admin -X DELETE \
  "http://<ODL_IP>:8181/rests/data/network-topology:network-topology/topology=topology-netconf/node=REPRO_DEVICE"
```

---

## Evidence Collected

### Datapath traces - StartSession status 2 (missing End time root cause):
```
# From /core/traces/datapath/wb_agent.cfm-20250704_03:54:12.gz
2025-07-04T03:48:21 START SLM_CLI_TAB_mep1 result-idx 2651 status 2   ← NO DONE → missing End time
2025-07-04T03:48:31 START SLM_CLI_TAB_mep1 result-idx 2653 status 2   ← NO DONE → missing End time
2025-07-04T03:48:41 START SLM_CLI_TAB_mep1 result-idx 2655 status 2   ← NO DONE → missing End time
2025-07-04T03:48:51 START SLM_CLI_TAB_mep1 result-idx 2657 status 2   ← NO DONE → missing End time
2025-07-04T03:49:01 START SLM_CLI_TAB_mep1 result-idx 2659 status 2   ← NO DONE → missing End time
```

### CLI Python traceback (show detail crash):
```
File "/dn/python/cli/commands/show_commands/show_services_commands.py", line 1060, in rows_gen
    measurements.sort(key=lambda el: el[1].start_time)
TypeError: '<' not supported between instances of 'str' and 'NoneType'
```

### Session restart from concurrent commit:
```
# From /core/traces/datapath/wb_agent.cfm-20250704_03:42:46.gz
2025-07-04T03:41:22.580950Z CFM: PROACTIVE SESSION DEL { name: "SLM_CLI_TAB_mep1" type: SLM }
2025-07-04T03:41:22.582998Z CFM: PROACTIVE SESSION ADD { name: "SLM_CLI_TAB_mep1" id: 2 ... }
```

### Concurrent commit conflicts (system-events.log):
```
local7.err 2025-07-04T03:35:37 TRANSACTION_COMMIT_CHECK_FAILED: User dnroot committed commit check operation...
local7.err 2025-07-04T03:38:29 TRANSACTION_COMMIT_CHECK_FAILED: ...
local7.err 2025-07-04T03:40:58 TRANSACTION_COMMIT_CHECK_FAILED: ...
(12+ commit failures from 03:35 to 04:06)
```

### Show detail crash (user-facing):
```
NCPL-CFM-nog# show services performance-monitoring cfm tests proactive two-way-synthetic-loss session-name SLM_CLI_TAB_mep1 detail
ERROR: Command failed due to unexpected reason.
```

---

## Additional Bug: YANG Model uint8 Overflow

When retrieving PM operational data via RESTCONF GET, the response fails with:
```
java.lang.IllegalArgumentException: Invalid range: 2828, expected: [[0..255]]
```

The test history index field (values like 2828, 2900, etc.) is typed as `uint8` (0-255) in the YANG model, but actual values exceed 255. This causes all RESTCONF GET operations for PM operational/all data to fail with HTTP 500.

---

## Recommendations

1. **BUG A Fix (datapath):** `Proactive.cpp:219 StartSession()` should NOT allocate an OperDB result-idx when returning status 2. If a result-idx is allocated, the code must ensure `DoneSession()` is called to write an End time, or the entry should be cleaned up immediately.
2. **BUG B Fix (CLI):** `show_services_commands.py:1060` should handle `None` start_time in the sort:
   ```python
   measurements.sort(key=lambda el: el[1].start_time or "")
   ```
   Or filter out entries with None start_time before rendering.
3. **Validation:** Consider adding a commit validation that warns when `repeat-interval < test-duration`.
4. **YANG Fix:** Change the test index field type from `uint8` to `uint32` or larger.
