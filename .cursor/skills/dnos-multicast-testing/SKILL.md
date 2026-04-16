---
name: dnos-multicast-testing
description: >-
  DNOS CLI syntax, PIM/multicast testing conventions, and Q3D platform
  behaviors. Use when working on multicast, PIM, IGMP, MFIB, or Spirent
  PIM SSM related testing tasks.
---

# DNOS Multicast / PIM Testing

## Interface Sub-interface Config

Sub-interfaces use `vlan-id` (NOT `encapsulation dot1q vlan-id`). Each block needs double `!` terminators — one to close the sub-interface, one to close the parent `interfaces` context:

```
configure
interfaces ge800-0/0/10.5
  admin-state enabled
  ipv4-address 3.5.6.1/24
  vlan-id 5
!
!
```

## PIM Enable on Interface (one-liner)

```
protocols pim address-family ipv4 interface ge800-0/0/10.5 admin-state enabled
```

## Common Show Commands

- `show pim summary` — route/replication counts, MFIB limit
- `show pim tree group <G> source <S>` — per-(S,G) IIF/OIF detail
- `show pim neighbors` — PIM adjacencies
- `show pim statistics` — Join/Prune RX/TX counters
- `show multicast route summary` — MFIB route counts + failed installs
- `show multicast route group <G> source <S>` — per-route forwarding counters
- `show multicast route failed` — routes that failed MFIB install
- `show system alarms` — system health
- `show config <section>` — running config (NOT `show running-config`)
- `show interfaces <if>` — interface state and counters

Pipe through `| no-more` to avoid paging. Commands like `show system cpu`, `show system memory`, `show logging last N`, `| count` are NOT supported on Q3D.

## Config Removal

Use `no` at the START of the line:

```
no protocols static address-family ipv4-unicast route 3.5.0.2/32 next-hop 3.5.1.2 interface ge800-0/0/10
```

NOT in the middle: `protocols static ... no next-hop ...`

## Device Details

- **Q3D-nog**: Management IP `100.64.6.171`, credentials `dnroot/dnroot`
- SSH via `paramiko` when MCP tool is unavailable
- Source interface: `ge800-0/0/31` (3.5.0.1/24, 800G)
- Receiver interface: `ge800-0/0/10` with VLAN sub-interfaces

## Known Platform Behaviors

- `ge800-0/0/31` may report "Physical link state: down / Remote Fault" while actively forwarding traffic — known state reporting bug
- PIM MFIB limit: 60,000 routes; replication limit: 220,000
- PIM MFIB threshold alarm at 45,000 routes
- `PIM_MAXIMUM_MFIB_ROUTES_LIMIT_REACHED` event may not fire (observed gap)
- IIF = OIF is allowed in multicast route table (potential PIM implementation gap)

## Spirent PIM SSM Join Config

When configuring Spirent PIM Groups for SSM joins:
- **Join Source Address**: Must be the actual multicast source IP (e.g., 3.5.0.2), NOT the DUT IP or Spirent's own IP
- **Join Source Prefix**: 32 (host route)
- **Starting Multicast Group Address**: SSM range 232.0.0.1+
- PIM Groups must be on the **receiver-facing** (downstream) Spirent port, NOT the source-facing port
