---
name: jira-project-context
description: >-
  Jira project context — custom fields, platform mapping, JQL patterns,
  and status values for SW (DNOS) and ART (Artemis) projects. Use when
  working with Jira issues, writing JQL queries, or mapping platforms.
---

# Jira Project Context

## MCP Server

Use `plugin-atlassian-atlassian` MCP server (tool names: `searchJiraIssuesUsingJql`, `getJiraIssue`, `editJiraIssue`). Some setups expose these as `atlassian_jira_search`, `atlassian_jira_get_issue`, `atlassian_jira_update_issue`.

## Projects

| Key | Name | Notes |
|-----|------|-------|
| **SW** | DNOS | Main product — Testing Tasks, Test Categories, Epics |
| **ART** | Artemis | AT&T certification — optical, platform testing |

## Issue Types

Testing Task, Test Category, Epic, Story, Bug

## Custom Fields

| Field ID | Name | Notes |
|----------|------|-------|
| `customfield_11772` | Test Steps | Full test body on SW project |
| `customfield_15029` | Version | e.g., v25.4 |
| `customfield_12001` | Customer | e.g., AT&T - Artemis |
| `customfield_12006` | Test environment | e.g., Lab / Certification |
| `customfield_11767` | Test type | e.g., Manual Regression |

## Platform Mapping

| Abbreviation | Platform | Hardware |
|---|---|---|
| AGG | Aggregation (Q3D) | Q3D |
| PLEAF | Leaf | CL192 (v18.2.8) |
| ME10 (SE10/VE10) | Metro Edge 10G | NCP9 (Q2C) |
| ME100 (SE100) | Metro Edge 100G | NCP6-S (Q2C+) |
| MSE (ADI) | Multi-Service Edge | NCP3-SA |
| ASE | Access Service Edge | L2 VPN (EVPN VPWS) |

## JQL Patterns

```
# Find testing tasks by optic type and platform
project = ART AND summary ~ '400GBase-LR4' AND summary ~ 'ME100'

# Batch lookup by key
key in (ART-8025, ART-8026, ART-8027)

# Find all testing tasks under parent test category
parent = ART-25

# Filter by status
project = ART AND status = 'Test Passed' AND labels = Optical

# Filter by sprint label
project = ART AND labels = 'Cert_MS1_Sprint2' AND labels = Optical
```

## Status Values

- **Test Passed** (Done/green) — Testing completed successfully
- **Open** (To Do/blue-gray) — Not yet started
- **In Progress** (yellow) — Currently being tested
- **Blocked** (yellow) — Cannot proceed, dependency issue
