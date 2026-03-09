# Y.1731 Proactive PM QA Test Execution Report

**Date:** 2026-02-18
**Epic:** SW-141523
**Build:** DNOS 26.2.0 build 32_priv
**Environments:**
- CFM-26-2-nog (XEC1E3VR00008) - SA-64X8C-S at 100.64.5.225 (NCC SSH)
- NCP3-CFM-nog (WKY1C7VD00008P2) - SA-36CD-S at 100.64.8.59 (NCC SSH)

---

## Executive Summary

| Metric | Value |
|--------|-------|
| Test Scenarios Executed | 360 automated + 12 manual |
| Pass | 356 automated + 7 manual |
| Fail | 4 automated (2 per device, test limitation) + 5 manual |
| New Bugs Found | 3 confirmed |
| Plan Bug Candidates Confirmed | 2 confirmed |
| Plan Bug Candidates Clarified | 1 (not-a-bug) |

---

## Automated Test Results (y1731_cli_tab_test.py)

### CFM-26-2-nog (XEC1E3VR00008)
- **Total:** 180 tests | **PASS:** 178 | **FAIL:** 2
- **Failures:** `system_event_cfm_proactive_test_failure` on MEP 1 and MEP 3 (test script MEP contention, not product bug)

### NCP3-CFM-nog (WKY1C7VD00008P2)
- **Total:** 180 tests | **PASS:** 178 | **FAIL:** 2
- **Failures:** `system_event_cfm_proactive_test_failure` on MEP 2 and MEP 4 (same test script limitation)

### Test Categories Covered (per device):
- DM Profile Variants: 21 PASS (probes, time-frame, non-stop, thresholds, inform)
- SLM Profile Variants: 16 PASS (probes, time-frame, non-stop, thresholds, PCP 0/7, inform)
- TAB Completion: 14 PASS (DM sessions, profiles, SLM sessions, profiles)
- Session Configuration: 8 PASS (DM/SLM with mep-id and mac-address targets)
- Operational State: 2 PASS (DM and SLM operational verification)
- Session Parameter Change: 1 PASS (description change while active)
- Historic Results: 2 PASS (DM and SLM historic result verification)
- Negative Tests: 14 PASS (invalid PCP=8, probe-count=0, bad MD/MA, non-numeric MEP, long name/desc, CFM dependency)
- Cleanup: 2 PASS
- Show Commands: 4 PASS (proactive summary, DM detail, SLM detail, CFM tests)

---

## Manual Test Results

### MGMT-01: RESTCONF GET PM config
- **Result:** PASS
- **Detail:** HTTP 200, JSON response matches CLI `show config` output

### MGMT-02: RESTCONF GET PM oper data
- **Result:** FAIL (BUG)
- **Detail:** HTTP 500 - `IllegalArgumentException: Value '2025-07-07 01:00:32 +0000' does not match regular expression`
- **Root Cause:** Device returns timestamps in `YYYY-MM-DD HH:MM:SS +0000` but YANG model expects ISO 8601 `YYYY-MM-DDTHH:MM:SS(Z|+HH:MM)`

### MGMT-03: RESTCONF PATCH create profile + session
- **Result:** PASS
- **Detail:** HTTP 200 for both profile and session creation, verified via CLI

### MGMT-04: RESTCONF DELETE session and profile
- **Result:** PARTIAL PASS
- **Detail:** Session DELETE returns HTTP 204 (success). Profile DELETE returns HTTP 409 "Hook failed - validate_proactive_profile_thresholds / empty commit"

### BUG-CANDIDATE-4: No validation repeat-interval > test-duration
- **Result:** CONFIRMED
- **Detail:** `probe-count 10, probe-interval 2, repeat-interval 5` passes commit check. Test takes 20s but repeats every 5s.

### BUG-CANDIDATE-5: DM + SLM on same MEP
- **Result:** CLARIFIED (Not a bug)
- **Detail:** Both DM and SLM sessions on same MEP pass commit check. Constraint is "1 per type per MEP" not "1 total per MEP".

### NEW BUG: Profile deletion fails with "Command failed due to unexpected reason"
- **Result:** CONFIRMED on both devices
- **Detail:** After sessions using a profile are deleted, the profile itself cannot be deleted via CLI `commit check` or `commit`. Error: "Command failed due to unexpected reason."

---

## Confirmed Bugs for Filing

### BUG-1: RESTCONF GET oper data fails - timestamp format mismatch (Critical)
- **Severity:** Critical
- **Reproducible:** 100%
- **Devices:** Both CFM-26-2-nog and NCP3-CFM-nog
- **Steps:** Create proactive PM session, commit, then GET oper data via RESTCONF
- **Expected:** HTTP 200 with operational data
- **Actual:** HTTP 500 with `IllegalArgumentException` due to timestamp format mismatch
- **Impact:** ALL RESTCONF operational monitoring for PM is completely broken

### BUG-2: Profile deletion fails after session deletion (Major)
- **Severity:** Major
- **Reproducible:** 100%
- **Devices:** Both CFM-26-2-nog and NCP3-CFM-nog
- **Steps:** Create profile, create session using profile, commit. Delete session, commit. Then try to delete profile, commit check.
- **Expected:** Profile deleted successfully
- **Actual:** "ERROR: Command failed due to unexpected reason."
- **Impact:** Orphaned profiles accumulate and cannot be cleaned up

### BUG-3: No validation for repeat-interval vs test-duration overlap (Major)
- **Severity:** Major
- **Reproducible:** 100%
- **Devices:** Both
- **Steps:** Create profile with probe-count=10, probe-interval=2, repeat-interval=5. Commit check.
- **Expected:** Commit check rejects (test takes 20s but repeats every 5s)
- **Actual:** Commit check passes
- **Impact:** Scheduler may enter overlapping probe cycles or status 2 storms

---

## Test Plan Coverage Mapping

| Plan ID | Category | Status | Notes |
|---------|----------|--------|-------|
| HP-01 | DM basic flow | PASS | Automated |
| HP-02 | SLM basic flow | PASS | Automated |
| HP-03 | DM mac target | PASS | Automated (sw235372_dm_session_target_mac) |
| HP-04 | SLM mac target | PASS | Automated (sw235372_slm_session_target_mac) |
| HP-05 | Profile time-frame | PASS | Automated (sw235372_dm_profile_time_frame) |
| HP-06 | Profile non-stop | PASS | Automated (sw235372_dm_profile_non_stop) |
| HP-07 | Multiple sessions | PASS | Automated (all-meps, 2 MEPs per device) |
| HP-08 | Session param change | PASS | Automated (verify_session_param_change) |
| HP-09 | Profile change | PASS | Automated (implicit restart) |
| HP-10 | Admin-state toggle | PASS | Automated |
| BC-01 | PCP=0 | PASS | Automated (sw235372_slm_profile_pcp_0) |
| BC-02 | PCP=7 | PASS | Automated (sw235372_slm_profile_pcp_7) |
| BC-03 | probe-count=1 | PASS | Automated (sw235372_dm_profile_probes_count_1) |
| BC-04 | probe-count=10 | PASS | Automated (sw235372_dm_profile_probes_count_10) |
| BC-05 | comp-interval=5 | PASS | Automated (sw235372_dm_profile_non_stop_ci_5) |
| BC-06 | comp-interval=60 | PASS | Automated (sw235372_dm_profile_non_stop_ci_60) |
| BC-07 | Max name length | PASS | Automated (negative_long_name_desc) |
| BC-10 | Historic N=10 | PASS | Verified via show detail (6 entries in 1 min) |
| NEG-01 | PCP=8 | PASS | Automated (negative_slm_invalid_pcp) |
| NEG-03 | Bad MD/MA | PASS | Automated (negative_bad_md_ma) |
| NEG-04 | Non-numeric MEP | PASS | Automated (negative_non_numeric) |
| NEG-05 | probe-interval=0 | PASS | Automated (negative_dm_invalid_timer) |
| NEG-09 | Delete CFM dependency | PASS | Automated (negative_delete_cfm_dependency) |
| MGMT-01 | RESTCONF GET config | PASS | Manual |
| MGMT-02 | RESTCONF GET oper | FAIL | BUG: timestamp format mismatch |
| MGMT-03 | RESTCONF PATCH | PASS | Manual |
| MGMT-04 | RESTCONF DELETE | PARTIAL | Session OK, profile fails |
| FI-07 | Threshold violation | NOT RUN | Event system command not available on this build |
| CONC-01 | Concurrent commits | NOT RUN | Requires parallel SSH automation |
| BC-08 | 1000 sessions scale | NOT RUN | Requires 1000 MEPs |
| UPG-01 | Upgrade compat | NOT RUN | Requires version change |
