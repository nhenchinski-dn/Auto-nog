---
name: dnos-bulk-interface-config
description: >-
  DNOS bulk interface configuration patterns (admin-state, LLDP).
  Use when enabling or configuring many interfaces at once on a DNOS device,
  or when enabling LLDP globally across all interfaces.
---

# DNOS Bulk Interface Configuration

## Admin Enable All Interfaces

Use the network-mapper tools to discover interfaces first, then generate configuration:

```
configure
interfaces
  ge10-0/0/0
    admin-state enabled
  !
  ge10-0/0/1
    admin-state enabled
  !
  ge100-0/0/96
    admin-state enabled
  !
!
commit
end
```

**Key Points:**
- Use `discover_device()` to get interface list
- Use `get_device_interfaces_detail()` to see all interfaces
- Each interface needs its own block with `admin-state enabled`
- Close each interface block with `!` at content indentation level
- Close interfaces section with `!`
- Always `commit` to apply changes

## Enable LLDP on All Interfaces

LLDP configuration is under protocols hierarchy:

```
configure
protocols
  lldp
    admin-state enabled
    interface ge10-0/0/0
    !
    interface ge10-0/0/1
    !
    interface ge100-0/0/96
    !
  !
!
commit
end
```

**Key Points:**
- Enable global LLDP with `admin-state enabled`
- Add each interface under `protocols lldp`
- By default, both transmit and receive are enabled
- Simply declaring the interface enables LLDP on it

## Verification Commands

- `show interfaces` — verify interface admin/operational state
- `show lldp neighbors` — see discovered LLDP neighbors
- `show lldp interfaces` — verify LLDP status per interface

## Configuration Format Rules

- 2 spaces per indentation level (NOT tabs)
- Close every block with `!` at the content indentation level
- Use `get_device_config()` to see proper hierarchy format
