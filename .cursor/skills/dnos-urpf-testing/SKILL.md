---
name: dnos-urpf-testing
description: >-
  DNOS uRPF (Unicast Reverse Path Forwarding) CLI reference, testing patterns,
  Spirent traffic/protocol emulation workflows, counter verification, and known
  gotchas. Use when working with uRPF strict/loose mode, allow-default, per-AFI
  configuration, or any uRPF testing task on DNOS.
---

# DNOS uRPF Testing

## What Is uRPF

Unicast Reverse Path Forwarding validates the **source IP** of ingress packets
against the FIB. If the source has no valid reverse path, the packet is dropped.
Defined by RFC 2827 / RFC 3704.

- **Loose mode** (default): Source IP must have *any* FIB match (except Null0/discard).
- **Strict mode**: Source IP must match a FIB entry **and** the route must resolve
  via the **same interface** the packet arrived on.

In both modes, a route pointing to **Null0 / discard** always fails the uRPF check.

## Supported Interface Types

Bundle, Bundle VLAN sub-interface, Physical, Physical VLAN sub-interface, IRB.

## CLI Reference (from RST docs)

### Global uRPF on an Interface

```
configure
interfaces <IF>
  urpf
    admin-state enabled
    mode strict          # or loose (default)
    allow-default enabled  # or disabled (default)
  !
!
commit
```

Prompt progression example (bundle):
```
dnRouter(cfg-if)# bundle-1
dnRouter(cfg-if-bundle-1)# urpf
dnRouter(cfg-if-bundle-1-urpf)# admin-state enabled
dnRouter(cfg-if-bundle-1-urpf)# mode strict
dnRouter(cfg-if-bundle-1-urpf)# allow-default disabled
```

For sub-interfaces:
```
dnRouter(cfg-if)# ge400-0/0/3.100
dnRouter(cfg-if-ge400-0/0/3.100)# urpf
dnRouter(cfg-if-ge400-0/0/3.100-urpf)# admin-state enabled
dnRouter(cfg-if-ge400-0/0/3.100-urpf)# mode strict
```

### Per-AFI Configuration (Takes Precedence Over Global)

```
configure
interfaces <IF>
  urpf
    admin-state enabled
    mode loose                    # global fallback
    address-family ipv4
      admin-state enabled
      mode strict                 # IPv4 = strict
      allow-default disabled
    !
    address-family ipv6
      admin-state enabled
      mode loose                  # IPv6 = loose
      allow-default enabled
    !
  !
!
commit
```

Per-AFI settings **override** the global `mode` and `allow-default` for that
address family on the same interface.

### Removing Configuration

```
dnRouter(cfg-if-bundle-1)# no urpf           # remove all uRPF config
dnRouter(cfg-if-bundle-1-urpf)# no mode      # revert mode to default (loose)
dnRouter(cfg-if-bundle-1-urpf)# no allow-default   # revert to disabled
dnRouter(cfg-if-bundle-1-urpf)# no address-family ipv4  # remove per-AFI
```

### allow-default Behavior

- **disabled** (default): If the source IP resolves only via the default route,
  the packet is **dropped** by uRPF.
- **enabled**: The default route participates in the uRPF check. In strict mode,
  the default route's egress must equal the ingress interface. In loose mode,
  any interface is acceptable.
- `allow-default` must be **identical** across all uRPF-enabled interfaces (per RST docs).
- Even with `allow-default enabled`, a default route pointing to **Null0** still
  causes a drop.

### Static Routes (for test setup)

```
protocols static address-family ipv4-unicast route 10.10.10.0/24 next-hop 10.100.1.2
protocols static address-family ipv4-unicast route 198.51.101.0/24 discard       # Null0
no protocols static address-family ipv4-unicast route 10.10.10.0/24              # remove
```

### Aggregate Routes

```
protocols aggregate address-family ipv4-unicast route 10.40.0.0/16
no protocols aggregate address-family ipv4-unicast route 10.40.0.0/16
```

An aggregate route installs to Null0. uRPF uses **longest match**: if a more
specific contributing route resolves via the ingress interface, traffic passes.

### VRF-Scoped uRPF

uRPF config is always on the interface, not the VRF. Static routes in a VRF use:
```
network-services vrf instance <VRF> protocols static address-family ipv4-unicast route ...
```

### BGP Configuration (for route advertisement)

```
protocols bgp <AS> router-id <IP>
protocols bgp <AS> neighbor <PEER_IP> remote-as <PEER_AS>
protocols bgp <AS> neighbor <PEER_IP> admin-state enabled
protocols bgp <AS> neighbor <PEER_IP> address-family ipv4-unicast
```

### OSPF Configuration

```
protocols ospf instance <NAME> router-id <IP>
protocols ospf instance <NAME> area 0.0.0.0 interface <IF> network-type point-to-point
```

**Not** `protocols ospf <number>` — DNOS uses named instances, not numeric.

## Show / Verification Commands

| Command | Purpose |
|---------|---------|
| `show config interfaces <IF> urpf \| no-more` | Verify uRPF config |
| `show interfaces detail <IF> \| no-more` | Operational state — look for `uRPF IPv4 check: enabled, Mode: strict` |
| `show interfaces counters <IF> \| no-more` | Counter values — `RX packets:`, `uRPF Ipv4 drops:`, `uRPF Ipv6 drops:` |
| `show route vrf default table ipv4-unicast <PREFIX> \| no-more` | Verify route is installed |
| `show route vrf <VRF> table ipv4-unicast \| no-more` | VRF routing table |
| `show bgp ipv4 unicast summary \| no-more` | BGP neighbor state |
| `show ospf neighbor \| no-more` | OSPF adjacency state |
| `show isis adjacency \| no-more` | IS-IS adjacency state |

Always pipe through `| no-more` to avoid interactive paging.

## uRPF Drop Counter Verification Pattern

The core test pattern (before/after traffic):

```python
def extract_counter(text, label):
    for line in text.split('\n'):
        if label in line:
            val = line.split(':')[-1].strip().split('(')[0].strip().replace(',', '')
            try:
                return int(val)
            except ValueError:
                return 0
    return 0

def get_urpf_counters(chan, interface):
    out = dut_run(chan, f"show interfaces counters {interface} | no-more", 10)
    rx = extract_counter(out, "RX packets:")
    drops = extract_counter(out, "uRPF Ipv4 drops:")
    return rx, drops, out

# Before traffic
rx_before, drops_before, _ = get_urpf_counters(chan, SUB_IF)

# ... send traffic ...

# After traffic
rx_after, drops_after, counters_out = get_urpf_counters(chan, SUB_IF)
rx_delta = rx_after - rx_before
drop_delta = drops_after - drops_before

# Pass scenario: drops Δ == 0 and RX Δ > 0
# Drop scenario: drops Δ > 0 (typically drops Δ ≈ RX Δ)
```

Counter labels (case-sensitive as they appear in output):
- `RX packets:`
- `uRPF Ipv4 drops:`
- `uRPF Ipv6 drops:`
- `TX packets:` (to verify forwarded traffic reached egress)

## Spirent Traffic Generation Pattern

### Basic Stream (VLAN-tagged IPv4)

```python
from stcrestclient import stchttp

stc = stchttp.StcHttp('il-auto-containers', port=80)
# Clean old sessions, create new, attach port, then:

sb = stc.create('streamBlock', under=port1)
stc.config(sb, {
    'Name': 'urpf_test_stream',
    'FixedFrameLength': '128',
    'LoadUnit': 'FRAMES_PER_SECOND',
    'Load': '1000',
})
stc.apply()

eth = stc.get(sb, 'children-ethernet:EthernetII').split()[0]
stc.config(eth, {'srcMac': SRC_MAC, 'dstMac': DUT_MAC})

vlans_c = stc.get(eth, 'children-vlans').split()[0]
vlan = stc.create('Vlan', under=vlans_c)
stc.config(vlan, {'id': '100'})  # must match DUT sub-interface VLAN

ipv4 = stc.get(sb, 'children-ipv4:IPv4').split()[0]
stc.config(ipv4, {
    'sourceAddr': '10.10.10.1',   # source being tested
    'destAddr': '20.0.0.2',       # routable destination
    'ttl': '64',
})
stc.apply()
```

### Traffic Start / Stop

```python
gen = stc.get(port1, 'children-generator')
gen_cfg = stc.get(gen, 'children-generatorconfig')
stc.config(gen_cfg, {
    'SchedulingMode': 'PORT_BASED',
    'DurationMode': 'CONTINUOUS',
    'LoadUnit': 'FRAMES_PER_SECOND',
    'FixedLoad': '1000',
})
stc.apply()

stc.perform('GeneratorStart', params={'GeneratorList': gen})
time.sleep(12)
stc.perform('GeneratorStop', params={'GeneratorList': gen})
time.sleep(3)
```

### Spirent BGP Emulation

```python
dev = stc.create('EmulatedDevice', under=project,
                 **{'Name': 'BGP_Peer', 'EnablePingResponse': 'TRUE',
                    'RouterId': SPIRENT_IP})

eth = stc.create('EthIIIf', under=dev, **{'SourceMac': SRC_MAC})
vlan = stc.create('VlanIf', under=dev, **{'VlanId': '100'})
ip = stc.create('Ipv4If', under=dev,
                **{'Address': SPIRENT_IP, 'Gateway': DUT_IP,
                   'PrefixLength': '24'})
stc.config(ip, **{'StackedOnEndpoint-targets': vlan})
stc.config(vlan, **{'StackedOnEndpoint-targets': eth})
stc.config(dev, **{'TopLevelIf-targets': ip, 'PrimaryIf-targets': ip})
stc.config(port1, **{'AffiliationPort-sources': dev})

bgp = stc.create('BgpRouterConfig', under=dev,
                  **{'AsNum': str(SPIRENT_AS), 'DutAsNum': str(DUT_AS),
                     'IpVersion': 'IPV4', 'UseGatewayAsDut': 'TRUE'})
stc.config(bgp, **{'UsesIf-targets': ip})

bgp_rt = stc.create('BgpIpv4RouteConfig', under=bgp,
                     **{'NextHop': SPIRENT_IP, 'AsPath': str(SPIRENT_AS)})
blk = stc.get(bgp_rt, 'children-Ipv4NetworkBlock').split()[0]
stc.config(blk, {'StartIpList': '172.16.1.0', 'PrefixLength': '24',
                  'NetworkCount': '1'})
stc.apply()

stc.perform('ArpNdStartCommand', params={'HandleList': port1})
time.sleep(5)
stc.perform('DeviceStartCommand', params={'DeviceList': dev})
time.sleep(5)
```

Poll DUT: `show bgp ipv4 unicast summary | no-more` until neighbor shows
established (last column is a prefix count, not a state word).

### Spirent OSPF Emulation

```python
ospf = stc.create('Ospfv2RouterConfig', under=dev,
                   **{'AreaId': '0.0.0.0', 'NetworkType': 'P2P',
                      'Name': 'OSPF_Router'})
stc.config(ospf, **{'UsesIf-targets': ip})

ospf_rt = stc.create('Ospfv2ExternalLsaBlock', under=ospf)
blk = stc.get(ospf_rt, 'children-Ipv4NetworkBlock').split()[0]
stc.config(blk, {'StartIpList': '10.20.0.0', 'PrefixLength': '24',
                  'NetworkCount': '1'})
stc.apply()
```

Key points:
- Use `P2P` (not `POINT_TO_POINT`) for `NetworkType`.
- Set `RouterId` on the `EmulatedDevice`, not on `Ospfv2RouterConfig`.
- DUT OSPF uses named instances: `protocols ospf instance <name>`, not `protocols ospf <N>`.

## Test Scenarios Matrix (SW-244103 Pattern)

| # | Scenario | Source IP | Route Type | Expected | uRPF Action |
|---|----------|-----------|------------|----------|-------------|
| 1 | Config verify | — | — | uRPF strict enabled | — |
| 2 | Static pass | 10.10.10.1 | Static via ingress NH | Forward | drops Δ = 0 |
| 3 | Static drop | 192.0.2.1 | Static via different egress | Drop | drops Δ > 0 |
| 4 | Connected pass | 10.100.1.2 | Connected prefix on ingress | Forward | drops Δ = 0 |
| 5 | BGP pass | 172.16.1.1 | eBGP route via ingress peer | Forward | drops Δ = 0 |
| 6 | BGP drop | 172.16.2.1 | Route via different egress | Drop | drops Δ > 0 |
| 7 | OSPF pass | 10.20.0.1 | OSPF external via ingress | Forward | drops Δ = 0 |
| 8 | OSPF drop | 10.20.1.1 | Route via different egress | Drop | drops Δ > 0 |
| 9 | IS-IS drop | 10.30.0.1 | Route via different egress | Drop | drops Δ > 0 |
| 10 | Aggregate pass | 10.40.1.1 | More-specific contributing static via ingress | Forward | drops Δ = 0 |
| 11 | Null0 / discard | 198.51.101.1 | Static discard route | Drop | drops Δ > 0 |
| 12 | No route | 203.0.114.1 | No FIB entry | Drop | drops Δ > 0 |
| 13 | Cleanup | — | Remove test routes | — | — |

### "Drop" Shortcuts

For "drop via different egress" steps, you don't need full protocol emulation.
Install a static route via an interface that is NOT the ingress:

```
protocols static address-family ipv4-unicast route 172.16.2.0/24 next-hop 20.0.0.2
```

Then send traffic with source `172.16.2.1` into the uRPF-enabled ingress
interface — strict mode drops it because the reverse path resolves via a
different interface.

## Jira Epic Context

**Parent Test Category:** SW-243699 "Strict uRPF | Basic Functionality"

| Task | Summary | Status |
|------|---------|--------|
| SW-244097 | Counters increment on pass/drop (IPv4) on sub-interface | Test Passed |
| SW-244098 | To-US packets (control-plane destined) IPv4 | Test Passed |
| SW-244100 | Per-AFI CLI precedence (IPv4 strict / IPv6 loose) | Test Passed |
| SW-244102 | allow-default-route behavior v4/v6 on sub-interface | Test Failed |
| SW-244103 | IPv4 strict mode on sub-interface with different routing conclusions | Test Passed |
| SW-244104 | Strict mode transition from L2 -> L3 | Test Passed |
| SW-244105 | uRPF Strict allow-default enabled with Null0 default-route must drop | In Progress |
| SW-245154 | uRPF Loose | In Progress |

Related bugs / features: SW-196595 (strict mode), SW-107810 (counters).

## Known Gotchas and Lessons Learned

### DNOS CLI

1. **Not Cisco/Junos.** Never guess CLI paths. Always verify against RST docs at
   `cheetah/prod/dnos_monolith/dnos_cli/Interfaces/interfaces urpf*.rst`.
2. **`show running-config` does not exist.** Use `show config interfaces <IF> urpf | no-more`.
3. **OSPF uses named instances** (`protocols ospf instance urpf_test`), not
   numeric IDs (`protocols ospf 1` → `Unknown word '1'`).
4. **`no` goes at the start:** `no protocols static ...`, not `protocols static ... no next-hop`.
5. **`commit` is required** after any config change.

### Spirent

1. **VLAN tagging is mandatory** for sub-interface tests. Create a `Vlan` object
   under the stream's ethernet `vlans` container with `id` matching the DUT
   sub-interface VLAN.
2. **EmulatedDevice VLAN stacking** for protocol emulation: `Ipv4If` stacked on
   `VlanIf` stacked on `EthIIIf`, with `VlanId` matching the sub-interface.
3. **OSPF `NetworkType`**: Use `P2P`, not `POINT_TO_POINT`.
4. **Spirent OSPF external LSA**: The route must actually appear in the DUT RIB
   (`show route ... | no-more`). If it doesn't, the issue is Spirent config, not
   the DUT. Verify with `show ospf database external | no-more`.
5. **IS-IS Multi-Topology mismatch**: DNOS defaults to MT-enabled IS-IS (TLV 237
   for IPv6). Spirent defaults to non-MT (TLV 236). If IPv6 IS-IS routes are
   missing from the DUT, either enable MT on Spirent or disable MT on the DUT
   with `topology disabled` under `ipv6-unicast`.
6. **Spirent REST 405 errors** on TX counter reads are a known flake — retry.
7. **Windows STC GUI config** may not be visible in Lab Server REST sessions.

### uRPF Behavior

1. **Null0/discard always fails uRPF** in both strict and loose mode, even with
   `allow-default enabled`.
2. **Aggregate route to Null0** is fine as long as a **more specific contributing
   route** via the ingress interface exists (longest match wins).
3. **Per-AFI overrides global**: If `address-family ipv4 mode strict` is set, it
   takes precedence over the global `mode loose`.
4. **allow-default must match** across all uRPF-enabled interfaces.
5. **Counter label is case-sensitive**: Parse `uRPF Ipv4 drops:` (capital I,
   lowercase v4) and `uRPF Ipv6 drops:`.

## Reference Test Scripts

Located in workspace root (`/home/dn/`):

| Script | Description |
|--------|-------------|
| `test_sw244103_exec.py` | Steps 1-4, 10-13 (static/connected/aggregate/null0/no-route) |
| `test_sw244103_proto.py` | Steps 5-9 (BGP/OSPF/IS-IS with Spirent emulation) |
| `test_sw244103_recon.py` | Initial device reconnaissance |
| `test_sw244113_step*.py` | VRF-scoped uRPF testing (SW-244113) |
| `test_aggregate_urpf.py` | Dedicated aggregate route + uRPF testing |
| `tests/bugs/test_sw258863_breakout_urpf.py` | Breakout port uRPF testing |
| `tests/bugs/test_sw244118_urpf_cycle.py` | uRPF enable/disable cycling under route scale |
| `setup/configure_sw244116_urpf.py` | Sub-interface uRPF setup helper |
