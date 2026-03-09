SW-141523 TP Checklist (Detailed)

Epic: https://drivenets.atlassian.net/browse/SW-141523

Pre-Checks
- Confirm supported PM types (DM/SLM/LM) and any exclusions (LMM not supported).
- Confirm scale limits (max endpoints 1000; one DMM or SLM session per MEP).
- Identify DUT interfaces and services to cover (L2/L3, VLAN, sub-interfaces, bundles).

1) CLI / Config Validation
- Configure proactive session with valid MD/MA/MEP.
  - Verify config appears in show config and show output (summary/detail).
- Configure with invalid MD/MA or non-existent MEP.
  - Expected: commit fails with clear validation error (no "unexpected error").
- Verify CLI help and autocomplete for MD/MA/session names.
- Verify no commands remove config cleanly.
- Run commit check, commit confirm, rollback 1-49, load override, load merge,
  factory-default and re-apply.

2) Functionality - Proactive Sessions
- DM (DMM/DMR/1DM) with CCM to MEP ID:
  - Expected: valid DMRs, accurate delay/jitter metrics.
- DM without CCM to MAC target:
  - Expected: correct behavior when MAC is reachable/unreachable.
- SLM (SLM/SLR/1SL) with CCM and without CCM:
  - Expected: correct loss stats.
- PCP per session:
  - Verify PCP is reflected in packet capture.
- Scheduling:
  - Verify min/max intervals (1..3600 sec/min) and boundary validation.

3) On-Demand Output Correctness (Regression Coverage)
- run ... delay-measurement ... detail with unreachable peer:
  - Expected: no fake "DMR received" lines.
- DM stats summary vs detail:
  - Expected: stats match actual received DMRs (non-zero when responses exist).
- run ... linktrace ...:
  - Expected: no "unexpected reason" failure.

4) Profiles / Thresholds / Statistics
- Create PM profiles and apply to sessions.
- Verify show services performance-monitoring profiles for:
  - test-duration (count/time-frame/non-stop)
  - thresholds (delay/jitter/loss, near/far end)
- Force threshold violation:
  - Expected: CFM_PROACTIVE_TEST_FAILURE system event.
  - Expected: dnCfmProactiveTestFailure SNMP trap with correct fields.

5) Show Commands
- Verify summary and detail shows:
  - show services performance-monitoring cfm tests
  - show ... tests on-demand (summary/detail)
  - show ... tests proactive (summary/detail)
  - Per MD/MA summary views (if supported)
- Verify output correctness and no missing fields.

6) SNMP / NETCONF / GNMI / RESTConf
- SNMP walk for proactive tables; verify values match CLI.
- NETCONF/GNMI/RESTConf:
  - config visibility
  - oper data alignment with CLI outputs

7) Scale
- One DMM or SLM session per MEP (reject multiple per MEP).
- Max endpoints 1000:
  - Verify commit validation at/over limit.
- Overscale recovery:
  - After removing extra sessions, system recovers cleanly.

8) HA / Restart
- Process restarts (wb_agent / relevant services):
  - sessions recover or fail cleanly, no stale counters.
- System warm/cold restart:
  - config preserved; sessions re-established if enabled.
- Switchover/failover:
  - no crashes, expected session behavior.

9) Logs / Counters
- Check traces/logs update with session lifecycle.
- clear counters resets correctly.
- Restart behavior updates uptime/counters.

10) Interface/Service Permutations
- Physical, bundle, sub-interfaces.
- VLAN single/double tagging.
- L2/L3 services as supported (EVPN/VPWS/L3VPN/IRB).
- Verify behavior when moving config between interface types.

11) Negative Testing
- Invalid PCP values, timers, MEP IDs.
- Unreachable targets.
- Malformed PDUs (if tool supports).

12) Upgrade/Downgrade
- Config preserved across versions.
- Oper outputs consistent post-upgrade.

Pass/Fail Notes
- Any misleading CLI output is a fail (e.g., fake DMRs).
- Any unexpected error or crash is a fail.
- Missing fields in show outputs or SNMP traps is a fail.
