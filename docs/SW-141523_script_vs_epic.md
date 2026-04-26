# y1731_cli_tab_test.py vs Epic SW-141523

Epic: [SW-141523](https://drivenets.atlassian.net/browse/SW-141523)

---

## 1. Everything the script tests

### Discovery & context
| Step | Description |
|------|-------------|
| `discover_cfm_context` | Auto-discover MD/MA/MEP/target from `show config services ethernet-oam` (or prompt) |
| `discover_source_mep_id` | Validate source mep-id via PM CLI completion / ethernet-oam config |
| `discover_dm_target_mep_id` | Discover DM target mep-id via TAB completion |
| `manual_cfm_context` | Fallback when auto-discovery skipped; prompts for MD/MA/MEP/target |

### TAB completion
| Step | Description |
|------|-------------|
| `tab_completion: <prefix>` | TAB completion for DM profile, session, source maintenance-domain/association/mep-id, target mep-id; SLM profile, session, source, target |

### DM (two-way delay measurement) – base config
| Step | Description |
|------|-------------|
| `configure_dm_session` | Create DM profile + DM session (valid MD/MA/MEP, profile, target), commit |
| `commit` | Final commit after all config steps |
| `verify_dm_config_present` | Show config contains DM session |

### SLM (two-way synthetic loss) – base config
| Step | Description |
|------|-------------|
| `configure_slm_session` | Create SLM profile + SLM session (valid MD/MA/MEP, profile, target), commit |
| `commit_slm` | Commit evaluation for SLM |
| `verify_slm_config_present` | Show config contains SLM session |

### SW-235372 – DM profile (test-duration + thresholds)
| Step | Description |
|------|-------------|
| `sw235372_dm_profile_probes` | test-duration probes (probe-count 5, probe-interval 1, repeat-interval 10) |
| `sw235372_dm_profile_time_frame` | test-duration time-frame (minutes 1, probe-interval 1, repeat-interval 10) |
| `sw235372_dm_profile_non_stop` | test-duration non-stop (probe-interval 1, computation-interval 10) |
| `sw235372_dm_profile_probes_count_1` | probe-count 1 |
| `sw235372_dm_profile_probes_count_10` | probe-count 10 |
| `sw235372_dm_profile_probes_interval_10_10` | probe-interval 10, repeat-interval 10 |
| `sw235372_dm_profile_probes_interval_1_30` | probe-interval 1, repeat-interval 30 |
| `sw235372_dm_profile_time_frame_5min` | time-frame minutes 5 |
| `sw235372_dm_profile_non_stop_ci_5` | non-stop computation-interval 5 |
| `sw235372_dm_profile_non_stop_ci_60` | non-stop computation-interval 60 |
| `sw235372_dm_profile_threshold_delay_min_only` | thresholds: delay-rtt-min 100 only |
| `sw235372_dm_profile_threshold_success_rate_only` | thresholds: success-rate 90 only |
| `sw235372_dm_profile_threshold_delay_min_and_success` | thresholds: delay-rtt-min 100 + success-rate 90 |
| `sw235372_dm_profile_threshold_delay_avg_only` | thresholds: delay-rtt-avg 1000 only |
| `sw235372_dm_profile_threshold_jitter_avg_only` | thresholds: jitter-rtt-avg 500 only |
| `sw235372_dm_profile_threshold_all_six` | all 6 DM thresholds (delay min/avg/max, jitter avg/max, success-rate) |
| `sw235372_dm_profile_threshold_value_delay_min_50` | delay-rtt-min 50 |
| `sw235372_dm_profile_threshold_value_delay_min_200` | delay-rtt-min 200 |
| `sw235372_dm_profile_threshold_value_success_rate_50` | success-rate 50 |
| `sw235372_dm_profile_threshold_value_success_rate_99` | success-rate 99 |

### SW-235372 – SLM profile (test-duration + thresholds + PCP)
| Step | Description |
|------|-------------|
| `sw235372_slm_profile_probes` | test-duration probes (probe-count 5, …) |
| `sw235372_slm_profile_time_frame` | test-duration time-frame (minutes 1, …) |
| `sw235372_slm_profile_non_stop` | test-duration non-stop (… computation-interval 10) |
| `sw235372_slm_profile_probes_count_1` | probe-count 1 |
| `sw235372_slm_profile_probes_count_10` | probe-count 10 |
| `sw235372_slm_profile_time_frame_5min` | time-frame minutes 5 |
| `sw235372_slm_profile_non_stop_ci_5` | non-stop computation-interval 5 |
| `sw235372_slm_profile_non_stop_ci_60` | non-stop computation-interval 60 |
| `sw235372_slm_profile_threshold_near_only` | thresholds: near-end-loss 1 only |
| `sw235372_slm_profile_threshold_far_only` | thresholds: far-end-loss 1 only |
| `sw235372_slm_profile_threshold_both_1` | near-end-loss 1 + far-end-loss 1 |
| `sw235372_slm_profile_threshold_near_0_far_0` | near-end-loss 0, far-end-loss 0 |
| `sw235372_slm_profile_threshold_near_5_far_5` | near-end-loss 5, far-end-loss 5 |
| `sw235372_slm_profile_pcp_0` | PCP 0 |
| `sw235372_slm_profile_pcp_7` | PCP 7 |

### SW-235372 – DM session variants
| Step | Description |
|------|-------------|
| `sw235372_dm_session_target_mep` | Session: admin-state enabled/disabled, description, profile, source md/ma mep-id, target mep-id |
| `sw235372_dm_session_target_mac` | Session: target mac-address 00:11:22:33:44:55 |

### SW-235372 – SLM session variants
| Step | Description |
|------|-------------|
| `sw235372_slm_session_target_mep` | Session: admin-state enabled/disabled, description, profile, source, target mep-id |
| `sw235372_slm_session_target_mac` | Session: target mac-address 00:11:22:33:44:55 |

### Negative tests (expect command/commit-check failure)
| Step | Description |
|------|-------------|
| `negative_slm_long_name_desc` | SLM profile/session with very long name and description |
| `negative_slm_bad_md_ma` | SLM session with invalid MD/MA (bad-md, bad-ma) |
| `negative_long_name_desc` | DM profile/session with very long name and description |
| `negative_bad_md_ma` | DM session with invalid MD/MA |
| `negative_non_numeric` | Non-numeric mep-id and target mep-id (e.g. abc) |

### Cleanup
| Step | Description |
|------|-------------|
| `cleanup` | Remove script-created DM/SLM sessions and profiles only (no rollback 0); commit |

---

## 2. Epic SW-141523 – what needs to be tested (from TP checklist)

| # | Area | Epic requirement | Script covers? |
|---|------|------------------|----------------|
| **1** | **CLI / Config validation** | Configure proactive session with valid MD/MA/MEP; verify show config and show output | ✅ Yes – configure_dm_session, configure_slm_session, verify_*_config_present |
| | | Configure with invalid MD/MA or non-existent MEP → commit fails with clear error | ✅ Yes – negative_bad_md_ma, negative_slm_bad_md_ma |
| | | CLI help and autocomplete for MD/MA/session names | ✅ Yes – TAB completion steps |
| | | Verify no commands remove config cleanly | ⚠️ Partial – script only removes its own sessions/profiles; no “no” command validation |
| | | commit check, commit confirm, rollback 1–49, load override, load merge, factory-default, re-apply | ❌ No – only commit check + commit; no rollback/load/factory-default |
| **2** | **Functionality – proactive** | DM (DMM/DMR/1DM) with CCM to MEP ID; valid DMRs, delay/jitter metrics | ❌ No – CLI/config only; no DMM/DMR or metric verification |
| | | DM without CCM to MAC target; reachable/unreachable | ❌ No |
| | | SLM with/without CCM; loss stats | ❌ No |
| | | PCP reflected in packet capture | ❌ No – PCP 0/7 config only |
| | | Scheduling min/max intervals (1..3600) and boundary validation | ⚠️ Partial – some intervals (1, 5, 10, 30, 60) in test-duration; no full range/boundary |
| **3** | **On-demand output** | run … delay-measurement … detail with unreachable peer → no fake “DMR received” | ❌ No |
| | | DM stats summary vs detail match | ❌ No |
| | | run … linktrace … → no “unexpected reason” | ❌ No |
| **4** | **Profiles / thresholds / stats** | Create PM profiles and apply to sessions | ✅ Yes – DM/SLM profiles + sessions |
| | | show services performance-monitoring profiles: test-duration, thresholds | ⚠️ Partial – config/commit-check only; no explicit profile show validation |
| | | Force threshold violation → CFM_PROACTIVE_TEST_FAILURE event + SNMP trap | ❌ No |
| **5** | **Show commands** | show … cfm tests (summary/detail), on-demand, proactive, per MD/MA | ⚠️ Partial – verify config present only; no structured show output checks |
| **6** | **SNMP / NETCONF / GNMI / RESTConf** | SNMP walk; NETCONF/GNMI/RESTConf config + oper | ❌ No |
| **7** | **Scale** | One DMM or SLM per MEP; max 1000 endpoints; overscale recovery | ❌ No |
| **8** | **HA / Restart** | Process restart, warm/cold restart, switchover | ❌ No |
| **9** | **Logs / counters** | Traces, clear counters, restart behavior | ❌ No |
| **10** | **Interface/service permutations** | Physical, bundle, sub-interface, VLAN, L2/L3 services | ❌ No |
| **11** | **Negative** | Invalid PCP, timers, MEP IDs; unreachable targets; malformed PDUs | ⚠️ Partial – invalid MD/MA, non-numeric mep-id; no PCP/timer/unreachable/malformed |
| **12** | **Upgrade/downgrade** | Config preserved; oper consistent | ❌ No |

---

## 3. Testing checklist (SW-141523_testing_checklist.md) – script mapping

| Checklist item | Script step(s) |
|----------------|----------------|
| SW-235373 \| performance-monitoring \| cfm \| two-way-delay-measurement | configure_dm_session, sw235372_dm_session_*, negative_*_md_ma, negative_non_numeric |
| SW-235927 \| performance-monitoring \| cfm \| two-way-synthetic-loss-measurement | configure_slm_session, sw235372_slm_session_*, negative_slm_* |
| SW-235375 \| Profiles \| two-way-delay-measurement | build_dm_profile_commands; sw235372_dm_profile_* (all DM profile variants) |
| SW-236444 \| Profiles \| two-way-synthetic-loss-measurement | build_slm_profile_commands; sw235372_slm_profile_* (all SLM profile variants) |
| SW-236451 \| Profiles \| two-way-synthetic-loss-measurement \| test duration | sw235372_slm_profile_probes/time_frame/non_stop + probes_count_*, time_frame_5min, non_stop_ci_* |
| SW-236465 \| Profiles \| two-way-delay-measurement \| test duration | sw235372_dm_profile_probes/time_frame/non_stop + probes_count_*, probes_interval_*, time_frame_5min, non_stop_ci_* |
| SW-236452 \| Profiles \| two-way-delay-measurement \| thresholds | sw235372_dm_profile_threshold_* (subsets + value variants) |
| SW-236457 \| Profiles \| two-way-synthetic-loss-measurement \| thresholds | sw235372_slm_profile_threshold_* + pcp_0, pcp_7 |
| SW-235376 \| Show commands | verify_dm_config_present, verify_slm_config_present (show config presence only) |
| SW-237984 \| request ethernet-oam cfm on-demand stop | ❌ Not in script |
| Functionality (SW-236664 ETH-DM, SW-236665 ETH-SLM, etc.) | ❌ Script is CLI/config + commit-check only; no proactive run or metric verification |
| Scale (SW-236988, SW-236989, SW-236991) | ❌ Not in script |
| HA (SW-237045) | ❌ Not in script |
| SNMP (SW-235385) | ❌ Not in script |
| NETCONF / GNMI / RESTConf | ❌ Not in script |
| Statistics / Buckets / Thresholds (SW-238001, SW-238003, etc.) | ⚠️ Profile/session config and threshold CLI only; no stats/buckets/inform-test-results behavior |

---

## 4. Summary

- **Script focus:** CLI configuration and commit-check coverage for DM/SLM sessions and profiles (SW-235372, SW-235373, SW-235927, and related profile/test-duration/threshold tickets). It also does discovery, TAB completion, negative config tests, and teardown without rollback 0.
- **Epic coverage:** The script aligns with **§1 CLI/Config validation** and **§4 Profiles/thresholds** (config side), and with the **CLI-focused** checklist items. It does **not** cover functionality (DMM/DMR, metrics, on-demand), scale, HA, SNMP/NETCONF/GNMI/RESTConf, show output correctness, threshold violation events/traps, or upgrade/downgrade.
- **Gaps if “everything in epic” is required:** Add manual or separate automation for: proactive run + metric verification, on-demand output checks, show summary/detail correctness, rollback/load/factory-default, scale and HA, SNMP/NETCONF/GNMI/RESTConf, threshold violation events/traps, and interface/service permutations.
