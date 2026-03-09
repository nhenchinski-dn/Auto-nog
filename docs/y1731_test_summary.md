# Y.1731 CLI Test Script - Complete Coverage Summary

**Script:** `y1731_cli_tab_test.py`  
**Epic:** [SW-141523](https://drivenets.atlassian.net/browse/SW-141523) - Ethernet OAM Y.1731 - Proactive Initiator: Performance Monitoring [PM]  
**Generated:** February 8, 2026

---

## Overview

This script comprehensively tests the CLI implementation of Y.1731 Proactive Performance Monitoring (DM/SLM) features, including:
- TAB completion at all configuration levels
- Session and profile creation with all parameter variants
- Operational state verification
- Show command functionality with filters
- Historic test results (N-list)
- System event notifications (threshold violations)
- On-demand test start/stop operations
- Negative validation tests
- Dependency deletion protection

**Total Test Steps:** 80+ (scales with `--all-meps` for per-MEP iteration)

---

## Test Categories

### 1. Discovery & Setup

| Test Name | Description | Jira Reference |
|-----------|-------------|----------------|
| `discover_all_local_meps` | Discovers all local MEPs from device ethernet-oam CFM config (when `--all-meps` is used) | Prerequisite |
| `discover_cfm_context` | Auto-discovers MD, MA, MEP-ID, target from `show config services ethernet-oam connectivity-fault-management` | Prerequisite |
| `discover_source_mep_id` | Validates discovered MEP-ID is accepted by CLI via TAB completion and source command validation | Prerequisite |
| `discover_dm_target_mep_id` | Discovers valid DM target MEP-IDs via TAB completion | Prerequisite |

---

### 2. TAB Completion - Sessions

| Test Name | Description | Jira Reference |
|-----------|-------------|----------------|
| `tab_completion: services performance-monitoring cfm two-way-delay-measurement` | TAB completes at DM session level | SW-206808 |
| `tab_completion: ... two-way-delay-measurement <SESSION>` | TAB completes DM session subcommands (admin-state, description, profile, source, target) | SW-206808 |
| `tab_completion: ... two-way-delay-measurement <SESSION> description` | TAB completes DM description field | SW-206808 |
| `tab_completion: ... two-way-synthetic-loss-measurement` | TAB completes at SLM session level | SW-206813 |
| `tab_completion: ... two-way-synthetic-loss-measurement <SESSION>` | TAB completes SLM session subcommands | SW-206813 |
| `tab_completion: ... two-way-synthetic-loss-measurement <SESSION> description` | TAB completes SLM description field | SW-206813 |
| `tab_completion: ... profiles cfm two-way-delay-measurement` | TAB completes at DM profile level | SW-206817 |
| `tab_completion: ... profiles cfm two-way-synthetic-loss-measurement` | TAB completes at SLM profile level | SW-206829 |

---

### 3. TAB Completion - Profile Subcommands ⭐ NEW

| Test Name | Description | Jira Reference |
|-----------|-------------|----------------|
| `tab_completion_profile: ... profiles cfm two-way-delay-measurement <PROFILE>` | TAB completes DM profile knobs (inform-test-results, test-duration, thresholds) | SW-206817 |
| `tab_completion_profile: ... <PROFILE> thresholds` | TAB completes DM threshold options (delay-rtt-min/avg/max, jitter-rtt-avg/max, success-rate) | SW-206822 |
| `tab_completion_profile: ... <PROFILE> test-duration` | TAB completes DM test-duration variants (count, time-frame, non-stop) | SW-206819, SW-206820, SW-206821 |
| `tab_completion_profile: ... profiles cfm two-way-synthetic-loss-measurement <PROFILE>` | TAB completes SLM profile knobs (inform-test-results, pcp, test-duration, thresholds) | SW-206829 |
| `tab_completion_profile: ... <PROFILE> thresholds` | TAB completes SLM threshold options (near-end-loss, far-end-loss) | SW-206834 |
| `tab_completion_profile: ... <PROFILE> test-duration` | TAB completes SLM test-duration variants (count, time-frame, non-stop) | SW-206831, SW-206832, SW-206833 |

---

### 4. DM Session Configuration & Commit

| Test Name | Description | Jira Reference |
|-----------|-------------|----------------|
| `configure_dm_session` | Creates DM profile with thresholds and test-duration, creates DM session with admin-state, description, profile reference, source (md/ma/mep-id), target (mep-id) | SW-206807, SW-206808, SW-206809, SW-206810, SW-206812 |
| `commit` | Executes `commit` and verifies success (no CLI errors) | SW-198233 |
| `verify_dm_config_present` | Verifies DM session appears in `show config services performance-monitoring` | SW-206808 |
| `verify_dm_profile_config_present` | Verifies DM profile appears in `show config services performance-monitoring` | SW-206817 |

---

### 5. SLM Session Configuration & Commit

| Test Name | Description | Jira Reference |
|-----------|-------------|----------------|
| `configure_slm_session` | Creates SLM profile with thresholds, test-duration, and PCP; creates SLM session with admin-state, description, profile, source, target | SW-206813, SW-206814, SW-206815, SW-206816, SW-206829 |
| `commit_slm` | Verifies SLM configuration commit succeeds | SW-198233 |
| `verify_slm_config_present` | Verifies SLM session appears in `show config` | SW-206813 |
| `verify_slm_profile_config_present` | Verifies SLM profile appears in `show config` | SW-206829 |

---

### 6. DM Profile Variants (SW-235372)

| Test Name | Description | Jira Reference |
|-----------|-------------|----------------|
| `sw235372_dm_profile_probes` | DM profile with `test-duration count` (probe-count, probe-interval, repeat-interval) | SW-206819 |
| `sw235372_dm_profile_time_frame` | DM profile with `test-duration time-frame` (minutes, probe-interval, repeat-interval) | SW-206820 |
| `sw235372_dm_profile_non_stop` | DM profile with `test-duration non-stop` (probe-interval, computation-interval) | SW-206821 |
| `sw235372_dm_profile_probes_<N>` | DM profile with varying probe-count values (1, 10, 100, 1000) | SW-206819 |
| `sw235372_dm_profile_non_stop_ci_<N>` | DM profile with varying computation-interval values (15s, 60s, 300s, 900s) | SW-206821 |
| `sw235372_dm_profile_thresh_delay_rtt_min` | DM profile with `thresholds delay-rtt-min` only | SW-206824 |
| `sw235372_dm_profile_thresh_delay_rtt_avg` | DM profile with `thresholds delay-rtt-avg` only | SW-206825 |
| `sw235372_dm_profile_thresh_delay_rtt_max` | DM profile with `thresholds delay-rtt-max` only | SW-206826 |
| `sw235372_dm_profile_thresh_jitter_rtt_avg` | DM profile with `thresholds jitter-rtt-avg` only | SW-206827 |
| `sw235372_dm_profile_thresh_jitter_rtt_max` | DM profile with `thresholds jitter-rtt-max` only | SW-206828 |
| `sw235372_dm_profile_thresh_success_rate` | DM profile with `thresholds success-rate` only | SW-206822 |
| `sw235372_dm_profile_thresh_combo` | DM profile with all 6 thresholds combined | SW-206822 |
| `sw235372_dm_profile_inform_disabled` | DM profile with `inform-test-results disabled` ⭐ NEW | SW-206818 |

---

### 7. SLM Profile Variants (SW-235372)

| Test Name | Description | Jira Reference |
|-----------|-------------|----------------|
| `sw235372_slm_profile_probes` | SLM profile with `test-duration count` (probe-count, probe-interval, repeat-interval) | SW-206831 |
| `sw235372_slm_profile_time_frame` | SLM profile with `test-duration time-frame` (minutes, probe-interval, repeat-interval) | SW-206832 |
| `sw235372_slm_profile_non_stop` | SLM profile with `test-duration non-stop` (probe-interval, computation-interval) | SW-206833 |
| `sw235372_slm_profile_probes_<N>` | SLM profile with varying probe-count values (1, 10, 100, 1000) | SW-206831 |
| `sw235372_slm_profile_non_stop_ci_<N>` | SLM profile with varying computation-interval values (15s, 60s, 300s, 900s) | SW-206833 |
| `sw235372_slm_profile_pcp_<N>` | SLM profile with various PCP values (1, 2, 3, 4, 5, 6, 7) | SW-206829 |
| `sw235372_slm_profile_thresh_near_end` | SLM profile with `thresholds near-end-loss` only | SW-206834 |
| `sw235372_slm_profile_thresh_far_end` | SLM profile with `thresholds far-end-loss` only | SW-206835 |
| `sw235372_slm_profile_thresh_combo` | SLM profile with both near-end and far-end loss thresholds | SW-206834, SW-206836 |
| `sw235372_slm_profile_inform_disabled` | SLM profile with `inform-test-results disabled` ⭐ NEW | SW-206830 |

---

### 8. PCP Boundary Testing ⭐ NEW

| Test Name | Description | Jira Reference |
|-----------|-------------|----------------|
| `sw235372_slm_profile_pcp_0` | SLM profile with `pcp 0` (valid low boundary, uses `commit check`) | SW-206829 |
| `sw235372_slm_profile_pcp_7` | SLM profile with `pcp 7` (valid high boundary, uses `commit check`) | SW-206829 |
| `negative_slm_pcp_invalid` | SLM profile with `pcp 8` (invalid, expects CLI error) | SW-206829 |

---

### 9. DM Session Variants (SW-235372)

| Test Name | Description | Jira Reference |
|-----------|-------------|----------------|
| `sw235372_dm_session_target_mep` | DM session with `target mep-id <ID>`, toggles `admin-state enabled` → `disabled` | SW-206808, SW-206809, SW-206810 |
| `sw235372_dm_session_target_mac` | DM session with `target mac-address 00:11:22:33:44:55` | SW-206810 |

---

### 10. SLM Session Variants (SW-235372)

| Test Name | Description | Jira Reference |
|-----------|-------------|----------------|
| `sw235372_slm_session_target_mep` | SLM session with `target mep-id <ID>`, toggles `admin-state enabled` → `disabled` | SW-206813, SW-206814, SW-206815 |
| `sw235372_slm_session_target_mac` | SLM session with `target mac-address 00:11:22:33:44:55` | SW-206815 |

---

### 11. Show Proactive Results

| Test Name | Description | Jira Reference |
|-----------|-------------|----------------|
| `show_slm_proactive` | Runs `show services performance-monitoring cfm tests proactive` and verifies SLM session name appears in output | SW-206837 |
| `show_dm_proactive` | Runs `show services performance-monitoring cfm tests proactive` and verifies DM session name appears in output | SW-206837 |

---

### 12. On-Demand Start + Stop Tests (SW-237984)

| Test Name | Description | Jira Reference |
|-----------|-------------|----------------|
| `on_demand_disable_proactive` | Sets DM/SLM sessions to `admin-state disabled` to allow on-demand tests | SW-198125 |
| `on_demand_dm_mep_stop_all` | SSH1: `run ... on-demand delay-measurement ... target mep-id`, SSH2: `request ... stop all` | SW-198125 |
| `on_demand_dm_mac_stop_all` | SSH1: `run ... on-demand delay-measurement ... target mac-address`, SSH2: `request ... stop all` | SW-198125 |
| `on_demand_slm_mep_stop_md` | SSH1: `run ... on-demand synthetic-loss-measurement ... target mep-id`, SSH2: `request ... stop maintenance-domain <MD> maintenance-association <MA>` | SW-198125 |
| `on_demand_slm_mac_stop_md` | SSH1: `run ... on-demand synthetic-loss-measurement ... target mac-address`, SSH2: `request ... stop maintenance-domain <MD> maintenance-association <MA>` | SW-198125 |
| `on_demand_lt_mep_stop_md` | SSH1: `run ... on-demand linktrace ... target mep-id`, SSH2: `request ... stop maintenance-domain <MD> maintenance-association <MA>` | SW-198125 |
| `on_demand_dm_mep_stop_type` | SSH1: `run ... on-demand delay-measurement`, SSH2: `request ... stop test-type delay-measurement` | SW-198125 |
| `on_demand_slm_mep_stop_type` | SSH1: `run ... on-demand synthetic-loss-measurement`, SSH2: `request ... stop test-type synthetic-loss-measurement` | SW-198125 |
| `on_demand_lb_mep_stop_type` | SSH1: `run ... on-demand loopback ... target mep-id`, SSH2: `request ... stop test-type loopback` | SW-198125 |
| `on_demand_lb_mac_stop_type` | SSH1: `run ... on-demand loopback ... target mac-address`, SSH2: `request ... stop test-type loopback` | SW-198125 |
| `on_demand_lt_mep_stop_type` | SSH1: `run ... on-demand linktrace ... target mep-id`, SSH2: `request ... stop test-type linktrace` | SW-198125 |
| `on_demand_lt_mac_stop_type` | SSH1: `run ... on-demand linktrace ... target mac-address`, SSH2: `request ... stop test-type linktrace` | SW-198125 |
| `on_demand_all_stop_all` | SSH1: Starts DM, SLM, loopback, linktrace concurrently (4 tests), SSH2: `request ... stop all` (verifies count=4) | SW-198125 |
| `on_demand_reenable_proactive` | Sets DM/SLM sessions back to `admin-state enabled` after on-demand tests | SW-198125 |

---

### 13. Show Commands Testing ⭐ NEW

| Test Name | Description | Jira Reference |
|-----------|-------------|----------------|
| `show_cfm_tests_summary` | `show services performance-monitoring cfm tests` -- verifies session name in summary output | SW-206837 |
| `show_cfm_tests_proactive` | `show ... cfm tests proactive` -- verifies proactive sessions are listed | SW-206837 |
| `show_cfm_tests_proactive_dm` | `show ... cfm tests proactive two-way-delay-measurement` -- DM type filter | SW-206837 |
| `show_cfm_tests_proactive_slm` | `show ... cfm tests proactive two-way-synthetic-loss-measurement` -- SLM type filter | SW-206837 |
| `show_cfm_tests_dm_detail` | `show ... cfm tests proactive two-way-delay-measurement detail` -- verifies MD, MA, MEP-ID in detailed output | SW-206837 |
| `show_cfm_tests_slm_detail` | `show ... cfm tests proactive two-way-synthetic-loss-measurement detail` -- verifies MD, MA, MEP-ID for SLM | SW-206837 |
| `show_cfm_tests_filter_session` | `show ... cfm tests session-name <SESSION>` -- filter by session name | SW-206837 |
| `show_cfm_tests_filter_md` | `show ... cfm tests md-name <MD>` -- filter by maintenance domain | SW-206837 |
| `show_cfm_tests_filter_ma` | `show ... cfm tests ma-name <MA>` -- filter by maintenance association | SW-206837 |
| `show_cfm_tests_filter_mep` | `show ... cfm tests mep-id <MEP>` -- filter by MEP ID | SW-206837 |

---

### 14. Operational State Verification ⭐ NEW

| Test Name | Description | Jira Reference |
|-----------|-------------|----------------|
| `verify_dm_operational_state` | Runs `show ... cfm tests proactive two-way-delay-measurement detail` and verifies session is operationally running (checks for "enabled", "Ongoing", "active", etc.) | SW-206419 |
| `verify_slm_operational_state` | Runs `show ... cfm tests proactive two-way-synthetic-loss-measurement detail` and verifies SLM session is operationally running | SW-206421 |
| `verify_session_param_change` | Changes DM session description via CLI, verifies change appears in `show` output, then restores original description | SW-206419 |

---

### 15. Historic Test Results (N-list) ⭐ NEW

| Test Name | Description | Jira Reference |
|-----------|-------------|----------------|
| `verify_historic_results` | Waits `--wait-for-results` seconds for probes to complete, then runs detailed `show` commands for DM and checks for historic result indicators ("Index", "Historical Test Results", timestamps, counters) | SW-206804 |
| `verify_slm_historic_results` | Same for SLM historic test results | SW-206804 |

---

### 16. System Event Testing ⭐ NEW

| Test Name | Description | Jira Reference |
|-----------|-------------|----------------|
| `system_event_cfm_proactive_test_failure` | Creates temporary DM profile with guaranteed low threshold violation (`delay-rtt-max 1` microsecond), commits, waits `--low-threshold-wait` seconds for probes, checks syslog + `show system events` for `CFM_PROACTIVE_TEST_FAILURE` event, verifies event content contains session name and test type | SW-207209 |
| `cleanup_low_threshold` | Removes the low-threshold test profile and session | SW-207209 |

---

### 17. Dependency Deletion Rejection ⭐ NEW

| Test Name | Description | Jira Reference |
|-----------|-------------|----------------|
| `negative_delete_cfm_dependency` | While proactive DM session references a CFM MD/MA/MEP-ID, attempts to delete that MD/MA/mep-id from `services ethernet-oam connectivity-fault-management`, runs `commit check` expecting failure (dependency violation), then uses `rollback 0` to revert | SW-198127 |

---

### 18. Negative Tests

| Test Name | Description | Jira Reference |
|-----------|-------------|----------------|
| `negative_slm_long_name_desc` | SLM session with 220+ character name and 512+ character description (expects CLI error) | SW-235372 |
| `negative_slm_bad_md_ma` | SLM session with non-existent MD/MA (expects `commit check` failure) | SW-235372 |
| `negative_long_name_desc` | DM session with oversized name and description (expects CLI error) | SW-235372 |
| `negative_bad_md_ma` | DM session with invalid MD/MA (expects `commit check` failure) | SW-235372 |
| `negative_non_numeric` | DM session with `source mep-id abc` and `target mep-id abc` (expects CLI error) | SW-235372 |
| `negative_slm_invalid_pcp` | SLM profile with `pcp 8` or other invalid PCP (expects CLI error) | SW-235372 |
| `negative_dm_invalid_timer` | DM profile with invalid timer values (e.g., probe-interval 0, computation-interval 1) (expects CLI error) | SW-235372 |

---

### 19. Cleanup

| Test Name | Description | Jira Reference |
|-----------|-------------|----------------|
| `cleanup` | Removes all created DM/SLM sessions and profiles (including per-MEP iterations), runs `commit`, verifies removal via `show config` | All tests |

---

## Jira Issue Coverage

| Jira Issue | Description | Test Coverage |
|------------|-------------|---------------|
| **SW-141523** | Epic: Ethernet OAM Y.1731 - Proactive Initiator: Performance Monitoring [PM] | Overall scope, all tests validate this epic |
| **SW-206807** | CLI-config: PM cfm-sessions | Session creation, configuration |
| **SW-206808** | CLI-config: DM session | DM session + TAB completion + configuration tests |
| **SW-206809** | CLI-config: DM admin-state | admin-state enabled/disabled toggles |
| **SW-206810** | CLI-config: DM target | target mep-id + mac-address variants |
| **SW-206812** | CLI-config: DM profile reference | Profile attachment to DM session |
| **SW-206813** | CLI-config: SLM session | SLM session + TAB completion + configuration tests |
| **SW-206814** | CLI-config: SLM admin-state | admin-state enabled/disabled toggles |
| **SW-206815** | CLI-config: SLM target | target mep-id + mac-address variants |
| **SW-206816** | CLI-config: SLM profile reference | Profile attachment to SLM session |
| **SW-206817** | CLI-config: DM profile creation | DM profile + TAB completion at profile level |
| **SW-206818** | CLI-config: DM inform-test-results | `inform-test-results enabled/disabled` |
| **SW-206819** | CLI-config: DM test-duration count | `test-duration count` (probes variant) with probe-count variations |
| **SW-206820** | CLI-config: DM test-duration time-frame | `test-duration time-frame` variant |
| **SW-206821** | CLI-config: DM test-duration non-stop | `test-duration non-stop` with computation-interval variations |
| **SW-206822** | CLI-config: DM thresholds | All 6 DM thresholds (delay-rtt-min/avg/max, jitter-rtt-avg/max, success-rate) |
| **SW-206824** | CLI-config: DM threshold delay-rtt-min | delay-rtt-min individual test |
| **SW-206825** | CLI-config: DM threshold delay-rtt-avg | delay-rtt-avg individual test |
| **SW-206826** | CLI-config: DM threshold delay-rtt-max | delay-rtt-max individual test |
| **SW-206827** | CLI-config: DM threshold jitter-rtt-avg | jitter-rtt-avg individual test |
| **SW-206828** | CLI-config: DM threshold jitter-rtt-max | jitter-rtt-max individual test |
| **SW-206829** | CLI-config: SLM profile creation | SLM profile + PCP + TAB completion at profile level |
| **SW-206830** | CLI-config: SLM inform-test-results | `inform-test-results enabled/disabled` |
| **SW-206831** | CLI-config: SLM test-duration count | `test-duration count` (probes variant) with probe-count variations |
| **SW-206832** | CLI-config: SLM test-duration time-frame | `test-duration time-frame` variant |
| **SW-206833** | CLI-config: SLM test-duration non-stop | `test-duration non-stop` with computation-interval variations |
| **SW-206834** | CLI-config: SLM thresholds | near-end-loss and far-end-loss thresholds |
| **SW-206835** | CLI-config: SLM threshold far-end-loss | far-end-loss individual test |
| **SW-206836** | CLI-config: SLM threshold combinations | Combined threshold tests |
| **SW-206837** | CLI-show: show cfm tests | 10+ show command variants (summary, proactive, detail, filters by session/MD/MA/MEP) |
| **SW-206419** | Feature: Proactive two-way ETH-DM sessions | Operational state verification for DM |
| **SW-206421** | Feature: Proactive two-way ETH-SL sessions | Operational state verification for SLM |
| **SW-206804** | Feature: Historic test results (N-list) | Historic results verification after probe completion |
| **SW-207209** | Feature: System-Event CFM_PROACTIVE_TEST_FAILURE | Threshold violation event testing via syslog + show system events |
| **SW-198125** | CLI: on-demand stop operations | 13 on-demand start+stop test matrix (stop all, stop per MD/MA, stop per test-type) |
| **SW-198127** | CLI validation: reject commit on dependency deletion | Dependency deletion rejection test |
| **SW-198233** | CLI: handle config for proactive SLM/DM | Commit verification for all session/profile configs |
| **SW-235372** | CLI coverage: all knobs (SW-235373, SW-235927) | All profile/session variants + negative tests |
| **SW-237984** | Feature: on-demand test operations | On-demand start+stop comprehensive coverage |

---

## How to Run the Script

### Basic Execution

```bash
cd ~/Auto-nog && python3 y1731_cli_tab_test.py \
  --host <DEVICE_HOST> \
  --user <USERNAME> \
  --show-progress \
  --show-details \
  --cleanup
```

### Recommended Full Test Run

```bash
cd ~/Auto-nog && python3 y1731_cli_tab_test.py \
  --host WKY1C7VD00008P2 \
  --user dnroot \
  --all-meps \
  --show-progress \
  --show-details \
  --output-format table \
  --output-file results_$(date +%Y%m%d_%H%M%S).txt \
  --wait-for-results 40 \
  --low-threshold-wait 25 \
  --cleanup
```

### Command Line Arguments

| Argument | Description | Default |
|----------|-------------|---------|
| `--host` | Device hostname or IP | Required |
| `--user` | SSH username | Required |
| `--password` | SSH password (prompted if not provided) | - |
| `--all-meps` | Run tests for all discovered MEPs (scales test count) | False |
| `--show-progress` | Display real-time test progress | False |
| `--show-details` | Display detailed test step results | False |
| `--output-format` | Output format: `text` or `table` | `text` |
| `--output-file` | Save results to file | - |
| `--wait-for-results` | Seconds to wait for proactive session results before checking historic data | 30 |
| `--low-threshold-wait` | Seconds to wait for threshold violation event to appear in syslog | 20 |
| `--skip-on-demand-stop` | Skip on-demand start+stop tests | False |
| `--skip-show-proactive` | Skip show proactive commands after session creation | False |
| `--skip-event-test` | Skip system event CFM_PROACTIVE_TEST_FAILURE test | False |
| `--cleanup` | Remove all created sessions/profiles at end of test | False |
| `--timeout` | SSH connection timeout in seconds | 30 |

### Per-MEP Testing

Using `--all-meps` enables comprehensive per-MEP iteration:
- Discovers all local MEPs from device configuration
- Runs DM/SLM session and profile tests for each MEP independently
- Significantly increases test coverage and execution time
- Validates scale scenarios (e.g., multiple concurrent proactive sessions)

### Output Formats

**Text Format (`--output-format text`):**
- Simple pass/fail listing with details
- Lightweight, easy to grep
- Example:
  ```
  ✓ discover_cfm_context
  ✓ configure_dm_session
  ✗ negative_bad_md_ma: CLI validation failed as expected
  ```

**Table Format (`--output-format table`):**
- Categorized summary with pass/fail/skip counts
- Detailed per-category breakdown
- Example:
  ```
  Category: DM Session Configuration & Commit
  PASS: 4, FAIL: 0, SKIP: 0
  ✓ configure_dm_session
  ✓ commit
  ✓ verify_dm_config_present
  ✓ verify_dm_profile_config_present
  ```

---

## Test Execution Flow

1. **Discovery** → Identify CFM context (MD/MA/MEP)
2. **TAB Completion (Sessions)** → Validate CLI completions at session level
3. **Initial Config** → Create baseline DM/SLM sessions + profiles, commit
4. **TAB Completion (Profiles)** → Validate CLI completions at profile subcommand level ⭐ NEW
5. **Profile Variants** → Test all test-duration/threshold/PCP combinations (SW-235372)
6. **Session Variants** → Test target mep-id/mac-address, admin-state toggles (SW-235372)
7. **Show Commands** → Validate 10+ show command variants with filters ⭐ NEW
8. **On-Demand Tests** → Start+stop matrix with dual SSH sessions (SW-237984)
9. **Operational State** → Verify sessions are running, modify description ⭐ NEW
10. **Historic Results** → Wait for probes, check N-list data ⭐ NEW
11. **System Events** → Trigger threshold violation, verify syslog event ⭐ NEW
12. **Negative Tests** → Dependency deletion, invalid params, boundary violations
13. **Cleanup** → Remove all test artifacts (if `--cleanup` specified)

---

## Key Features

### ⭐ Newly Added Test Coverage (8 Gaps Resolved)

1. **inform-test-results disabled**: Tests DM/SLM profiles with result reporting disabled
2. **Profile-level TAB completion**: Validates TAB at threshold and test-duration subcommands
3. **PCP boundary testing**: Tests SLM PCP valid boundaries (0, 7) and invalid (8+)
4. **Dependency deletion rejection**: Validates CLI rejects commit when deleting referenced CFM entities
5. **Show commands comprehensive**: 10 show command variants with filters (session/MD/MA/MEP)
6. **Operational state verification**: Confirms sessions are running, tests parameter changes
7. **Historic test results**: Validates N-list data appears after probe completion
8. **System event CFM_PROACTIVE_TEST_FAILURE**: Tests threshold violation notifications in syslog

### Advanced Capabilities

- **Dual SSH Sessions**: On-demand stop tests use two concurrent SSH connections (start on SSH1, stop from SSH2)
- **Commit Check Validation**: Uses `commit check` for non-destructive config validation, `rollback 0` for teardown
- **Per-MEP Scale Testing**: `--all-meps` multiplies tests across all discovered MEPs
- **Smart Show Command Fallback**: Tries multiple show command variants (handles device CLI differences)
- **Real-time Progress Tracking**: `--show-progress` provides live test execution updates
- **Structured Output**: Table format categorizes results for easy analysis

---

## Notes

- **Candidate Config Preservation**: Script does NOT use `rollback 0` for most cleanups; instead, it explicitly deletes only the sessions/profiles it creates, preserving your existing candidate configuration
- **Commit History**: Use `rollback 1`, `rollback 2`, etc. to revert older committed configs (not `rollback 0`)
- **Scale Limitations**: Per epic comments, max 1 DMM/SLM session per MEP, max 1000 aggregated sessions across device
- **Timeouts**: Adjust `--wait-for-results` and `--low-threshold-wait` based on device probe intervals and threshold violation detection latency
- **Password Security**: If `--password` is omitted, the script prompts interactively (safer than command-line argument)

---

## Summary Statistics

| Metric | Value |
|--------|-------|
| **Total Test Categories** | 19 |
| **Base Test Steps** | ~80+ |
| **Test Steps (with --all-meps)** | 80+ × N (where N = number of MEPs) |
| **Jira Issues Covered** | 35+ |
| **CLI Commands Tested** | 50+ unique command paths |
| **Show Command Variants** | 10+ |
| **On-Demand Test Matrix** | 13 start+stop combinations |
| **Profile Variants (DM)** | 13+ (duration × threshold combinations) |
| **Profile Variants (SLM)** | 13+ (duration × threshold × PCP combinations) |
| **Negative Tests** | 7 |
| **TAB Completion Checks** | 14+ |

---

**Last Updated:** February 8, 2026  
**Script Version:** Includes all 8 gap implementations from plan `cli_test_gap_coverage_8e2865e7`  
**Maintainer:** Auto-nog team
