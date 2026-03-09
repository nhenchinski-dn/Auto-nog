# SW-141523 TP Outline (Y.1731 PM Initiator)

Epic: https://drivenets.atlassian.net/browse/SW-141523
Checklist reference: https://drivenets.atlassian.net/wiki/spaces/UP/pages/4959536319/Checklist+for+TP

Scope Summary
- FM: ETH-AIS, ETH-LB (ETH-LT covered elsewhere)
- PM Initiator: ETH-DM (DMM/DMR/1DM), ETH-SLM (SLM/SLR/1SL), ETH-LM (LMM/LMR/LM)
- Session targets: remote MEP ID with CCM, or remote MAC without CCM
- PCP per session
- Session timing range: 1-3600 seconds/minutes

Important Notes / Corrections (from epic comments)
- LMM is not supported (remove from testing scope unless clarified)
- Scale: max endpoints 1000 (not 2000)
- One DMM or SLM session per MEP at a time (no multiple sessions per MEP)

Test Categories to Include (Checklist-aligned)

1) Interface Types / Services Permutations
- Single and double-tag VLANs
- L2 and L3 sub-interfaces
- Physical and bundle interfaces
- Any relevant services (EVPN, L2VPN, L3VPN, IRB) if supported

2) Sanity
- Basic positive flow for each PM type (DM/SLM/LM)
- Basic negative flow (invalid target, invalid PCP, missing CCM)
- CLI + traffic validation

3) CLI
- Verify all config hierarchies and "no" commands
- Commit check/confirm/and-exit and rollback 1-49
- Load override/merge and factory-default paths
- Show commands with filters (include/exclude/monitor/no-more)
- Help lines and TAB completion for all knobs

4) Negative Testing
- Invalid MEP ID or MAC address formats
- Out-of-range PCP and timers
- Malformed PM PDUs (if test equipment allows)

5) Counters
- Validate show counters and clear/reset behavior
- Restart sessions and verify uptime/counters reset
- SNMP/NETCONF/gNMI visibility for counters

6) Logs / Traces
- Trigger events and verify log content and format
- Rotation behavior where relevant
- Verify errors are logged for invalid operations

7) Scale
- One session per MEP (DMM or SLM)
- Max endpoints 1000
- Overscale validations and recovery

8) SNMP
- Validate proactive session MIB coverage
- Verify fields align with CLI/show outputs

9) HA
- Process restarts, system warm/cold, switchover
- Verify sessions recover and state is consistent

10) System Resources Exhaustion
- Monitor CPU/memory while at max supported scale
- Validate no leaks or lingering threads/sockets

11) DP / Traffic Types
- Validate delay/jitter/loss accuracy across traffic profiles
- Verify no unintended traffic impact

12) Upgrade / Downgrade
- Config preserved across versions
- Oper behavior unchanged after upgrade/downgrade

Test Steps (Condensed)
1) Configure baseline MD/MA/MEP and a proactive DM session.
   - Verify show config and show outputs (summary/detail).
2) Run on-demand DM with valid target and confirm metrics are non-zero.
3) Run on-demand DM with unreachable target.
   - Verify no fake DMR lines and stats reflect no responses.
4) Configure SLM session (with CCM and without CCM).
   - Verify loss metrics and oper outputs.
5) Configure profiles (test-duration + thresholds), apply to sessions.
   - Verify show profile outputs include all fields.
6) Force a threshold violation.
   - Verify CFM_PROACTIVE_TEST_FAILURE event and SNMP trap.
7) Validate CLI UX.
   - Autocomplete, help, "no" commands, commit check/confirm.
8) Validate scale.
   - One session per MEP, max 1000 endpoints, overscale behavior.
9) Validate HA/restarts.
   - Process restart, warm/cold reboot, switchover with sessions active.
10) Validate management interfaces.
   - SNMP/NETCONF/gNMI/RESTConf reflect config + oper data.

Y.1731-Specific Scenarios
- With CCM vs without CCM (MEP ID vs MAC target)
- PCP per session (verify value reflected in PDUs)
- Multi-session across different MEPs (but only one per MEP)
- Validate output fields for 1DM/DMR and SLM/SLR

Open Items to Confirm
- Final supported list of PM types (LM/LMM support status)
- Exact CLI structure and show commands for proactive sessions
- Any limits per MA/MEP beyond the 1000 endpoints note
