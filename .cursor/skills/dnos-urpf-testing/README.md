# dnos-urpf-testing

Cursor Agent skill for testing **uRPF (Unicast Reverse Path Forwarding)** on
DNOS devices. Built from hands-on execution of the SW-243699 "Strict uRPF |
Basic Functionality" epic and its child testing tasks.

## What's Inside

`SKILL.md` gives the agent everything it needs to write and execute uRPF tests:

| Section | What It Covers |
|---------|----------------|
| **CLI Reference** | Full config hierarchy from RST docs — global mode, per-AFI overrides, allow-default, VRF scope, removal |
| **Show Commands** | `show config`, `show interfaces detail`, `show interfaces counters` with exact counter labels |
| **Counter Verification** | Python before/after delta pattern for `RX packets:` and `uRPF Ipv4 drops:` |
| **Spirent Patterns** | VLAN-tagged stream creation, generator start/stop, BGP emulation, OSPF emulation |
| **Test Matrix** | 13-step scenario table covering static, connected, BGP, OSPF, IS-IS, aggregate, Null0, and no-route cases |
| **Jira Context** | Parent epic SW-243699, all sibling tasks and their statuses |
| **Gotchas** | 12 hard-won lessons (DNOS CLI, Spirent quirks, uRPF behavioral edge cases) |
| **Reference Scripts** | Pointers to existing test scripts by scenario |

## When This Skill Activates

The agent loads this skill when working with:

- uRPF strict or loose mode configuration
- allow-default route behavior
- Per-AFI (IPv4/IPv6) uRPF settings
- uRPF drop counter verification
- Any testing task under the SW-243699 epic

## Key Concepts

**Strict mode**: Source IP must have a FIB entry that resolves via the *same
interface* the packet arrived on. Otherwise the packet is dropped.

**Loose mode**: Any FIB match (except Null0) is enough — ingress interface
doesn't matter.

**Null0 always drops**: In both modes, a route pointing to discard/Null0 fails
the uRPF check, even with `allow-default enabled`.

## Related Skills

- `dnos-ssh-connection` — SSH to DNOS devices via paramiko
- `spirent-stc-connection` — Spirent TestCenter REST API patterns
- `dnos-acl-reference` — ACL config (sometimes tested alongside uRPF)
- `execute-testing-task` — Running Jira testing tasks and posting results
