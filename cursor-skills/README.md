# Cursor AI Skills for DNOS QA

Shared Cursor agent skills for DNOS testing workflows.

## Skills Included

| Skill | Description |
|-------|-------------|
| `bug-report-format` | Format bug summaries and Jira bug descriptions using the team template |
| `dnos-acl-reference` | DNOS ACL CLI reference — families, config hierarchy, interface binding, show commands, TCAM verification |
| `dnos-bulk-interface-config` | Bulk interface configuration patterns (admin-state, LLDP) |
| `dnos-deploy-upgrade` | Deploy or upgrade a DNOS device from a Jenkins build (fresh deploy + upgrade workflows) |
| `dnos-multicast-testing` | DNOS CLI syntax, PIM/multicast testing conventions, Q3D platform behaviors |
| `dnos-snmp-testing` | SNMP testing patterns and workflows on DNOS devices |
| `dnos-ssh-connection` | SSH into DNOS devices and run CLI commands via paramiko |
| `execute-testing-task` | Execute a Jira testing task on a DNOS device and record results |
| `jira-project-context` | Jira custom fields, platform mapping, JQL patterns, status values |
| `jira-testing-task-populate` | Populate Jira Testing Task Test Steps |

## Installation

Copy all skill folders into your Cursor skills directory:

```bash
# Clone the repo (if you haven't already)
git clone git@github.com:nhenchinski-dn/Auto-nog.git

# Copy skills into your Cursor config
cp -r Auto-nog/cursor-skills/*/ ~/.cursor/skills/
```

After copying, restart Cursor (or open a new chat) and the skills will be available to the agent automatically.

## Updating

If skills are updated in this repo, pull and re-copy:

```bash
cd Auto-nog && git pull
cp -r cursor-skills/*/ ~/.cursor/skills/
```
