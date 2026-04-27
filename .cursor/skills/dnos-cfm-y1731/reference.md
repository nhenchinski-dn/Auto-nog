# DNOS CFM / Y.1731 — Deep Reference

This file is the cross-reference lookup for the `dnos-cfm-y1731` skill: every limitation, every BCM case number, every YANG path, the FNG state machine, defect taxonomy, full validation list, and the Auto-nog test-script weakness inventory. Read this when you need to drill into a specific behavior; use SKILL.md + examples.md for normal config tasks.

---

## 1. Feature scope by epic

Sourced from `cheetah/.ai/spec/current/Services/oam/cfm/*` and `…/oam/y1731/*`.

| Epic / story | Capability | DNOS version |
|---|---|---|
| SW-53144 | CFM essentials, MD/MA/MEP, CCM TX/RX, FNG, show/clear CLI | v19.1 |
| SW-134176 | CPRL for CFM PDUs (1000 pps / 1000 burst default) | v19.1 |
| SW-134427 | Multi-VLAN sub-if `l2-originated-vlan-tags`, `m` flag in `show interfaces` | v19.2 |
| SW-137884 | Y.1731 Responder (DMM/DMR, SLM/SLR), CCM interop 802.1ag↔Y.1731 | v19.2 |
| SW-141474 | Y.1731 essentials: ICC-based MEG-ID, interface-level show, VPN-ID canonicalization | v19.2 |
| SW-144479 / SW-145812 | `short-ma-name icc-based <icc> <umc>` CLI/YANG | v19.2 |
| SW-144490 | `show … connectivity-fault-management interfaces` | v19.2 |
| SW-144792 | MY-CFM-MAC BGP type-2 sticky on first unicast CFM reply | v19.2 |
| SW-91269 | Y.1731 On-demand Initiator (DM, SLM), 16 concurrent CLI sessions | v19.3 |
| SW-148407 | NCP3 Y.1731 (Responder + Initiator) — depends on BCM SDK 6.5.29–6.5.32 | v25.1 |
| SW-125005 / SW-144478 | DNOS alarm `CFM_DEFECT_CONDITION_DETECTED` via alarm manager | v25.1 |
| SW-188362 / SW-197915 | DRIVENETS-CFM-MIB (SNMP on-demand DM/SLM, fault traps) | v25.4 |
| SW-208623 | Y.1731 proactive Initiator (DMM/SLM) | dev_v26.x |
| SW-216153 (PR #92176) | SW responder for ETH-DM with Test ID TLV (Cisco interop), payload bit-exact reflection | dev_v26_3 |
| SW-229213 | Expose `dmm_in` / `dmr_out` per-MEP counters | dev_v26_3 |
| SW-129152 | Cluster HA sync for CFM (open epic) | not yet |

Rejected as out-of-scope for the current code line:
- **SW-144774 / SW-144775 / SW-144776 / SW-144777** — per-opcode HW counters for DMM/DMR/SLM/SLR (BCM CRPS exhaustion CS00012340847)
- **SW-144512 / SW-144513 / SW-144600** — multi-Test-ID SLM (BCM OAMP supports only Test-ID = 0, CS00012339955)
- **SW-144787** — CLI knob to bind MA/MEG to a specific L2 service instance

---

## 2. Defect taxonomy and FNG state machine

Five defect types per MEP (highest priority first):

| Priority | Defect | Trigger |
|---|---|---|
| 5 | DefXconCCM | CCM from a different MAID, or from a MEP at a lower MD level (cross-connect) |
| 4 | DefErrorCCM | Wrong CCM interval, wrong MEPID (incl. own MEPID echoed back → loop), MAID format errors |
| 3 | DefRemoteCCM | LOC: `loss-threshold` consecutive missed CCMs from a configured / discovered RMEP |
| 2 | DefMACStatus | Port Status TLV error or Interface Status TLV ≠ `isUp` in received CCM |
| 1 | DefRDICCM | Remote MEP advertising RDI (some other MEP in MA has a defect) |

FNG states: `fng-reset → fng-defect → fng-report-defect → fng-defect-reported → fng-defect-clearing`.

- Alarm raise: highest defect ≥ `lowest-priority-defect` threshold sustained for `fng-alarm-time` (default 2500 ms, range 2500–10000).
- Alarm clear: highest defect drops below threshold sustained for `fng-reset-time` (default 10000 ms, range 2500–10000).
- A **higher**-priority defect replacing an existing one (both above threshold) triggers a **new** event after `fng-alarm-time`.
- The highest defect clearing while a **lower** defect remains above threshold sends an event **immediately**.
- Threshold change at runtime resets both timers and re-evaluates; can momentarily clear-then-raise.
- **RDI bit** is set in outgoing CCMs whenever any of {DefXconCCM, DefErrorCCM, DefRemoteCCM, DefMACStatus} is present — independent of `lowest-priority-defect`. **This is a deviation from IEEE 802.1ag**: DNOS reports any detected defect regardless of the operator-configured threshold.
- The `action` leaf in `CFM_DEFECT_CONDITION_DETECTED` always carries `no-action` (EFD `disable-interface` deferred).

---

## 3. Full validations & constraints

| # | Rule | Source |
|---|---|---|
| 1 | MEPID range 1–8191; unique within an MA; must not equal any RMEP in the same MA | SW-53144 |
| 2 | MD level 0–7 mandatory | 802.1ag |
| 3 | MD-name string length 1–43; MA short-name string length 1–45 (RFC 2579 DisplayString) | SW-53144 |
| 4 | ASCII `"` (DEC 34) not allowed in MD/MA string names | SW-53144 |
| 5 | VPN-ID format `<3-byte hex OUI>:<4-byte hex index>`; canonicalized to upper-case hex internally (RFC 2685) | SW-141474 |
| 6 | DNS-format MD name follows RFC 1035 | SW-53144 |
| 7 | CCM interval is an MA-level setting, applies to all MEPs in MA; per-MEP / per-MD overrides rejected | SW-53144 |
| 8 | Loss threshold 3–255 (default 3) | SW-53144 |
| 9 | `fng-alarm-time` 2500–10000 ms (default 2500) | SW-53144 |
| 10 | `fng-reset-time` 2500–10000 ms (default 10000) | SW-53144 |
| 11 | Maximum 2000 MDs per system; 1 local MEP per MA | SW-53144 |
| 12 | Auto-discovered RMEPs: configurable 0–8191 (default 2048); syslog at configurable threshold (default 75%) | SW-53144 |
| 13 | `l2-originated-vlan-tags` only on L2 multi-VLAN sub-interfaces; inner-tag requires outer-tag | SW-134427 |
| 14 | For multi-VLAN UP MEPs running DM/SLM: `l2-originated-vlan-tags` is mandatory (otherwise OAMP malforms response) | SW-134427, BCM CS00012346542 |
| 15 | Sender ID TLV not supported (BCM SDK limitation, CS00012316431) | SW-53144 |
| 16 | Unicast CCM destination supported v19.2+; multicast-only on v19.1 | SW-53144 |
| 17 | Local MEP/MIP interface types: physical Ethernet, IEEE 802.3ad LAG, L2 VLAN sub-if. Loopback / IRB / GRE rejected | SW-53144 |
| 18 | Maximum 2048 combined CFM + BFD + Y.1731 sessions in MEP-DB (HW resource pool); excess accepted as config but not counted | SW-53144 |
| 19 | PCP 0–7 (default 7) for CCMs/LTMs | SW-53144 |
| 20 | Interface Status TLV: only `isUp` (1) and `isDown` (2) are transmitted | RFC 2863, BCM behavior |
| 21 | RMEP must be known (crosscheck or auto-discovered) for the MEP to reply to DMM/SLM | SW-137884 |
| 22 | Unicast DMAC only for DMM/SLM; multicast Class-1 DMAC dropped | SW-137884, SW-142027 |
| 23 | Tagging structure of received DMM/SLM must match interface VLAN config; mismatched → no/malformed response | SW-137884 |
| 24 | ICC-based MEG-ID requires `md-name null`; ICC 1–6 alphanum, UMC 7–12; combined 13 chars padded with NULLs; format/length only (content not validated) | SW-144479 |
| 25 | 1 ETH-SLM session per local MEP; SLM initiator ↔ responder mutually exclusive on same MEP (HW counter set) | SW-153275, SW-91269 |
| 26 | ETH-LM (LMM/LMR) and ETH-SLM mutually exclusive at HW level | SW-91269 |
| 27 | PCP cannot be configured for DMM (BCM OAMP limitation, CS00012355451) | SW-153273 |
| 28 | DM/SLM probe intervals: `1, 10, 60, 600` seconds; 100 ms waived for SLM | SW-153277 |
| 29 | DM/SLM count: 1–65535 | SW-153276 / SW-153277 |
| 30 | Max 16 concurrent on-demand initiator CLI sessions system-wide | SW-91269 |
| 31 | Max 1 ETH-DM and 1 ETH-SLM initiator session per MA at a time | SW-91269 |
| 32 | Max 1000 ETH-DM responder sessions per system (NCP6); 200 concurrent total PM target (Y.1731 v19.2) | SW-142044 |
| 33 | Late SLR frames (>5 s after test end) are discarded (ITU-T G.8021) | SW-153271 |
| 34 | DMR optional TLVs not reflected on the **HW** responder (known limitation) — SW responder PR #92176 (dev_v26_3) **does** preserve them | SW-142028 |
| 35 | CCM sequence number always 0 (BCM OAMP cannot increment, CS00012305249) | SW-53144 |
| 36 | MAC level consistency via destination-MAC nibble not verified in HW (TCAM cost) — level taken from PDU level field | SW-53144 |
| 37 | Source MAC of incoming DMR not extractable via BCM callback API (cannot be displayed in detail output) | SW-153276 |

---

## 4. Hardware / platform notes (BCM)

- **NCP6 / NCP4 (NCPL) standalone (BCM88690 / Jericho 2):** OAMP performs CCM TX, defect detection, DMM↔DMR, SLM↔SLR, optional TLV preservation rules, HW timestamping (1588 format).
- **DM HW timestamps** (`RxTimeStampf`, `TxTimeStampb`) require two recycle ports (one per core) configured as `INJECTED_2_PP` header type for UP MEPs. If unprovisioned, both fields are zero in the DMR (initiator falls back to plain two-way RTT only).
- **SLM endpoints** must be placed in BCM OAMP banks 4–7 (MEP-ID bit 15 = 1, i.e. MEP-ID ≥ 32768) and require the `BCM_OAM_GROUP_FLEXIBLE_MAID_20_BYTE` group flag.
- **OAMP one-Test-ID limit:** SLM hardware does not distinguish Test ID; only Test ID = 0 used. Multiple sessions per MEP overwrite each other's counters.
- **Per-opcode counter rejection:** BCM case CS00012340847 — enabling per-opcode OAMP counting exhausts CRP counters at scale. The 6 YANG leaves (`dmm-out, dmr-in, slm-out, slm-in, slr-out, slr-in`) exist but stay 0 even on the new SW responder; only `dmm_in` and `dmr_out` are populated by SW-229213.
- **Up-MEP TOD recycle:** controlled by BCM data key `oam.feature.up_mep_tod_recycle_enable`. If `0`, `tx_timestampb = 0` in DMRs from Up-MEPs (W4 in PR #92176 weakness inventory).
- **Hard-coded core gports:** `CFM_CORE_0_TOD_RECYCLE_GPORT` / `CFM_CORE_1_TOD_RECYCLE_GPORT` (BCM-side hardcodes).
- **DMM/DMR trap path is shared** with LBR / LT / BFD via `WB_RX_TRAP_NON_ACC_OAM_BFD` / `WB_RX_TRAP_OAM_UP_MEP_DEST1` (W9 in PR #92176 inventory) — at high scale these protocols contend for trap bandwidth.
- **NCP3-SA without inter-unit SyncE:** triggers a `MIN_SYNCE_SUPPORTED_DEVICE_VERSION_NCP3` skipif in dev tests; documented `tx_timestampb` behavior may differ (W17).
- **NCP3 (Cluster):** SLM Responder via OAMP Client-Server in BCM SDK 6.5.29–6.5.30; ETH-DM in 6.5.32 (BCM CS00012345851). Delivered in v25.1.
- **Cluster HA sync for CFM is not implemented** (SW-129152 open). CCM at 3.3 ms during NCP failover may flash an RDI.

---

## 5. YANG paths (high-level)

Top-level CFM container:

```
/dn-top:drivenets-top/dn-srv:services/dn-srv-eoam:ethernet-oam/dn-srv-cfm:connectivity-fault-management
  global/config-items:                    maximum-auto, maximum-auto-syslog-threshold
  global/global-statistics
  maintenance-domains/maintenance-domain[md-id]
    config-items:                          md-id, md-name (choice: null|string|dns|mac+uint), md-level (0-7)
    maintenance-associations/maintenance-association[ma-id]
      config-items:                        ma-id, ma-name (choice), continuity-check, remote-meps
      local-meps/local-mep[mep-id]
        config-items:                      interface-name, direction, admin-state, ccm-ltm-priority
        oper-items:                        mac-address, fng-state, highest-priority-defect, defects (bits),
                                           mep-db (list), missing-rmeps, statistics
      local-meps/mip[mip-name]
        config-items:                      interface-name, admin-state
        oper-items:                        mac-address, pdu-statistics
```

Performance-monitoring container (read-only test results):

```
/dn-top:drivenets-top/dn-srv:services/dn-pm:performance-monitoring/dn-pm:cfm-tests/on-demand-tests
  two-way-delay-measurement/test-result[source-md-name, source-ma-name, source-mep-id]
  two-way-synthetic-loss-measurement/test-result[…]
  loopback/test-result[…]
  linktrace/test-result[…]
```

Interface VLAN-tag origin (multi-VLAN sub-if):

```
/dn-top:drivenets-top/dn-interfaces:interfaces/interface[name]/config-items/l2-originated-vlan-tags
  outer-tag, outer-tpid, inner-tag, inner-tpid
```

Key types: `ccm-interval-type` (`3.3ms|10ms|100ms|1sec|10sec|1min|10min`); `lowest-alarm-priority-type` (`all-def|mac-remote-error-xcon|remote-error-xcon|error-xcon|xcon|no-xcon`); `mep-defects-type` bits (`def-rdi-ccm|def-mac-status|def-remote-ccm|def-error-ccm|def-xcon-ccm`); `fng-state-type` (5 states above).

YANG revision history note: `dn-srv-connectivity-fault-management.yang` bumped from `2025-08-10 → 2025-12-31` for SW-216153; existing gNMI subscriptions may break across the upgrade — exercise this in ISSU tests.

---

## 6. SNMP — DRIVENETS-CFM-MIB

OID root: `1.3.6.1.4.1.49739.2.15`. SNMPv2c via `dn_community`, restricted to localhost. Piggybacks on DNOS system-event infrastructure: when a CFM system event is emitted, the corresponding SNMP trap is sent automatically — no dedicated CFM trap backend.

| Notification | OID | Trigger | Variables |
|---|---|---|---|
| `dnCfmFaultAlarm` | `…0.1` | `CFM_DEFECT_CONDITION_DETECTED` | `dnCfmMdName, dnCfmMaName, dnCfmMepIdentifier, dnCfmMepHighestPrDefect` |
| `dnCfmFaultAlarmCleared` | `…0.2` | `CFM_DEFECT_CONDITION_CLEARED` | `dnCfmMdName, dnCfmMaName, dnCfmMepIdentifier` |
| `dnCfmProactiveTestFailure` | `…0.3` | proactive test threshold violation | `dnCfmSourceMdName, dnCfmSourceMaName, dnCfmSourceMepId, dnCfmDmSessionId, dnCfmSlmSessionId` — defined but **not active in v25.4** |

On-demand result tables:
- `dnCfmOnDemandDmTestResultsTable` — OID `…1.15` — DM validity, DMM TX, DMR RX, success rate, min/avg/max two-way delay (µs), avg/max two-way IFDV (µs)
- `dnCfmOnDemandSlmTestResultsTable` — OID `…1.17` — SLM validity, SLM TX/RX counts, RemoteSlmReceived, ValidSlrReceived, UnacknowledgedSlr, LocalTxfc1Value, LocalRxfc1Value, LastSlrTxfcfTc, LastSlrTxfcbTc, near-end loss count + %, far-end loss count + %

The `dnCfmFaultAlarm` is modeled on IEEE 802.1ag `dot1agCfmFaultAlarm` (clause 12.14.7.7). Only the highest-priority defect is reported; if a higher-priority defect arrives after a trap was sent, another trap is sent. Cleared trap uses `dnCfmTrapHighestDefectPri.0 = 0`.

Known facts:
- Trap delivery is best-effort. If the SNMP agent is unavailable when the system event fires, the trap is **not** queued for retry. The DNOS alarm and syslog are independent.
- Proactive PM MIB tables (`dnCfmProactiveDmSessionTable / DmResultTable / SlmSessionTable / SlmResultTable`, OIDs `…1.18` through `…1.21`) are defined in the MIB tree but not populated in v25.4.

---

## 7. Failure modes / edge cases (what to test for)

- **Duplicate MEPID on the wire** → `DefErrorCCM` (received CCM's MEPID equals local MEPID).
- **Cross-connect** (different MAID or lower MD level) → `DefXconCCM`, immediate RDI on outgoing CCMs.
- **Auto-discovery overflow** → syslog at threshold; further discovered MEPs are silently dropped.
- **Threshold change at runtime while a defect is active** → both FNG timers reset; alarm may clear-then-raise.
- **Multi-VLAN without `l2-originated-vlan-tags`** → DM/SLM responses malformed (untagged on tagged service); CCM-only operation unaffected.
- **Long MD/MA names in system events** — combined > 251 chars truncates; long-string infra fix tracked separately.
- **SNMP agent unavailable when defect fires** — trap lost, no retry.
- **Unknown RMEP DMM/SLM** — silently dropped; no specific counter increment for this condition.
- **Remote MEP MAC not yet learned** when targeting `mep-id` — on-demand test fails to resolve destination.
- **VLAN tag mismatch** — DMM/SLM not processed correctly; applies to both Up and Down MEPs.
- **Partial SLR receipt** — frames after the last SLR cannot be attributed near vs. far end; reported as Missing SLR.
- **`RxTimeStampf / TxTimeStampb = 0` in DMR** — responder lacks HW PTP timestamping; initiator falls back to plain two-way RTT only.
- **ETH-LM + ETH-SLM both configured** on same MEP → HW errors; only one role per endpoint.
- **NCC role-switch under DMM at scale** (500-session target) → DMM:DMR resumes within the failover window; transient RDI possible at 3.3 ms CCM.
- **Save + full reload** → all sessions reprovision; first DMM should get DMR within ~5 s.

---

## 8. SW-216153 (PR #92176) weakness inventory — for SW-DMR-responder testing

This is the QA test plan for the dev_v26_3 SW responder migration. Use it as a checklist when writing tests against any build that includes PR #92176.

| # | Location | Weakness | Severity |
|---|---|---|---|
| W1 | `EthDmReflector.cpp:18-21` | Non-unicast SRC MAC dropped silently; `dmm_in` not incremented; no drop counter | H |
| W2 | `EthDmReflector.cpp:38` | DMR TX failure has no counter (no SysArch counter defined) | M |
| W3 | `EthDmReflector.cpp:30-35` | Reflector copies entire payload incl. malformed TLVs (only swaps MACs, sets opcode, fills `rx_timestampf`) | H |
| W4 | `bcm_wrap_cfm.c:2885-2900` | Up-MEP TOD recycle depends on BCM data key `oam.feature.up_mep_tod_recycle_enable`; `0` ⇒ `tx_timestampb=0` | H |
| W5 | `bcm_wrap_cfm.c:2942-2949` | Hard-coded `CFM_CORE_0_TOD_RECYCLE_GPORT` / `CFM_CORE_1_TOD_RECYCLE_GPORT` | M |
| W6 | `bcm_wrap_tx.c:284-290` | Magic offsets `20` + `(direction ? 2 : 0)` for HW Tx ts injection; assumes single VLAN tag | H |
| W7 | `bcm_wrap_packet_parser.c:564-573` | New `skip_udh_parsing` flag; STAMP/DM divergence | M |
| W8 | `bcm_wrap_rx.c:732` | 64-bit `rx_timestamp = (md.time_stamp << 32) \| md.rx_timestamp`; TOD-second rollover risk | M |
| W9 | `wb_rx_traps.h:259-260` | DMM trapped via `WB_RX_TRAP_NON_ACC_OAM_BFD` / `WB_RX_TRAP_OAM_UP_MEP_DEST1` shared with LBR/LT/BFD | H |
| W10 | `CfmLocalMep.cpp:38-42` | `*stats = {}` in `ClearStats()` zeros redis-key fields (`hw_id/md_id/ma_id/mep_id`) | H |
| W11 | `cfm_common.h` | `counters / counters_snapshot` race between packet handlers and counter manager thread | M |
| W12 | `dn-srv-connectivity-fault-management.yang` | 6 of 8 new leaves never populated by this PR | M |
| W13 | YANG revision 2025-08-10 → 2025-12-31 | Existing gNMI subscriptions may break across upgrade | L |
| W14 | RST docs vs CLI | RST shows DMM/DMR/SLM/SLR rows; CLI only adds DMM/DMR (doc bug, not code) | L |
| W15 | `test_cfm_manager.py:4602` | `sleep(1)` hack hides race in test info create/delete | M |
| W16 | `test_cfm_wrap_mep.py` | Pre-existing `test_cfm_dmr` deleted; HW reflector path no longer exercised | L |
| W17 | NCP3 dev test skipif | NCP3 without inter-unit SyncE skipped; behavior undocumented | M |
| W18 | `EthDmReflector.cpp` | `egress_object_id[*] = SDK_WRAP_TX_NO_EGRESS_OBJECT` bypasses encap entirely | H |
| W19 | YANG common stats | `unsupported-cfm-pdu` / `unicast-mac-mismatch` moved to common (now apply to MIPs too) | L |

Common pre-conditions for SW-216153 verification: build = Jenkins build implementing PR #92176 on `dev_v26_3` lineage; DUT = NCP3-SA + NCP4 (NCPL) + NCP6; Spirent TestCenter port reserved with Y.1731 PM emulation OR a secondary DNOS device with proactive Initiator (SW-208623); `clear … connectivity-fault-management statistics` between tests.

Common pass criteria (override only when noted):
- DMR observed at initiator within 100 ms of DMM
- Per-MEP `dmm-in == dmr-out == #DMM_sent`
- DMR `tx_timestampb > rx_timestampf`
- DMR `tx_timestampf` equals DMM `tx_timestampf` (faithful echo)
- DMR `res_rx_timestampb == 0`
- VLAN tag structure of DMR matches DMM
- `cfm_manager` and `wbox` core-dump free; `dmesg` clean

---

## 9. Related skills / files

- Use `dnos-ssh-connection` for paramiko connection details and DNOS prompt handling.
- Use `dnos-bulk-interface-config` for the "out-of-sync" commit prompt and many-interface enable patterns.
- Use `dnos-snmp-testing` for walking the DRIVENETS-CFM-MIB tables.
- Use `spirent-stc-connection` for STC LabServer port reservation and traffic stream definition (incl. Y.1731 emulation).
- For verifying CLI command paths, follow workspace rule `dnos-cli-verify-rst` — search `cheetah/prod/dnos_monolith/dnos_cli/**/*.rst` before scripting.
- Auto-nog corpus of working CFM/Y.1731 tests: `Auto-nog/tests/y1731/` (notably `cfm_between_machines.py`, `dnos_test.py`, `slm_evt_test.py`, the `test_sw236664_*` series for ETH-DM, the `test_sw236665_*` series for ETH-SLM, and `verify_bc10_*` for HW vs SW reflector verification).
