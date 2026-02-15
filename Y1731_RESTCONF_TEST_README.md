# Y.1731 RESTCONF Sanity Test

Automated RESTCONF testing for **Y.1731 Performance Monitoring** on DNOS devices via **OpenDaylight (ODL)**.

| Field | Value |
|-------|-------|
| **Jira** | [SW-237067](https://drivenets.atlassian.net/browse/SW-237067) |
| **Epic** | [SW-141523](https://drivenets.atlassian.net/browse/SW-141523) (Ethernet OAM Y.1731 - Proactive PM) |
| **Script** | `y1731_restconf_test.py` |
| **ODL Ref** | [Confluence - ODL RESTCONF practical usage](https://drivenets.atlassian.net/wiki/spaces/QA/pages/5353865217) |

---

## Overview

This script validates that Y.1731 Performance Monitoring configuration can be managed end-to-end through the RESTCONF interface. It covers the full CRUD lifecycle:

```
Device (DNOS) <--NETCONF--> OpenDaylight (ODL) <--RESTCONF--> This Script
                             10.10.75.34:8181
```

### What it tests

| Phase | Operation | Description |
|-------|-----------|-------------|
| 1 | **Setup** | Mount device to ODL, discover YANG namespaces via `display-xml`, discover CFM MEP context |
| 2 | **GET** | Retrieve PM config, operational, and combined data via RESTCONF |
| 3 | **PATCH DM** | Create a Delay Measurement profile + session, verify via GET and CLI |
| 4 | **PATCH SLM** | Create a Synthetic Loss Measurement profile + session, verify via GET and CLI |
| 5 | **Modify** | Modify DM profile threshold (delay-rtt-min 100 -> 200), verify via GET and CLI |
| 6 | **DELETE** | Remove all test artifacts (sessions then profiles), verify removal via GET and CLI |
| 7 | **Negative** | Invalid YANG path, malformed XML, invalid threshold value -- all expect errors |
| 8 | **Cleanup** | Unmount device from ODL (optional, requires `--cleanup`) |

**Total: 32 test cases** across 8 phases.

---

## Prerequisites

### Python packages

```bash
pip install paramiko requests
```

### Lab environment

- **ODL server** running and accessible (default: `10.10.75.34:8181`, credentials: `admin/admin`)
- **DNOS device** with SSH and NETCONF enabled (default credentials: `dnroot/dnroot`)
- **CFM/Y.1731 configured** on the device -- the script needs at least one Maintenance Domain, Maintenance Association, and MEP pair. The script will auto-discover these from the device config, or you can provide them explicitly.

---

## How to Run

### Basic usage (auto-discover everything)

```bash
python3 y1731_restconf_test.py --host 192.168.174.101
```

This will:
1. Mount the device to ODL
2. SSH into the device to discover YANG namespaces and CFM context
3. Run all 32 test cases
4. Print a PASS/FAIL summary

### With explicit CFM context

```bash
python3 y1731_restconf_test.py --host 192.168.174.101 \
    --md-name MD1 --ma-name MA1 \
    --source-mep-id 1 --target-mep-id 2
```

### Device already mounted in ODL

```bash
python3 y1731_restconf_test.py --host 192.168.174.101 --skip-mount
```

### Custom ODL server

```bash
python3 y1731_restconf_test.py --host 192.168.174.101 \
    --odl-host 10.10.75.50 --odl-port 8181 \
    --odl-user admin --odl-password admin
```

### RESTCONF-only (skip SSH/CLI verification)

```bash
python3 y1731_restconf_test.py --host 192.168.174.101 --no-ssh-verify
```

### Full run with cleanup (unmount device after tests)

```bash
python3 y1731_restconf_test.py --host 192.168.174.101 --cleanup
```

---

## All CLI Arguments

| Argument | Default | Description |
|----------|---------|-------------|
| `--host` | **(required)** | Device management IP address |
| `--user` | `dnroot` | Device SSH/NETCONF username |
| `--password` | `dnroot` | Device SSH/NETCONF password |
| `--odl-host` | `10.10.75.34` | OpenDaylight server IP |
| `--odl-port` | `8181` | OpenDaylight server port |
| `--odl-user` | `admin` | ODL username |
| `--odl-password` | `admin` | ODL password |
| `--node-name` | auto (from IP) | Device mount name in ODL (e.g. `192_168_174_101`) |
| `--md-name` | auto-discovered | Maintenance Domain name |
| `--ma-name` | auto-discovered | Maintenance Association name |
| `--source-mep-id` | auto-discovered | Source MEP ID |
| `--target-mep-id` | auto-discovered | Target MEP ID |
| `--skip-mount` | off | Skip ODL mount (device already mounted) |
| `--no-ssh-verify` | off | Skip all SSH/CLI verification steps |
| `--cleanup` | off | Unmount device from ODL after tests |

---

## Output

The script prints real-time results for each test:

```
======================================================================
  Y.1731 RESTCONF SANITY TEST
  Device   : 192.168.174.101
  ODL      : 10.10.75.34:8181
  Node     : 192_168_174_101
  Jira     : SW-237067
  Started  : 2026-02-15 10:30:00
======================================================================

============================================================
PHASE 1.1: Mount device to ODL
============================================================
  [PASS] mount_device -- HTTP 201

...

======================================================================
  FULL RESULTS
======================================================================
  [PASS] mount_device -- HTTP 201
  [PASS] verify_mount_status -- Device connected
  [PASS] discover_yang_paths -- pm=http://drivenets.com/ns/yang/dn-pm
  ...

----------------------------------------------------------------------
  SUMMARY
----------------------------------------------------------------------
  Total : 32
  Passed: 32
  Failed: 0
  Time  : 45.2s
======================================================================

  >>> ALL TESTS PASSED <<<
```

Exit code: `0` if all tests pass, `1` if any fail.

---

## Architecture

### RESTCONF Flow

```
Script                   ODL (10.10.75.34:8181)           DNOS Device
  |                            |                              |
  |-- PUT /restconf/config --> |                              |
  |   (mount device)          |-- NETCONF connect ---------> |
  |                            |                              |
  |-- GET /rests/data -------> |                              |
  |   (read PM config)       |-- NETCONF get-config -------> |
  |                            |<---- config XML ------------ |
  |<---- JSON response ------- |                              |
  |                            |                              |
  |-- PATCH /rests/data -----> |                              |
  |   (create DM profile)    |-- NETCONF edit-config -------> |
  |                            |<---- ok -------------------- |
  |<---- 200 OK -------------- |                              |
  |                            |                              |
  |-- SSH (verify via CLI) ---------------------------------> |
  |<---- show config output --------------------------------- |
```

### Key URLs

| Operation | URL Pattern |
|-----------|-------------|
| Mount | `PUT http://ODL:8181/restconf/config/network-topology:network-topology/topology/topology-netconf/node/{name}` |
| GET data | `GET http://ODL:8181/rests/data/.../node={name}/yang-ext:mount/{yang-path}?content=config` |
| PATCH data | `PATCH http://ODL:8181/rests/data/.../node={name}/yang-ext:mount/dn-top:drivenets-top` |
| Unmount | `DELETE http://ODL:8181/restconf/config/.../node/{name}` |

### YANG Namespace Discovery

The script dynamically discovers YANG namespaces by running:
```
show config services performance-monitoring | display-xml | no-more
```
This produces XML with `xmlns` declarations that map to the correct RESTCONF paths:
- `dn-top` -> `http://drivenets.com/ns/yang/dn-top`
- `dn-services` -> `http://drivenets.com/ns/yang/dn-services`
- `dn-pm` -> `http://drivenets.com/ns/yang/dn-performance-monitoring`

### Test Artifacts

All config created by the test uses unique prefixed names to avoid collision:

| Artifact | Name |
|----------|------|
| DM Profile | `RESTCONF_DM_PROF` |
| DM Session | `RESTCONF_DM_SESS` |
| SLM Profile | `RESTCONF_SLM_PROF` |
| SLM Session | `RESTCONF_SLM_SESS` |

These are all cleaned up in Phase 6 (DELETE).

---

## Extending for Other Features

The script is designed to be extensible. To add RESTCONF tests for another feature:

1. Add new XML body builder methods (e.g. `_build_new_feature_xml()`)
2. Add test methods following the pattern: `_do_patch()` -> `_do_verify_get()` -> `_do_verify_cli()`
3. Add them to `run_all()` in the orchestrator
4. Update the `_del_xml()` method to handle cleanup of new artifacts
