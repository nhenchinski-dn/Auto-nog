---
name: spirent-stc-connection
description: >-
  Connect to a Spirent TestCenter chassis via Lab Server REST API using
  stcrestclient, reserve ports, configure L1/devices/traffic, and run
  traffic flows. Use when working with Spirent, traffic generation,
  stcrestclient, STC, OTG, or port reservation on a Spirent chassis.
---

# Spirent TestCenter Connection via Lab Server

## Architecture

```
[This machine] --REST--> [Lab Server] --control--> [Spirent Chassis]
                          (stcrestclient)            (physical ports)
```

- **Chassis**: Physical hardware with test ports. Does NOT host the STC REST API.
- **Lab Server**: Hosts the STC REST API (`/stcapi/sessions`). Mediates between scripts and chassis.
- Port locations use format: `//CHASSIS_IP/SLOT/PORT` (e.g., `//100.64.15.236/1/25`).

## Environment

| Component | Address | Notes |
|-----------|---------|-------|
| Lab Server | `il-auto-containers:80` | STC REST API endpoint |
| Python package | `stcrestclient==1.9.4` | `pip install stcrestclient` |

## Before Connecting — Ask the User

Different users have different chassis and port assignments. **Always ask the user** for:

1. **Chassis IP** — which Spirent chassis to connect to (e.g., `100.64.15.236`)
2. **Slot and port(s)** — which port(s) to reserve (e.g., slot 1 port 25)

Use the AskQuestion tool if available, or ask conversationally. Do NOT assume a default chassis or port. Example prompt:

> Which Spirent chassis IP should I connect to, and which slot/port(s) do you want me to use?

Once you have the chassis IP and port(s), you can optionally run the **Chassis Discovery** flow (below) to list all ports and their LLDP peers so the user can pick.

## Connection Pattern

```python
from stcrestclient import stchttp

LABSERVER = 'il-auto-containers'
CHASSIS_IP = '<chassis-ip-from-user>'

stc = stchttp.StcHttp(LABSERVER, port=80)
sid = stc.new_session('dn', 'my_session_name')
stc.join_session(sid)
```

- `new_session(user_name, session_name)` creates a session named `session_name - user_name`.
- Always `join_session(sid)` after creating.
- End with `stc.end_session(sid)` to release resources.

## Port Reservation

Ports on the chassis may be reserved by other STC clients (e.g., Windows STC Application). To take ownership:

```python
project = stc.get('system1', 'children-project')
port1 = stc.create('port', under=project)
stc.config(port1, {'location': f'//{CHASSIS_IP}/{SLOT}/{PORT}'})

# RevokeOwner=true forces reservation even if another client holds the port
stc.perform('AttachPorts', params={'RevokeOwner': 'true'})
stc.apply()
```

**Without `RevokeOwner=true`**, AttachPorts will fail with "Failed to reserve the following ports" if another session owns the port.

### Check port status after attach

```python
online = stc.get(port1, 'Online')       # 'true' / 'false'
```

## Chassis Discovery

Query physical port info (LLDP peers, link status):

```python
stc.perform('ChassisConnect', params={'Hostname': CHASSIS_IP})

pm = stc.get('system1', 'children-physicalchassismanager')
chassis = stc.get(pm, 'children-physicalchassis').split()[0]
modules = stc.get(chassis, 'children-physicaltestmodule')

for mod in modules.split():
    slot = stc.get(mod, 'Index')
    model = stc.get(mod, 'Model')
    pgs = stc.get(mod, 'children-physicalportgroup')
    for pg in pgs.split():
        for p in stc.get(pg, 'children-physicalport').split():
            idx = stc.get(p, 'Index')
            link = stc.get(p, 'LinkStatus')        # 'Up' / 'Down' / 'None'
            peer = stc.get(p, 'PeerSystemName')     # LLDP peer hostname
            peer_port = stc.get(p, 'PeerPortId')   # LLDP peer interface
```

**Note**: `ChassisConnect` param is `Hostname` (not `HostNameList`).

## Traffic Generation

### Streamblock + Header Object Model

A `streamBlock` is created under a `port`. It auto-creates default PDU headers as children. Use the **object-model API** (get/config/create on children) — do NOT set `FrameConfig` XML directly.

**Default children of a new streamBlock:**
- `ethernet:EthernetII` — Ethernet header (srcMac, dstMac)
  - `vlans` — container for 802.1Q tags (always exists, initially empty)
- `ipv4:IPv4` — IPv4 header (sourceAddr, destAddr, ttl)

To add IPv6 instead of IPv4, delete the default IPv4 child and create an IPv6 header:

```python
sb = stc.create('streamBlock', under=port)
ipv4 = stc.get(sb, 'children-ipv4:IPv4').split()[0]
stc.delete(ipv4)
ipv6 = stc.create('ipv6:IPv6', under=sb)
stc.config(ipv6, {'sourceAddr': '2001:db8::1', 'destAddr': '2001:db8::2', 'hopLimit': '64'})
stc.apply()
```

### Creating an IPv4 Stream with VLAN Tag

```python
sb = stc.create('streamBlock', under=port1)
stc.config(sb, {
    'Name': 'my_stream',
    'FixedFrameLength': '128',
    'LoadUnit': 'FRAMES_PER_SECOND',
    'Load': '1000',
})
stc.apply()

# Configure Ethernet header (always the first default child)
eth = stc.get(sb, 'children-ethernet:EthernetII').split()[0]
stc.config(eth, {'srcMac': '00:10:94:01:19:01', 'dstMac': 'e8:c5:7a:d6:30:18'})

# Add VLAN tag: get the pre-existing vlans container, create a Vlan under it
vlans_container = stc.get(eth, 'children-vlans').split()[0]
vlan = stc.create('Vlan', under=vlans_container)
stc.config(vlan, {'id': '100'})  # IMPORTANT: attribute is 'id', NOT 'VlanId'

# Configure the default IPv4 header
ipv4 = stc.get(sb, 'children-ipv4:IPv4').split()[0]
stc.config(ipv4, {'sourceAddr': '10.0.0.1', 'destAddr': '10.0.0.2', 'ttl': '64'})
stc.apply()
```

**Critical VLAN notes:**
- The `vlans` container already exists under `EthernetII` — do NOT create a new `Vlans` object. Get the existing one with `stc.get(eth, 'children-vlans')`.
- Create `Vlan` (singular, capital V) under the `vlans` container.
- The VLAN ID attribute is **`id`** (lowercase). Using `VlanId` or `vlanid` will fail with: `invalid vlan attribute "vlanid": should be Active, AlarmState, Handle, LocalActive, Name, Tags, cfi, id, pri, or type`.
- For QinQ (double-tagged), create two `Vlan` objects under the same `vlans` container.
- The class `vlan:VlanTag` does NOT exist — attempting to create it will fail with `unable to create unknown class`.

### Clearing / Replacing Streams

```python
sbs = stc.get(port1, 'children-streamblock')
if sbs:
    for sb in sbs.split():
        stc.delete(sb)
stc.apply()
```

### Create a generator and send traffic

```python
gen = stc.get(port1, 'children-generator')
gen_cfg = stc.get(gen, 'children-generatorconfig')
stc.config(gen_cfg, {
    'SchedulingMode': 'PORT_BASED',
    'DurationMode': 'BURSTS',
    'BurstSize': '1',
    'Duration': '1000',           # number of packets
    'LoadUnit': 'FRAMES_PER_SECOND',
    'FixedLoad': '100',           # rate in fps
})
stc.apply()

stc.perform('GeneratorStart', params={'GeneratorList': gen})
import time; time.sleep(15)
stc.perform('GeneratorStop', params={'GeneratorList': gen})
```

For continuous traffic (useful for counter-delta measurements):

```python
stc.config(gen_cfg, {
    'SchedulingMode': 'PORT_BASED',
    'DurationMode': 'CONTINUOUS',
    'LoadUnit': 'FRAMES_PER_SECOND',
    'FixedLoad': '1000',
})
stc.apply()
stc.perform('GeneratorStart', params={'GeneratorList': gen})
time.sleep(duration)
stc.perform('GeneratorStop', params={'GeneratorList': gen})
```

### Read TX/RX counters

```python
result_handle = stc.get(port1, 'children-generatorportresults')
total_tx = int(stc.get(result_handle, 'TotalFrameCount') or 0)

analyzer_results = stc.get(port1, 'children-analyzerportresults')
total_rx = int(stc.get(analyzer_results, 'TotalFrameCount') or 0)
```

## Session Management

### List existing sessions

```python
sessions = stc.sessions()  # Returns list like ['Spirent_Eitan - Eitan', ...]
```

### Join an existing session to inspect it

```python
stc.join_session('session_name - user')
project = stc.get('system1', 'children-project')
ports = stc.get(project, 'children-port')
for p in ports.split():
    loc = stc.get(p, 'Location')
    online = stc.get(p, 'Online')
```

### Cleanup

Always end sessions when done to release ports:

```python
stc.end_session(sid)
```

## Identifying Available Ports

After the user provides a chassis IP, run the Chassis Discovery code (above) to enumerate all slots, ports, their link status, and LLDP peers. Present the results as a table so the user can confirm which port(s) to use. Example output:

```
Slot 1: PX-400GMT-T8
  Port 1  (1/1):  link=None  peer=—
  Port 25 (1/25): link=Up    peer=NCP3-nog (ge400-0/0/3)
  Port 33 (1/33): link=Up    peer=David2 (ge400-0/0/3)
```

The chassis web UI is at `http://<CHASSIS_IP>/` (nginx, info only — no REST API for port reservation).

## Common Errors

| Error | Cause | Fix |
|-------|-------|-----|
| `Cannot connect to STC server: IP:80` | Connecting to chassis directly instead of Lab Server | Use `il-auto-containers` as the server, not the chassis IP |
| `Failed to reserve the following ports` | Port owned by another session/client | Add `RevokeOwner: 'true'` to `AttachPorts` params |
| `invalid chassisconnectcommand attribute "hostnamelist"` | Wrong param name | Use `Hostname` (not `HostNameList`) |
| `unable to create unknown command "RevokeOwnershipCommand"` | Not a valid STC command | Use `AttachPorts` with `RevokeOwner` param instead |
| `invalid physicalport attribute "ownershipinfo"` | Attribute doesn't exist on this firmware | Ownership is not exposed; use `RevokeOwner` to force-take |
| `unable to create unknown class "vlan:VlanTag"` | Wrong PDU class name for VLAN | Use `Vlan` (not `vlan:VlanTag`). Create under the existing `vlans` container, not directly under `EthernetII` or `streamBlock` |
| `invalid vlan attribute "vlanid"` or `"VlanId"` | Wrong attribute name for VLAN ID | The attribute is lowercase `id`, not `VlanId` or `vlanid` |
| `DoParseFrameConfig: Error ... Could not find PDU type` | Setting `FrameConfig` XML directly with bad PDU names | Do NOT use XML `FrameConfig`. Use the object-model approach: get/create children of the streamBlock and configure their attributes |
| `Invalid enum value 'POINT_TO_POINT'` on Ospfv2RouterConfig | Wrong NetworkType enum | Use `'P2P'` (not `'POINT_TO_POINT'`) |
| `Unknown class "Ospfv2ExternalLsaBlock"` | Wrong class name for OSPF external LSA | Use `ExternalLsaBlock` (not `Ospfv2ExternalLsaBlock`) |
| `invalid ospfv2routerconfig attribute "RouterId"` | RouterId doesn't exist on OSPF config | Set `RouterId` on the `EmulatedDevice` object instead |
| `invalid emulateddevice attribute "RouterId"` when using kwargs | Passing kwargs incorrectly | Use `**{'RouterId': '1.1.1.1'}` syntax with `stc.create()` |

## Protocol Emulation (BGP, OSPF)

Spirent can emulate routing protocol peers using `EmulatedDevice` objects. This is separate from streamblock traffic — an emulated device handles protocol sessions (ARP, BGP, OSPF) while streamblocks handle raw traffic generation. Both can coexist on the same port.

### EmulatedDevice — Interface Stack

An emulated device needs a network interface stack. For a VLAN-tagged L3 device the stack is: **Ipv4If → VlanIf → EthIIIf** (top to bottom).

```python
device = stc.create('EmulatedDevice', under=project,
                    **{'Name': 'MyDevice',
                       'EnablePingResponse': 'TRUE',
                       'RouterId': '10.100.1.2'})

eth_if = stc.create('EthIIIf', under=device,
                    **{'SourceMac': '00:10:94:01:19:01'})

vlan_if = stc.create('VlanIf', under=device,
                     **{'VlanId': '100'})

ipv4_if = stc.create('Ipv4If', under=device,
                     **{'Address': '10.100.1.2',
                        'Gateway': '10.100.1.1',
                        'PrefixLength': '24'})

# Stack: Ipv4If → VlanIf → EthIIIf
stc.config(ipv4_if, **{'StackedOnEndpoint-targets': vlan_if})
stc.config(vlan_if, **{'StackedOnEndpoint-targets': eth_if})

# Bind device to its top-level and primary interfaces
stc.config(device, **{'TopLevelIf-targets': ipv4_if,
                      'PrimaryIf-targets': ipv4_if})

# Associate device with port
stc.config(port1, **{'AffiliationPort-sources': device})

stc.apply()
```

**Critical associations** (all four are required):
- `StackedOnEndpoint-targets` — builds the interface stack (IP on VLAN on Ethernet)
- `TopLevelIf-targets` — tells the device which interface is the top of the stack
- `PrimaryIf-targets` — tells the device which interface to use for protocol sessions
- `AffiliationPort-sources` — links the device to a physical port

For an **untagged device** (no VLAN), omit the VlanIf and stack Ipv4If directly on EthIIIf.

### Starting and Stopping Devices

```python
# Resolve ARP first, then start device protocols
stc.perform('ArpNdStartCommand', params={'HandleList': port1})
time.sleep(5)
stc.perform('DeviceStartCommand', params={'DeviceList': device})
time.sleep(5)

# Stop devices
stc.perform('DeviceStopCommand', params={'DeviceList': device})
time.sleep(3)
```

### Cleaning Up Devices

```python
devs = stc.get(project, 'children-emulateddevice')
if devs and devs.strip():
    stc.perform('DeviceStopCommand', params={'DeviceList': devs})
    time.sleep(3)
    for d in devs.split():
        stc.delete(d)
    stc.apply()
```

### BGP Emulation

Create a `BgpRouterConfig` under the emulated device to establish a BGP session with the DUT and advertise routes.

```python
# Create BGP router config
bgp = stc.create('BgpRouterConfig', under=device,
                 **{'AsNum': '65002',
                    'DutAsNum': '65001',
                    'IpVersion': 'IPV4',
                    'UseGatewayAsDut': 'TRUE'})

# Bind BGP to the device's IPv4 interface
stc.config(bgp, **{'UsesIf-targets': ipv4_if})

# Advertise a route (e.g., 172.16.1.0/24)
bgp_route = stc.create('BgpIpv4RouteConfig', under=bgp,
                       **{'NextHop': '10.100.1.2',
                          'AsPath': '65002'})

# Configure the network block with prefix details
block = stc.get(bgp_route, 'children-Ipv4NetworkBlock').split()[0]
stc.config(block, {
    'StartIpList': '172.16.1.0',
    'PrefixLength': '24',
    'NetworkCount': '1',       # number of prefixes to advertise
})

stc.apply()
```

**BgpRouterConfig key attributes:**
- `AsNum` — local AS number of the Spirent device
- `DutAsNum` — AS number of the DUT
- `IpVersion` — `IPV4` or `IPV6`
- `UseGatewayAsDut` — set to `TRUE` when the DUT is the gateway (same subnet)

**To advertise multiple prefixes:**
```python
stc.config(block, {
    'StartIpList': '100.0.0.0',
    'PrefixLength': '24',
    'NetworkCount': '1000',    # advertises 100.0.0.0/24 through 100.3.231.0/24
})
```

### OSPF Emulation

Create an `Ospfv2RouterConfig` under the emulated device for OSPFv2 adjacency.

```python
ospf = stc.create('Ospfv2RouterConfig', under=device,
                  **{'AreaId': '0.0.0.0',
                     'NetworkType': 'P2P',
                     'Name': 'OSPF_Router'})

# Bind OSPF to the device's IPv4 interface
stc.config(ospf, **{'UsesIf-targets': ipv4_if})

# Advertise an external route (e.g., 10.20.0.0/24)
ospf_route = stc.create('ExternalLsaBlock', under=ospf)

block = stc.get(ospf_route, 'children-Ipv4NetworkBlock').split()[0]
stc.config(block, {
    'StartIpList': '10.20.0.0',
    'PrefixLength': '24',
    'NetworkCount': '1',
})

stc.apply()
```

**Ospfv2RouterConfig key attributes:**
- `AreaId` — OSPF area (e.g., `0.0.0.0` for backbone)
- `NetworkType` — must be `BROADCAST`, `NATIVE`, or `P2P` (not `POINT_TO_POINT`)
- `HelloInterval`, `RouterDeadInterval`, `RouterPriority`, `IfCost` — optional tuning

**Critical OSPF notes:**
- `RouterId` is NOT a valid attribute on `Ospfv2RouterConfig`. Set it on the `EmulatedDevice` instead.
- External LSA class is `ExternalLsaBlock` (NOT `Ospfv2ExternalLsaBlock`).
- NetworkType enum values: `BROADCAST`, `NATIVE`, `P2P`. Using `POINT_TO_POINT` will fail.

### Combining Protocol Emulation with Traffic Streams

Protocol emulation (EmulatedDevice) and traffic generation (streamBlock) are independent and can run simultaneously on the same port:

1. EmulatedDevice handles ARP resolution and protocol sessions
2. StreamBlocks send raw frames with configured headers
3. The streamBlock source MAC/IP does not need to match the emulated device

Typical workflow for protocol-aware testing:
```python
# 1. Create and start emulated device (BGP/OSPF)
# 2. Wait for protocol convergence (check DUT CLI)
# 3. Create streamBlock with desired source IP
# 4. Send traffic and measure counters
# 5. Stop device, delete streams, cleanup
```

## Stale Session Cleanup

Before creating a new session, clean up any stale sessions with the same name to avoid conflicts:

```python
stc = stchttp.StcHttp(LABSERVER, port=80)
for s in stc.sessions():
    if 'my_session_name' in s:
        try:
            stc.join_session(s)
            stc.end_session(s)
        except Exception:
            pass

sid = stc.new_session('dn', 'my_session_name')
stc.join_session(sid)
```

## Dual-Port Tests

For L3 forwarding tests (traffic routed through a DUT), two ports are needed. Reserve both:

```python
port1 = stc.create('port', under=project)
port2 = stc.create('port', under=project)
stc.config(port1, {'location': f'//{CHASSIS_IP}/{SLOT}/{PORT_A}'})
stc.config(port2, {'location': f'//{CHASSIS_IP}/{SLOT}/{PORT_B}'})
stc.perform('AttachPorts', params={'RevokeOwner': 'true'})
stc.apply()
```

Single-port tests (ping, ARP, BGP peering, uRPF) work with one port.
