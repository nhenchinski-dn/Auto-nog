# Test Plan: Y.1731 ETH-DM Responder Migration to SW and Test ID TLV Support

**Epic:** [SW-216153](https://drivenets.atlassian.net/browse/SW-216153)
**Parent Initiative:** [SW-205679](https://drivenets.atlassian.net/browse/SW-205679) - DNOS as a Test Equipment (AT&T DNscope)
**Customer:** TATA - GDE
**Platforms:** NCP3-SA, NCP4 (NCPL)
**Fix Version:** v26.3, PI_1_2026, PI_2_2026
**Reference Test Plan:** [SW-141523](https://drivenets.atlassian.net/browse/SW-141523) (Y.1731 Proactive Initiator PM)
**Dev Assignee:** Vilian Postovaru
**QA Assignee:** Noga Henchinski

---

## 1. Feature Overview

### 1.1 What Changed

The Y.1731 ETH-DM (Delay Measurement) **responder** is being migrated from hardware (Broadcom OAMP) to software (CPU-based `cfm_manager`). Previously, when a DMM (Delay Measurement Message) arrived, the Broadcom chip reflected it back as a DMR (Delay Measurement Reply) entirely in hardware. This HW reflector has a **J2-family limitation**: it cannot preserve optional TLVs such as the **Test ID TLV (Type 36)** that Cisco NCS devices attach to DMM frames.

### 1.2 Why It Changed

Cisco NCS uses the Test ID TLV to correlate DMR responses back to active test profiles. Per ITU-T G.8013 section 8.2.2.2, every field in a DMM frame must be copied into the DMR. When DNOS's HW reflector dropped the Test ID TLV, Cisco silently discarded those DMR replies -- breaking interop for the TATA GDE deployment.

### 1.3 How It Works Now

```
DMM arrives on wire
      |
      v
[BCM ASIC punts DMM to CPU]  <-- SW-250192
      |
      v
[bcm_wrap_rx extracts HW Rx timestamp from ASE-timestamp + ToD]
      |
      v
[cfm_manager builds DMR]  <-- SW-250193
  - copies all TLVs from DMM (including Test ID TLV, preserving order)
  - inserts RxTimestampf from HW metadata
  - leaves TxTimestampb = 0 (filled by HW at egress)
      |
      v
[DMR injected to ASIC with OAM_TS header]  <-- SW-250195
  - OAM_Offset set so HW stamps TxTimestampb at egress
      |
      v
DMR exits on wire with all 4 timestamps + all TLVs intact
```

### 1.4 Key Technical Details

- **RxTimestampf** (64-bit): computed from system headers -- 30b from `TOD::Time_Stamp` and 34b from `FTMH_AES::OAM_TS_Data`; fallback: `bcm_oam_control_get(0, bcmOamControl1588ToD, ...)`
- **TxTimestampb** (64-bit): stamped by HW pipeline at egress using the `OAM_TS.OAM_Offset` system header
- **DownMEP**: CPU injects DMR with ITMH + OAM_TS header; HW stamps TxTimestampb at egress
- **UpMEP on NCP1 (J2)**: uses dedicated RCY (recycle) port for DM timestamping (`up_mep_tod_recycle_enable = 1`)
- **UpMEP on NCP3 (J2C+)**: no RCY port needed (`up_mep_tod_recycle_enable = 0`); pipeline stamps via FTMH_TSH + Latency calculation
- **DMM/DMR Counters**: new counters exposed in CLI and YANG/gNMI (global summary + per-MEP)

### 1.5 Scale Parameters

- **Target concurrent DMM responder sessions:** 500 (proactive, from TATA GDE RFP)
- **DMM probe interval:** 1 second
- **Max MEPs:** 1000 (theoretical limit)

---

## 2. Test Areas

Following the structure from [SW-141523](https://drivenets.atlassian.net/browse/SW-141523), testing is organized into these categories:

### 2.1 Sanity / Functional -- DMR Responder (SW-229208)

**Objective:** Verify the SW-based DMM responder correctly reflects DMR with accurate timestamps.

- **TC-FUNC-01: Basic DMR reflection -- DownMEP**
  - Configure CFM MD/MA/MEP (down) on a single-tagged VLAN sub-interface
  - Send DMM from remote peer (or on-demand initiator on a second DNOS device)
  - Verify DMR is received with:
    - All 4 timestamps populated (TxTimestampf, RxTimestampf, TxTimestampb, RxTimestampb)
    - RxTimestampf and TxTimestampb are non-zero and plausible (within expected delay range)
    - All original DMM TLVs preserved in DMR (including End TLV)
  - Verify DMM/DMR counters increment
- **TC-FUNC-02: Basic DMR reflection -- UpMEP**
  - Same as TC-FUNC-01 but with Up MEP configuration
  - Pay special attention to TxTimestampb accuracy (UpMEP uses different stamping mechanism per platform)
  - Test on both NCP1 (J2 -- uses RCY port) and NCP3 (J2C+ -- direct pipeline stamping) if available
- **TC-FUNC-03: DMR with double-tagged VLAN (S,C)**
  - Configure CFM on a double-tagged `<s-tag, c-tag>` L2 sub-interface
  - Verify DMR reflection works correctly
  - Verify timestamps accuracy
- **TC-FUNC-04: DMR with VLAN list sub-interface**
  - Configure CFM on a VLAN list sub-interface
  - Verify DMR works correctly
- **TC-FUNC-05: DMR with CCM enabled vs disabled**
  - Test DMR reflection when CCM (Continuity Check Messages) is enabled between MEPs
  - Repeat with CCM disabled (no remote-mep configured)
  - Verify no interference between CCM and DM responder
- **TC-FUNC-06: DMR with different PCP values**
  - Send DMM with different PCP values (0, 3, 7)
  - Verify DMR preserves the PCP value from the DMM
- **TC-FUNC-07: Multiple simultaneous DMM sessions on same MEP**
  - Configure a MEP and have multiple remote peers send DMM simultaneously
  - Verify all DMRs are correctly reflected with proper timestamps and no cross-session contamination
- **TC-FUNC-08: DMR with various L2 service types**
  - Test over: EVPN-VPWS, E-LAN, Bridge Domain, VPWS, EVPN-E-tree, EVPN-VPWS FXC
  - Verify DMR works correctly on each service type
- **TC-FUNC-09: DMM to non-existent MEP**
  - Send DMM targeting an MEP ID that does not exist on the DUT
  - Verify no DMR is sent back, no crash
- **TC-FUNC-10: DMM with wrong MEL (Maintenance Entity Level)**
  - Send DMM at wrong MEG level
  - Verify DUT handles per standard (forward or drop based on level)
- **TC-FUNC-11: DMM with oversized payload**
  - Send DMM exceeding interface MTU
  - Verify behavior (drop or fragment per standard)
- **TC-FUNC-12: DMM with invalid OpCode**
  - Send an OAM PDU with incorrect OpCode
  - Verify DUT ignores it
- **TC-FUNC-13: Rapid MEP flap while DMM in flight**
  - Delete and recreate MEP config while DMM traffic is flowing
  - Verify no crash, counters are consistent after re-creation

### 2.2 Test ID TLV Support (SW-229210)

**Objective:** Verify the optional Test ID TLV (Type 36) is properly handled.

- **TC-TID-01: Responder reflects Test ID TLV from DMM to DMR**
  - Send DMM containing a Test ID TLV from a Cisco NCS (or simulated peer)
  - Verify DMR contains the exact same Test ID TLV, unmodified
  - Verify TLV order is preserved (Test ID TLV position relative to other TLVs)
- **TC-TID-02: Responder handles DMM without Test ID TLV**
  - Send DMM without a Test ID TLV (backward compatibility)
  - Verify DMR does not contain a Test ID TLV
  - Verify timestamps are still correct
- **TC-TID-03: Responder handles DMM with Test ID TLV + Data TLV**
  - Send DMM with both Test ID TLV and Data TLV
  - Verify both TLVs are preserved in DMR in the same order
  - Verify TLV boundaries are correct (no corruption)
- **TC-TID-04: Responder handles DMM with multiple optional TLVs**
  - Send DMM with Test ID TLV, Data TLV, and Organization-Specific TLV
  - Verify all TLVs preserved in order in DMR
- **TC-TID-05: Responder handles DMM with only End TLV (no optional TLVs)**
  - Send DMM with just the End TLV (Type 0)
  - Verify DMR is correct with just the End TLV
- **TC-TID-06: Interop -- Cisco NCS as DMM initiator, DNOS as responder**
  - Use actual Cisco NCS device sending proactive DMM with Test ID TLV
  - Verify Cisco NCS successfully receives and correlates DMR responses
  - Verify delay measurements on Cisco side show valid results
  - (This requires TATA GDE lab or Cisco interop setup)
- **TC-TID-07: Malformed DMM (truncated Test ID TLV)**
  - Send a DMM with a truncated Test ID TLV (length field exceeds actual data)
  - Verify DUT does not crash, logs appropriate error, drops or handles gracefully

### 2.3 DMM/DMR Counters (SW-229213)

**Objective:** Verify DMM and DMR counters are correctly exposed and incremented.

- **TC-CTR-01: Global counter view**
  - Send multiple DMMs
  - Verify `show services ethernet-oam connectivity-fault-management summary` displays correct DMM received count and DMR sent count
- **TC-CTR-02: Per-MEP counter view**
  - Send DMMs targeting different MEPs
  - Verify `show services ethernet-oam connectivity-fault-management maintenance-domains maintenance-associations meps` shows correct per-MEP DMM/DMR counters
- **TC-CTR-03: Counter increment accuracy**
  - Send N DMM packets
  - Verify DMM-received counter = N
  - Verify DMR-sent counter = N
  - Repeat with different values of N (1, 10, 100, 1000)
- **TC-CTR-04: Counter persistence across session stop/start**
  - Send some DMMs, note counters
  - Stop and restart CFM session
  - Verify counters are reset or preserved per design
- **TC-CTR-05: gNMI counter verification**
  - Query DMM/DMR counters via gNMI
  - Verify they match CLI counters

### 2.4 CLI Tests

**Objective:** Verify all CLI show commands and configurations related to the DMR responder and counters.

- **TC-CLI-01: Show CFM summary with DMM/DMR counters**
  - `show services ethernet-oam connectivity-fault-management summary | no-more`
  - Verify new DMM-received and DMR-sent counter fields are present and correct
- **TC-CLI-02: Show MEP detail with DMM/DMR counters**
  - `show services ethernet-oam connectivity-fault-management maintenance-domains maintenance-associations meps | no-more`
  - Verify per-MEP DMM/DMR counters in output
- **TC-CLI-03: CLI output matches documentation**
  - Compare actual CLI output against Yang model/documentation for DMM/DMR counter fields
  - Verify field names, formatting, and units are correct
- **TC-CLI-04: TAB completion for new counter fields**
  - Verify TAB completion works for new CLI paths related to DMM/DMR counters
  - Test `show services ethernet-oam <TAB>` progression

### 2.5 RESTCONF / NETCONF / gNMI Tests

**Objective:** Verify DMM/DMR counter data is accessible via management interfaces.

- **TC-REST-01: GET DMM/DMR counters via RESTCONF**
  - Query the operational data path for CFM DMM/DMR counters
  - Verify HTTP 200 and correct JSON/XML payload
- **TC-REST-02: GET DMM/DMR counters via NETCONF**
  - Issue a NETCONF `get` for the DMM/DMR counter YANG path
  - Verify correct response
- **TC-REST-03: GET DMM/DMR counters via gNMI**
  - Subscribe or get the DMM/DMR counter path via gNMI
  - Verify counters are accurate

### 2.6 Scale and Performance

**Objective:** Verify the SW responder handles target scale without degradation.

- **TC-SCALE-01: 500 concurrent DMM responder sessions**
  - Configure 500 MEPs on the DUT, each with a remote peer sending DMM at 1-second intervals
  - Verify all 500 sessions receive valid DMR responses
  - Measure CPU utilization during the test
  - Run for 30+ minutes
- **TC-SCALE-02: Maximum MEP count (1000) with DMM**
  - Ramp up to 1000 MEPs with DMM traffic
  - Monitor for DMR drops, timestamp errors, CPU exhaustion
- **TC-SCALE-03: DMM burst handling**
  - Send a burst of DMMs (multiple DMMs within < 1ms)
  - Verify all are reflected correctly (no drops, no corruption)
- **TC-SCALE-04: Sustained 1-second interval probe**
  - Run 500 sessions at 1-second DMM interval for 24+ hours
  - Check for memory leaks, counter overflow, timestamp drift
  - Monitor system stability (no crashes, no OOM)
- **TC-SCALE-05: CPU impact measurement**
  - Baseline CPU with 0 DM sessions
  - Measure CPU with 100, 200, 500 DMM sessions at 1-second interval
  - Verify CPU does not exceed acceptable thresholds

### 2.7 Platform-Specific Tests

**Objective:** Verify correct behavior on both target chipsets.

- **TC-PLAT-01: NCP3-SA (J2C+) DownMEP DMR timestamps**
  - Verify RxTimestampf and TxTimestampb accuracy on NCP3
  - Verify `up_mep_tod_recycle_enable = 0` path works
- **TC-PLAT-02: NCP3-SA (J2C+) UpMEP DMR timestamps**
  - Verify UpMEP timestamp stamping via pipeline (no RCY port)
  - Cross-check FTMH_TSH + Latency for TxTimestampb
- **TC-PLAT-03: NCP4 (NCPL/J2) DownMEP DMR timestamps**
  - Verify DownMEP flow on NCP4/J2
  - Verify CPU injection with ITMH + OAM_TS header
- **TC-PLAT-04: NCP4 (NCPL/J2) UpMEP DMR timestamps**
  - Verify UpMEP uses RCY port (`up_mep_tod_recycle_enable = 1`)
  - Verify TxTimestampb accuracy through recycle path

### 2.8 Regression / Coexistence

**Objective:** Verify the change does not break existing functionality.

- **TC-REG-01: On-demand DM sessions still work**
  - Run on-demand DMM initiator test (existing functionality)
  - Verify delay measurements are correct
  - Verify existing show commands still display results
- **TC-REG-02: Proactive DM sessions still work**
  - Configure proactive DM session (from SW-141523 feature)
  - Verify proactive sessions produce correct results
  - Verify no interference between proactive initiator and SW responder on same device
- **TC-REG-03: SLM sessions unaffected**
  - Run SLM on-demand and proactive sessions
  - Verify they continue to work (SLM responder is separate from DM responder)
- **TC-REG-04: CCM / CFM fault management unaffected**
  - Verify CCM, LBM/LBR, LTM/LTR still work
  - Verify defect detection and alarms are not impacted
- **TC-REG-05: Interaction with on-demand DM show commands**
  - Per comment from Noga on SW-141523: verify on-demand results are filterable by MD/MA context when multiple sessions exist
  - `show services performance-monitoring cfm tests on-demand two-way-delay ...`
- **TC-REG-06: Upgrade/downgrade path**
  - Upgrade from a version with HW responder to the new SW responder version
  - Verify DMR continues to work after upgrade without re-configuration
  - Verify rollback to previous version restores HW responder behavior

### 2.9 Stability / Longevity

**Objective:** Long-running tests to uncover memory leaks, counter overflow, and stability issues.

- **TC-LONG-01: 72-hour DMR responder longevity**
  - Run 200+ DMM sessions at 1-second interval for 72 hours
  - Monitor: CPU, memory, counter values, timestamp accuracy
  - Verify no degradation over time
- **TC-LONG-02: DMR under HA failover**
  - Trigger NCC failover while DMM traffic is active
  - Verify DMR responses resume after failover
  - Verify counters are consistent

---

## 3. Test Environment

- **DUT Platforms:** NCP3-SA (J2C+), NCP4/NCPL (J2)
- **Peer Device:** Second DNOS device (as DMM initiator) and/or Cisco NCS (for interop)
- **L2 Services:** EVPN-VPWS, Bridge Domain, at minimum
- **Interfaces:** Single-tagged, double-tagged, VLAN list
- **MEP types:** DownMEP and UpMEP
- **Management:** CLI, RESTCONF, NETCONF, gNMI, SNMP

---

## 4. Risks and Concerns

- **Timestamp accuracy**: The SW responder introduces CPU processing latency between DMM arrival and DMR departure. While RxTimestampf is HW-stamped at ingress and TxTimestampb is HW-stamped at egress, the processing time in between could introduce jitter. Need to validate that delay measurements remain within acceptable margins.
- **Scale**: Moving from HW to SW responder means CPU must handle every DMM packet. At 500 sessions with 1-second interval, that is 500 packets/second. CPU budget must be validated.
- **NCP1 (J2) vs NCP3 (J2C+) differences**: The UpMEP timestamping mechanism differs (RCY port vs pipeline). Both paths need dedicated testing.
- **Dev ETA slippage**: Dev ETA was postponed to April 10, 2026 (from March 30). Current status shows code items as Done but umbrella stories are still "To Do" -- likely awaiting integration.
- **Interaction with Proactive Initiator (SW-141523)**: The proactive initiator feature is in SIT. Both features share the `cfm_manager` component. Regression testing is critical.

---

## 5. Out of Scope

- DMM Initiator SW implementation (covered by [SW-208623](https://drivenets.atlassian.net/browse/SW-208623))
- Dual-ended (one-way) DM -- only single-ended (two-way) is supported
- LMM (Loss Measurement Message) -- not supported in DNOS
- ETH-AIS (Alarm Indication Signal) -- separate feature
- Multi-point/Multicast loopback -- separate feature

---

## 6. Test Tracking

| Test Area                      | Estimated Cases | Priority |
| ------------------------------ | --------------- | -------- |
| Sanity / Functional (2.1)      | 13              | P1       |
| Test ID TLV (2.2)              | 7               | P1       |
| DMM/DMR Counters (2.3)         | 5               | P1       |
| CLI (2.4)                      | 4               | P1       |
| RESTCONF/NETCONF/gNMI (2.5)    | 3               | P2       |
| Scale and Performance (2.6)    | 5               | P1       |
| Platform-Specific (2.7)        | 4               | P1       |
| Regression / Coexistence (2.8) | 6               | P1       |
| Stability / Longevity (2.9)    | 2               | P2       |
| **Total**                      | **49**          |          |
