---
name: dnos-acl-reference
description: >-
  DNOS ACL CLI reference — families, config hierarchy, interface binding,
  global ACLs, show commands, and TCAM verification. Use when working with
  access-lists, ACLs, KBP/TCAM resources, or packet filtering on DNOS.
---

# DNOS Access-Lists (ACL) Reference

## ACL Families

| Family | Config keyword | KBP/TCAM slot | Key size |
|--------|---------------|---------------|----------|
| IPv4 | `ipv4` | KBP-1 (ingress/global), L4-L6 (egress) | 320 / 160 bits |
| IPv6 | `ipv6` | KBP-2 (ingress/global), L8-L9 (egress) | 480 / 320 bits |
| Ethernet | `eth` | KBP-5 | 160 bits |
| Control-plane IPv4 | (CP-ACLv4) | KBP-3 | 320 bits |
| Control-plane IPv6 | (CP-ACLv6) | KBP-4 | 480 bits |

TCAM "Used by" labels: `I-ACLv4`, `I-ACLv6` (ingress), `G-ACLv4`, `G-ACLv6` (global), `E-ACLv4`, `E-ACLv6` (egress), `CP-ACLv4`, `CP-ACLv6` (control-plane), `I-ACLeth` (ingress ethernet).

## Creating an ACL (configure mode)

```
configure
access-lists ipv4 <NAME> rule <INDEX> allow|deny [protocol <proto>] [packet-length <range>]
access-lists ipv6 <NAME> rule <INDEX> allow|deny
access-lists eth <NAME> rule <INDEX> allow|deny [ether-type <value>]
commit
end
```

- Rule index: lower = higher priority. Use `65000` for a catch-all allow.
- Each ACL gets implicit `default-icmp allow` + `default deny` appended.

## Binding ACL to an Interface (ingress)

Inside `configure` mode — use `direction in`:

```
interfaces <INTF> access-list ipv4|ipv6|eth <NAME> direction in
```

Examples with sub-interfaces:

```
interfaces ge400-0/0/4 access-list ipv6 MY_V6_ACL direction in
interfaces ge10-0/0/32.100 access-list eth DROP_CFM direction in
```

## Global ACL (applied system-wide, all ingress interfaces)

Global ACLs live under `forwarding-options` — NOT under `access-lists`:

```
configure
forwarding-options
  global-access-list in ipv4|ipv6 <NAME>
top
commit
end
```

Remove with:

```
configure
no forwarding-options global-access-list in ipv4|ipv6 <NAME>
commit
end
```

## Removing / Cleanup

```
configure
no interfaces <INTF> access-list ipv4|ipv6|eth <NAME>
top
no access-lists ipv4|ipv6|eth <NAME>
commit
end
```

## Show Commands

| Command | Purpose |
|---------|---------|
| `show access-lists \| no-more` | All ACLs (ipv4 + ipv6 + eth) |
| `show access-lists ipv4\|ipv6\|eth [NAME] \| no-more` | Filter by family or name |
| `show access-lists counters \| no-more` | Hit counters per rule |
| `show access-lists counters <INTF> \| no-more` | Per-interface counters |
| `show access-lists global-acl \| no-more` | Show global ACL bindings |
| `show access-lists management \| no-more` | Management interface ACLs |
| `show config access-lists \| no-more` | Running config for ACLs |
| `show config \| include global-access-list \| no-more` | Verify global ACL binding in config |

## TCAM / KBP Hardware Resource Verification

```
show system npu-resources | no-more
```

Look at the **Access-Lists TCAM utilization** section. Key columns:

- **ACL TCAM**: KBP-1 through KBP-5, L4-L9
- **ACL HW entries [installed/total]**: hardware usage vs capacity
- **Used by**: `I-ACLv6: N, G-ACLv6: M` — must NEVER be negative

## Paramiko Script Pattern

```python
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(host, username='dnroot', password='dnroot', timeout=15,
            look_for_keys=False, allow_agent=False)
chan = ssh.invoke_shell(width=400, height=1000)
```

DNOS CLI requires an interactive shell (invoke_shell), not exec_command.
