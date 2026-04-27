---
name: dnos-cfm-y1731
description: DNOS CFM (IEEE 802.1ag) and Y.1731 OAM reference — baseline configuration, mandatory pre-conditions, on-demand and proactive Performance Monitoring (ETH-DM, ETH-SLM), platform/version support matrix, hardware limitations, and test-script patterns. Use when configuring CFM/CCM, MEPs, MIPs, LBM/LTM, ETH-DM (DMM/DMR), ETH-SLM (SLM/SLR), Y.1731 MEG-ID/ICC-based naming, or when writing tests for any of these.
---

# DNOS CFM / Y.1731

CFM (IEEE 802.1ag) is DNOS's L2 OAM stack for connectivity-fault detection over Ethernet services (VPWS, EVPN, EVPN-VPWS, Bridge-Domain). Y.1731 layers Performance Monitoring (delay + synthetic loss) on top of the same MEP/MEG hierarchy.

Always verify CLI command paths in the RST docs before scripting (workspace rule). DNOS CFM hierarchy is **not** Cisco-style — see baseline below.

## What it does (summary)

| Capability | Direction | Standard | DNOS feature |
|---|---|---|---|
| Continuity Check (CCM) | TX + RX | 802.1ag | Always-on for enabled MEP |
| Loopback (LBM/LBR) | Initiator + responder | 802.1ag | On-demand `run` |
| Linktrace (LTM/LTR) | Initiator + responder | 802.1ag | On-demand `run` |
| ETH-DM (DMM/DMR) | Initiator + responder | Y.1731 | On-demand and proactive |
| ETH-SLM (SLM/SLR) | Initiator + responder | Y.1731 | On-demand and proactive (1 session/MEP) |
| MIP (LBM/LTM responder) | Responder only | 802.1ag | CPU path |
| Auto-discovery of RMEPs | — | 802.1ag | Per-MA toggle, system cap |
| ICC-based MEG-ID | — | Y.1731 | `short-ma-name icc-based <icc> <umc>` |
| MY-CFM-MAC BGP advertise | — | DN-specific | Type-2 sticky on first unicast reply |

Out of scope on DNOS: **dual-ended PM (1DM, 1SL), ETH-LM (LMM/LMR), ETH-AIS/MCC/LCK, MIP creation logic (MHF), Sender ID TLV, multicast DMM/SLM, EFD `oper-down` action.**

## Up-MEP vs Down-MEP (the directionality model)

Pick the wrong direction and CFM will commit cleanly but never see a frame. Direction is set per local MEP via `direction {up|down}`.

```
                           ┌──────────────────── DNOS box ────────────────────┐
                           │                                                  │
                           │  ┌──────────── bridge-relay /                ┐  │
                           │  │            L2 service (BD/VPWS/EVPN/…)     │  │
                           │  │                                            │  │
   wire ─── port A ───────►│──┤                                            │──│───── port B ───── wire
            ▲              │  │  ▲                                         │  │       ▲
            │              │  │  │                                         │  │       │
       Down-MEP            │  │  Up-MEP (faces the relay)                  │  │   Down-MEP
       (faces the wire)    │  └────────────────────────────────────────────┘  │   (faces the wire)
                           │                                                  │
                           └──────────────────────────────────────────────────┘
```

| | **Down-MEP** | **Up-MEP** |
|---|---|---|
| Faces | Outward — the physical wire on the bound interface | Inward — the bridge relay / L2 service |
| Where its CCMs are emitted | Out the bound (sub-)interface, onto the wire | Into the bridge relay → out the **other** member ports of the same L2 service |
| Where it processes received CCMs | Frames arriving on the bound (sub-)interface from the wire | Frames arriving into the bridge relay from the other side, destined to the bound (sub-)interface |
| Bind point | Physical port, bundle, physical VLAN sub-if, bundle VLAN sub-if | Same set, but the (sub-)interface must be a member of an L2 service |
| Canonical use case | **PE-CE link monitoring** — DNOS PE port toward CE; one MEP at each end of the link | **PE-PE service monitoring** — end-to-end across the provider core (any number of P-routers in between); MEPs sit at the service edges |
| Forwarding plane requirement on this box | None for a physical port; bridge-domain / xc / VPWS / EVPN if on an L2 sub-if | Always — Up-MEP only "exists" through a working L2 service on the box (BD, VPWS, EVPN, EVPN-VPWS, EVPN-E-LAN) |
| Sees frames terminating on its own port? | Yes (the link is what it monitors) | No — those terminate before reaching the relay |
| Sees frames passing through to other service members? | No | Yes (its whole purpose) |
| Multi-VLAN tag handling | Inherits sub-if tag config; no extra config needed | Requires `interfaces … l2-originated-vlan-tags` on multi-VLAN sub-ifs — without it OAMP-generated DMR/SLR/LBR ship untagged on a tagged service |
| Y.1731 ETH-DM HW timestamping (`tx_timestampb`) | Works without recycle-port plumbing | NCP6/BCM88690 requires two BCM recycle ports (one per core) configured `INJECTED_2_PP`, **and** BCM data key `oam.feature.up_mep_tod_recycle_enable = 1`; if `0` then `tx_timestampb = 0` in DMRs (PR #92176 W4) |
| MY-CFM-MAC BGP type-2 advertisement (EVPN-LAN/E-TREE) | Not relevant (link-local) | Triggered by first unicast CFM reply (DMR / SLR / LTR) — important when CCM TX is disabled, otherwise initial unicast PM probes get flooded |
| Counters / show paths | Identical (both report under `…/meps/<id>`) | Identical |

Pick **Down** for any "is the link to the customer up and clean" question. Pick **Up** for any "is the L2 service end-to-end healthy across the core" question. The two perspectives are layered as different MD levels in real deployments — customer/EVC level (Up-MEPs) at a higher MD level, link/operator level (Down-MEPs) at a lower MD level on the same physical port — so CCMs of the inner (lower-level) domain pass transparently through the outer Up-MEPs and the two state machines stay independent.

## Must-haves for CFM to actually work

These are the most common reasons "CFM is configured but not running":

1. **Forwarding context.** A Down-MEP on an L2 sub-interface (`ge400-0/0/X.Y`) **must** be attached to a forwarding instance — a `network-services bridge-domain instance <name>`, `l2-cross-connect`, EVPN-VPWS/E-LAN, or VPWS service. Without it CCM/DMM/DMR PDUs never leave the wire even though `l2-service enabled` is set. (The Auto-nog `cfm_between_machines.py` helper enforces this — see baseline-config.md.)
2. **L2 service on the (sub-)interface.** Set `l2-service enabled` on the sub-interface; otherwise the MEP binds but no Ethernet path exists.
3. **MEP direction.** `direction down` for PE-CE link monitoring (CCMs go out the wire), `direction up` for PE-PE service monitoring (CCMs go into the bridge relay). See the Up-MEP vs Down-MEP table above. Up-MEPs additionally need `l2-originated-vlan-tags` on multi-VLAN sub-interfaces, and Up-MEP DM HW timestamping needs the BCM recycle-port plumbing (reference.md → "Up-MEP HW path").
4. **MD level matches.** Both ends must use the same `level` (0–7); CCMs at the wrong level are dropped and counted in `Wrong MD Level`. Multicast DMAC last nibble = MD level.
5. **MA `short-ma-name` matches** between peers; a mismatch produces `DefXconCCM` (cross-connect, highest priority).
6. **Remote-MEP entry.** Either `crosscheck mep-id <id>` per peer **or** `auto-discovery enabled`. ETH-DM/ETH-SLM responders **require** the RMEP to be known; DMM/SLM from unknown peers are silently dropped.
7. **Y.1731 ICC-based MEG-ID requires `md-name null`.** The `short-ma-name icc-based ICC UMC` form is rejected with any other `md-name` format.
8. **For multi-VLAN UP MEPs:** configure `interfaces … l2-originated-vlan-tags` (outer-tag/inner-tag with TPID) — without it the OAMP-generated DMR/SLR/LBR ships untagged on a tagged service.
9. **MEP-ID uniqueness.** A local MEP's `mep-id` must not match any configured RMEP in the same MA — the BCM OAMP treats it as `DefErrorCCM`.
10. **CPRL.** CFM PDUs are rate-limited (1000 pps / 1000 burst default). At high scale tune `system cprl` accordingly.

## Baseline Down-MEP between two DNOS devices

This is the verified minimal config that produces a working CCM session (matches the `WKY1C7VD00008P2` config backup and the `cfm_between_machines.py` helper):

```
configure
  interfaces ge400-0/0/33.1
    admin-state enabled
    vlan-id 1
    l2-service enabled
  exit
  network-services bridge-domain instance bd-cfm
    interface ge400-0/0/33.1
  exit exit
  services ethernet-oam connectivity-fault-management
    maintenance-domains MD-CUST
      level 1
      md-name string md-cust
      maintenance-associations MA-CUST
        short-ma-name string ma-cust
        local-mep 2
          admin-state enabled
          direction down
          interface ge400-0/0/33.1
        exit
        remote-meps
          crosscheck mep-id 1
        exit
      exit
    exit
  exit
commit and-exit
```

The peer device uses `local-mep 1` and `crosscheck mep-id 2`; everything else mirrors. Replace `bridge-domain bd-cfm` with `l2-cross-connect`, EVPN-VPWS, or any other L2 service if that's the real deployment topology.

## Quick CLI reference

| What | Where |
|---|---|
| Top-level config | `services ethernet-oam connectivity-fault-management` |
| MD | `maintenance-domains <md-id>` → `level <0-7>`, `md-name {string\|dns\|mac-address\|null}` |
| MA | `maintenance-associations <ma-id>` → `short-ma-name {string\|vlan-id\|number\|vpn-id\|icc-based <icc> <umc>}` |
| CCM tuning | `continuity-check` → `interval`, `loss-threshold`, `transmit`, `fault-alarm` |
| Local MEP | `local-mep <1-8191>` → `interface`, `direction {up\|down}`, `admin-state`, `pcp` |
| MIP | `mip <name>` → `interface`, `admin-state` |
| Remote MEPs | `remote-meps` → `crosscheck mep-id <n>`, `auto-discovery {enabled\|disabled}` |
| On-demand DM | `run ethernet-oam cfm on-demand delay-measurement two-way maintenance-domain MD maintenance-association MA target {mep-id N \| mac-address X} [interval {1\|10\|60\|600}] [count 1-65535] [detail]` |
| On-demand SLM | `run ethernet-oam cfm on-demand synthetic-loss-measurement maintenance-domain MD maintenance-association MA target {mep-id N \| mac-address X} [interval ...] [count ...] [pcp 0-7]` |
| On-demand LBM | `run ethernet-oam cfm on-demand loopback maintenance-domain MD maintenance-association MA target {mep-id N \| mac-address X} [count] [pdu-size]` |
| On-demand LTM | `run ethernet-oam cfm on-demand linktrace maintenance-domain MD maintenance-association MA target mep-id N [ttl]` |
| Proactive DM | `services performance-monitoring cfm two-way-delay-measurement <session> profile <p> source maintenance-domain MD maintenance-association MA mep-id <id> target mep-id <id> admin-state enabled` |
| Proactive SLM | same hierarchy, `two-way-synthetic-loss-measurement <session>` |
| PM profiles | `services performance-monitoring profiles cfm two-way-{delay,synthetic-loss}-measurement <p> test-duration probes probe-count N probe-interval S repeat-interval S` |
| Show summary | `show services ethernet-oam connectivity-fault-management summary` |
| Show MEP detail | `show services ethernet-oam connectivity-fault-management maintenance-domains MD maintenance-associations MA meps {<id>\|local\|remote\|all}` |
| Show by interface | `show services ethernet-oam connectivity-fault-management interfaces [<if>]` (v19.2+) |
| Show DM/SLM result | `show services performance-monitoring cfm tests {on-demand\|proactive} two-way-{delay,synthetic-loss} session-name <s> [detail]` |
| Clear stats | `clear services ethernet-oam connectivity-fault-management statistics {<if>\|maintenance-domain MD [maintenance-association MA [mep-id N]]\|all}` |

## Platform / version matrix

| Capability | NCP6 / NCP4 (NCPL) Standalone | NCP3-SA | Cluster |
|---|---|---|---|
| CFM essentials (CCM, LB, LT, MIP) | v19.1 | v19.1 | not supported (SW-129152 epic open) |
| Multi-VLAN / `l2-originated-vlan-tags` | v19.2 | v19.2 | n/a |
| Y.1731 Responder (DMM/DMR, SLM/SLR) | v19.2 | v25.1 (SW-148407) | n/a |
| Y.1731 On-demand Initiator (DM, SLM) | v19.3 | v25.1 | n/a |
| Y.1731 Proactive Initiator | dev_v26.x (SW-208623) | dev_v26.x | n/a |
| SW DMR responder + Test ID TLV (Cisco interop) | dev_v26_3 (SW-216153 / PR #92176) | dev_v26_3 | n/a |
| DRIVENETS-CFM-MIB (SNMP on-demand DM/SLM) | v25.4 | v25.4 | n/a |
| DNOS alarm `CFM_DEFECT_CONDITION_DETECTED` | v25.1 | v25.1 | n/a |

CCM intervals supported: `3.3ms, 10ms, 100ms, 1sec (default), 10sec, 1min, 10min`. PM probe intervals: `1, 10, 60, 600` seconds (100 ms waived for SLM).

## Top-10 gotchas (ordered by how often they bite)

1. **Down-MEP without a forwarding instance** — CCM never leaves the wire. Always attach the sub-if to a bridge-domain / l2-cross-connect / VPWS / EVPN service.
2. **DMM/SLM from unknown RMEP** is silently dropped — no counter, no DMR. Configure `crosscheck mep-id` or enable `auto-discovery`.
3. **Multicast DMAC for DMM/SLM is dropped.** Only unicast DMAC matching the MEP MAC is accepted.
4. **Optional TLVs not reflected in DMR** on the HW responder (BCM OAMP). The new SW responder (PR #92176, dev_v26_3) preserves payload bit-exact, including Test ID TLV — used for Cisco NCS interop.
5. **One ETH-SLM session per MEP** (BCM hardware: only Test ID = 0; multi-Test-ID rejected as SW-144512/144513/144600). ETH-SLM initiator and responder roles cannot coexist on the same MEP.
6. **No PCP for DMM** (BCM OAMP limitation, CS00012355451). PCP is configurable for SLM only.
7. **`md-name` must be `null`** to use `short-ma-name icc-based`.
8. **Per-opcode DMM/DMR/SLM/SLR HW counters are not exposed** (BCM CRPS exhaustion CS00012340847). YANG leaves exist but are 0. The new SW responder counts `dmm_in` and `dmr_out` per-MEP (SW-229213, exposed via existing `summary` and `meps` show); the other 6 leaves remain 0.
9. **Up-MEP on Q-in-Q without `l2-originated-vlan-tags`** → malformed DMR (untagged on a tagged service). Always set outer + inner tag with TPID.
10. **CCM sequence number is always 0** (BCM OAMP cannot increment, CS00012305249). Do not assert sequence-number monotonicity in tests.

## Counters and visibility

Per-MEP PDU counters (always populated): CCM TX/RX, LBM TX, LBR RX, LTM TX/RX, LTR TX/RX. Plus SW responder: `dmm_in`, `dmr_out` (v26.3+).

Per-MEP error counters: `Wrong MD Level`, `CCM Wrong Interval`, `CCM Wrong Remote-MEP`, `CCM Wrong MAID`, `Unknown CFM PDUs`, `Unicast MAC Mismatch`, `RX Passive Side`.

System events: `CFM_DEFECT_CONDITION_DETECTED` / `CLEARED` (group `CFM-OAM`, trigger component `WB Agent`). SNMP traps `dnCfmFaultAlarm` / `dnCfmFaultAlarmCleared` (DRIVENETS-CFM-MIB, OID `1.3.6.1.4.1.49739.2.15.0.{1,2}`) piggyback on the system event.

Defect priority (highest → lowest, FNG): `DefXconCCM` (5) → `DefErrorCCM` (4) → `DefRemoteCCM` (3) → `DefMACStatus` (2) → `DefRDICCM` (1).

Scale ceilings: 2000 MDs system-wide; 1 local MEP per MA; 2048 combined CFM/BFD/Y.1731 sessions in the MEP-DB; 200 concurrent PM sessions (Y.1731 v19.2); 1000 ETH-DM responder sessions (NCP6); 16 concurrent on-demand initiator CLI sessions; 500-session @ 1 s sustained PM target (epic SW-216153). Auto-discovery default cap: 2048 RMEPs (configurable 0–8191).

## More

- For full working configs (down-MEP, up-MEP, on-demand DM/SLM, proactive PM, ICC-based MEG-ID), see [examples.md](examples.md).
- For deep technical reference (every limitation, every BCM CS#, every YANG path, FNG state machine, defect taxonomy, full validation list, Auto-nog test-script idioms), see [reference.md](reference.md).
