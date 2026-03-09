# Y.1731 On-Demand Stop Test (SW-237984)

Automated CLI testing for **`request ethernet-oam cfm on-demand stop`** on DNOS devices via SSH.

| Field | Value |
|-------|-------|
| **Jira** | [SW-237984](https://drivenets.atlassian.net/browse/SW-237984) |
| **Epic** | [SW-141523](https://drivenets.atlassian.net/browse/SW-141523) (Ethernet OAM Y.1731) |
| **Script** | `on_demand_stop_test.py` |
| **Based on** | `y1731_cli_tab_test.py` (SSH helpers, CFM discovery) |

---

## Overview

This script validates the full lifecycle of Y.1731 on-demand stop functionality via the CLI. It covers starting on-demand sessions with both **mep-id** and **mac-address** targets, stopping them using every supported stop variant, and verifying sessions are properly cleared with deep output validation.

```
Test Host <--SSH (paramiko)--> DNOS Device
  (SSH1: start sessions)
  (SSH2: stop & show commands)
  (SSH3: streaming session for crash test)
```

### What it tests

| Phase | Test | Description |
|-------|------|-------------|
| 1-2 | **Start & Verify (mep-id)** | Start DM + SLM with `target mep-id`, verify running via `show on-demand` |
| 1c | **Start & Verify (mac DM)** | Start DM with `target mac-address`, verify in `show on-demand`, stop and confirm |
| 1d | **Start & Verify (mac LB)** | Start LB with `target mac-address`, verify in `show on-demand`, stop and confirm |
| 3 | **Show Running** | Validate `show on-demand` and `show on-demand detail` output contains MD, MA, test types, operational keywords |
| 4 | **Stop (bare)** | `request ethernet-oam cfm on-demand stop` (without qualifiers), validate output |
| 5 | **Verify Cleared** | Confirm sessions cleared in show, counters do not increment after stop |
| 6 | **Stop Variants (mep-id)** | All stop forms with mep-id targets: `stop all`, `stop md+ma+type`, `stop test-type` for DM, SLM, LB, LT |
| 6 | **Stop Variants (mac)** | All stop forms with mac-address targets: `stop all`, `stop md+ma+type`, `stop test-type` for DM, SLM, LB, LT |
| 6 | **Multi-Session Stops** | Start multiple sessions (mep, mac, mixed) and stop all at once |
| 6 | **Stop While Streaming** | Stop session while `show on-demand` runs on a third SSH channel -- verify no crash |
| 7 | **Stop No Active** | Issue every stop variant when no sessions are running -- verify graceful handling |
| 8 | **Restart After Stop (mep-id)** | Start DM and SLM with mep-id target after previous stop, verify they appear in show |
| 8 | **Restart After Stop (mac)** | Start DM and LB with mac-address target after previous stop, verify they appear in show |
| Neg | **Unreachable Target** | Start DM to unreachable MAC, stop, verify no stale entries remain |
| Long | **Longevity Cycles** | Repeated start/stop cycles alternating mep-id and mac-address targets |
| Long | **Post-Longevity Health** | Verify device can still run new sessions after longevity test |

### Target types tested

| Target Type | Command Format | Session Types |
|-------------|---------------|---------------|
| **mep-id** | `target mep-id <N>` | DM, SLM, LB, LT |
| **mac-address** | `target mac-address <MAC>` | DM, SLM, LB, LT |

### Stop command variants tested

| Stop Variant | Command |
|-------------|---------|
| Bare | `request ethernet-oam cfm on-demand stop` |
| All | `request ethernet-oam cfm on-demand stop all` |
| MD+MA+Type | `request ethernet-oam cfm on-demand stop maintenance-domain <MD> maintenance-association <MA> test-type <TYPE>` |
| Test-Type only | `request ethernet-oam cfm on-demand stop test-type <TYPE>` |

### Test types and their behavior

| Test Type | CLI Name | Persistent? | Notes |
|-----------|----------|-------------|-------|
| Delay Measurement (DM) | `delay-measurement two-way` | Yes | Runs until explicitly stopped |
| Loopback (LB) | `loopback` | Yes | Runs until explicitly stopped |
| Synthetic Loss (SLM) | `synthetic-loss-measurement` | No (transient) | Completes after a few probes; may finish before stop |
| Linktrace (LT) | `linktrace` | No (transient) | Completes after a few hops; may finish before stop |

---

## Prerequisites

### Python packages

```bash
pip install paramiko
```

### Lab environment

- **DNOS device** with SSH enabled (default credentials: `dnroot/dnroot`)
- **CFM/Y.1731 configured** on the device -- at least one Maintenance Domain, Maintenance Association, and MEP pair
- The script auto-discovers MD/MA/MEP/target from the device config, or you can override manually

---

## How to Run

### Basic usage (auto-discover everything)

```bash
python3 on_demand_stop_test.py
```

The script will prompt for the device hostname/IP.

### Specify host directly

```bash
python3 on_demand_stop_test.py --host 10.10.5.50
```

### Override MD/MA/target and set custom MAC target

```bash
python3 on_demand_stop_test.py --host 10.10.5.50 \
    --md MD-CUST --ma MA-CUST \
    --target "mep-id 2" \
    --target-mac 00:11:22:33:44:55
```

### Show detailed output and progress

```bash
python3 on_demand_stop_test.py --host 10.10.5.50 --show-details --show-progress
```

### Save raw CLI output to file

```bash
python3 on_demand_stop_test.py --host 10.10.5.50 --output-file results.txt
```

### Skip longevity and unreachable tests (quick run)

```bash
python3 on_demand_stop_test.py --host 10.10.5.50 --skip-longevity --skip-unreachable
```

---

## CLI Arguments

| Argument | Default | Description |
|----------|---------|-------------|
| `--host` | (prompted) | Device hostname or IP |
| `--user` | `dnroot` | SSH username |
| `--password` | `dnroot` | SSH password |
| `--timeout` | `30` | SSH timeout (seconds) |
| `--auto-from-cfm` | `true` | Auto-discover MD/MA/MEP/target from device config |
| `--md` | auto / `MD-CUST` | Override maintenance-domain name |
| `--ma` | auto / `MA-CUST` | Override maintenance-association name |
| `--mep-id` | auto / `1` | Override local MEP ID |
| `--target` | auto / `mep-id 2` | Override mep-id target |
| `--target-mac` | `00:11:22:33:44:55` | MAC address for mac-address target tests |
| `--settle-time` | `5` | Seconds to wait after start before stop |
| `--counter-wait` | `5` | Seconds to wait after stop before checking counters |
| `--longevity-cycles` | `5` | Number of start/stop cycles for longevity |
| `--skip-longevity` | `false` | Skip longevity test |
| `--skip-unreachable` | `false` | Skip unreachable target test |
| `--unreachable-mac` | `FF:FF:FF:00:00:01` | MAC address for unreachable target test |
| `--show-details` | `false` | Show detailed results per test |
| `--show-progress` | `false` | Print running test names |
| `--show-cli-output` | `false` | Print raw CLI output |
| `--output-file` | none | Save raw output to file |
| `--output-format` | `table` | Output format: `table` or `list` |

---

## Output Validation

The script performs **deep output validation**, not just CLI error checking:

- **Stop output**: Parses `Stopped tests: N` count, validates against expected minimum, detects `No ongoing sessions`
- **Show running**: Validates MD name, MA name, expected test types (delay-measurement, synthetic-loss, loopback, linktrace), and operational indicators
- **Show stopped ("invalid" check)**: After a session is stopped, the `show on-demand` output **MUST** contain the word **"invalid"** to indicate the session is no longer valid. If the MD/MA is still present in the output but "invalid" is missing, the test **FAILS** (this indicates the stopped session was not correctly marked as invalid). Accepted outcomes: (1) `No ongoing sessions` / empty table -- PASS; (2) Sessions listed with `invalid` -- PASS; (3) Sessions listed without `invalid` -- FAIL; (4) Active running indicators -- FAIL
- **Counter verification**: Extracts numeric counters before and after stop, verifies they do not increment
- **Transient handling**: SLM and LT tests may complete before stop arrives; the script accepts `No ongoing sessions` or `Stopped tests: 0` as valid outcomes for these types
- **Streaming crash detection**: Verifies the show CLI session does not crash or produce errors when a concurrent stop is issued

---

## Expected Test Count

| Category | Approximate Tests |
|----------|-------------------|
| Setup / Discovery | 3 |
| Start & Verify (mep-id + mac) | ~10 |
| Stop & Counters | 4-5 |
| Stop Variants (mep-id) | ~22 |
| Stop Variants (mac) | ~22 |
| Multi-session / Streaming | ~6 |
| No Active Sessions | 8 |
| Restart After Stop (mep + mac) | ~8 |
| Negative / Longevity | 4-5 |
| **Total** | **~75+** |

---

## Known Behaviors

1. **SLM/LT transient**: These test types send a fixed number of probes and auto-complete. The stop command may report `No ongoing sessions` if they finished before the stop arrives. This is expected behavior.
2. **`show on-demand detail` unsupported on some builds**: The script falls back to `show on-demand` (without `detail`) if the device rejects the keyword.
3. **Bare stop output format**: `request ethernet-oam cfm on-demand stop` (without `all`) may not include `Stopped tests: N` in its output. The script accepts any non-error response.
4. **MAC-address target**: The `--target-mac` should be the MAC of a reachable remote MEP. If unreachable, the session will start but DM/SLM probes will fail (the stop should still work).
e the MAC of a reachable remote MEP. If unreachable, the session will start but DM/SLM probes will fail (the stop should still work).
