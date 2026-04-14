# AI-Assisted Test Automation Workflow with Cursor

> How I use Cursor IDE + AI to plan, build, execute, and report on DNOS test automation — from Jira ticket to verified result.

---

## Overview

This document describes the AI-powered workflow I've built around Cursor IDE for the **Auto-nog** project (DNOS test automation for DriveNets). The setup connects the AI to live systems — Jira, Jenkins, lab devices, Slack, Confluence — so it acts as a hands-on testing partner rather than just a code assistant.

**By the numbers:**

| Metric | Count |
|--------|-------|
| Cursor Rules | 3 (always-on guardrails + workflow definitions) |
| MCP Integrations | 5 (Jira, Confluence, Slack, GitHub, Network Mapper) |
| Structured Plans | 21 (AI-generated, collaboratively refined) |
| Test Scripts | 52+ (Y.1731, QoS, BFD, multicast, RESTCONF) |
| Documentation Files | 44 (runbooks, guides, fix summaries) |
| AI Conversation Sessions | 51+ |

---

## 1. Cursor Rules — Persistent AI Instructions

Rules live in `.cursor/rules/` and are loaded into every AI conversation automatically. They teach the AI the project context so I never have to re-explain things.

### `jira-mcp.mdc` — Project Knowledge Base

Teaches the AI everything about our Jira structure:

- **Project:** ART (Artemis)
- **Issue types:** Testing Task, Test Category, Epic, Story, Bug
- **Custom fields:** Test Steps, Version, Customer, Test Environment, Test Type
- **Platform mappings:** AGG → Q3D, PLEAF → CL192, ME10 → NCP9, ME100 → NCP6-S, MSE → NCP3-SA, ASE → L2 VPN
- **JQL patterns:** Ready-made queries for optic types, sprint labels, status filters
- **Status values:** Test Passed, Open, In Progress, Blocked

The AI can search and navigate Jira tickets using this context without any prompting.

### `jira-approval.mdc` — Safety Guardrail

Prevents the AI from making any Jira changes without explicit permission:

- Must present bug summary, severity, and repro steps before filing
- No batch-filing bugs — each one needs individual approval
- No updating fields, adding comments, linking issues, or changing assignees without asking first

### `execute-testing-task.mdc` — End-to-End Test Execution

A full workflow the AI follows when I point it at a Jira testing task:

1. **Fetch** the ticket and parse test steps from the description
2. **Resolve** the target device (from the ticket or by asking)
3. **Execute** CLI commands over SSH using `run_show_command` / `device_shell_execute`
4. **Capture** output and compare against expected results
5. **Build** a Test Results section (PASS/FAIL with CLI output and analysis)
6. **Update** the Jira ticket with results (after approval)

---

## 2. MCP Servers — Live System Integrations

The AI connects to external systems through MCP (Model Context Protocol), giving it direct access to the tools I use daily.

| Server | Capabilities |
|--------|-------------|
| **dn-mcp-server** | Jira (query/create/update), Confluence (read/write), Slack, GitHub, Broadcom RAG for hardware docs |
| **network-mapper** | Lab topology discovery — the AI can find and resolve devices |
| **Atlassian** | Direct Atlassian cloud API integration |
| **cursor-ide-browser** | Browser automation for web UI testing and verification |
| **Slack** | Read channels, post messages, search conversations |

**Example:** I can say *"check what's happening with SW-248225"* and the AI will query Jira, read the latest comments, check the fix version, and summarize the status — all without me opening a browser.

---

## 3. Plans — Collaborative Design Before Coding

Before writing complex tests, the AI and I create structured plans in `.cursor/plans/`. These have YAML frontmatter with tracked TODOs and detailed markdown bodies.

### Notable Plans

| Plan | What It Covers |
|------|---------------|
| **Y.1731 Epic QA Review** | Full QA review of Epic SW-141523 — Mermaid architecture diagrams, P0/P1/P2 test matrices, bug candidates, automation suggestions |
| **CLI Test Gap Coverage** | 8 identified gaps in `y1731_cli_tab_test.py` — TAB completion, PCP boundary, dependency deletion, show commands, etc. All completed. |
| **Smart Bug Monitor** | Design for `bug_monitor.py` — per-bug Jenkins branch mapping, dynamic branch detection from Jira comments/fixVersions |
| **RESTCONF Test Automation** | Multi-phase plan: mount → GET → PATCH (DM/SLM) → modify → DELETE → negative tests → cleanup |
| **BFD over BGP Test** | Two-device setup via LLDP, following existing `qos_sanity_test.py` patterns |

---

## 4. Test Scripts & Automation

### Bug Verification Scripts (Top-Level)

| Script | Bug | What It Tests |
|--------|-----|--------------|
| `verify_sw248225.py` | SW-248225 | PM profile allows multiple test-duration types (probes vs time-frame) |
| `verify_sw245106.py` | SW-245106 | PIM use-after-free with 60K+ SSM routes and rapid RPF flapping |
| `qa_bugs.py` | Multiple | 8-test suite for Y.1731 Proactive PM bugs |
| `apply_acl.py` | — | Drop CFM frames via ACL to test near-end loss behavior |

### Y.1731 Test Suite (`tests/y1731/`)

52 test scripts covering:

- **CLI Testing:** `y1731_cli_tab_test.py` (~3,700 lines) — comprehensive CLI and TAB completion validation for DM/SLM
- **RESTCONF Testing:** `y1731_restconf_test.py` — ODL RESTCONF API testing with XML body discovery
- **Bug-Specific Tests:** SW-236664 (ETH-DM delete), SW-236665 (SLM), SW-236668 (timing), SW-236991 (scale), SW-237053 (system events), SW-237984 (on-demand stop), SW-238848 (valid/invalid), SW-241365 (SLM PCP/QoS)
- **Feature Tests:** CFM discovery, SLM events, on-demand stop, multi-MEP support

### Testing Patterns Used

- **SSH:** paramiko and pexpect for device access
- **CLI Parsing:** ANSI stripping, prompt detection, `-- More --` handling
- **Config Flow:** configure → commands → commit → verify → exit
- **Output:** Structured JSON results + CLI capture logs
- **Arguments:** `--host`, `--md`, `--ma`, `--mep-id` for flexible targeting

### Bug Monitor (`bug_monitor.py`)

An autonomous script that:

1. Polls Jira every 5 minutes for a list of tracked bugs
2. Detects when a bug moves to Done/Closed/Resolved
3. Infers the correct Jenkins branch from comments and fixVersions
4. Triggers a Jenkins build on that branch automatically
5. Tracks state in `.bug_monitor_state.json` and logs to `bug_monitor.log`

---

## 5. Documentation (`docs/`)

44 markdown files co-authored with the AI:

- **Complete Guides:** `Y1731_COMPLETE_GUIDE.md` — comprehensive feature reference
- **Manual Runbooks:** `SW-236664_ETH-DM_manual_runbook.md` — step-by-step procedures
- **Quick Starts:** `y1731_quick_start.md` — getting started fast
- **Fix Summaries:** Per-bug documentation of what was fixed and how to verify
- **QoS Summaries:** Timestamped test results in `output/`

---

## 6. Output & Results (`output/`)

55 files of test artifacts:

- **Structured Results:** `sw237053_system_event_results.json`, `sw241365_slm_pcp_results.json`
- **CLI Captures:** `cli2_results.log` through `cli6_results.log`
- **Test Logs:** `test_run_cfm26_restconf_MAIN.log`, session logs from concurrent SSH tests
- **QoS Summaries:** `qos_test_summary_*.md` with timestamps

---

## 7. The Workflow Cycle

```
┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐
│  PLAN    │───▶│  BUILD   │───▶│ EXECUTE  │───▶│  REPORT  │───▶│ MONITOR  │
│          │    │          │    │          │    │          │    │          │
│ Read Jira│    │ Write    │    │ Run on   │    │ PASS/FAIL│    │ Watch    │
│ ticket,  │    │ test     │    │ live     │    │ results  │    │ bugs,    │
│ explore  │    │ script   │    │ devices  │    │ back to  │    │ trigger  │
│ feature, │    │ with SSH │    │ via MCP  │    │ Jira     │    │ Jenkins  │
│ create   │    │ + CLI    │    │          │    │          │    │ on fix   │
│ plan     │    │ parsing  │    │          │    │          │    │          │
└──────────┘    └──────────┘    └──────────┘    └──────────┘    └──────────┘
```

1. **Plan** — AI reads the Jira ticket, explores the feature area, generates a structured plan with tracked TODOs
2. **Build** — AI writes the test script (SSH/pexpect/paramiko, CLI parsing, RESTCONF) following established patterns
3. **Execute** — AI runs the test on live lab devices via MCP network-mapper, captures all output
4. **Report** — AI builds PASS/FAIL results with CLI evidence and updates the Jira ticket (with approval)
5. **Monitor** — `bug_monitor.py` watches for bug fixes and auto-triggers Jenkins verification builds

---

## 8. Repository Structure

```
Auto-nog/
├── tests/
│   └── y1731/          # 52 Y.1731/CFM test scripts
├── setup/              # 15 setup/provisioning scripts
├── diag/               # 28 diagnostic scripts
├── lib/                # 3 shared library modules
├── docs/               # 44 markdown docs (guides, runbooks, summaries)
├── output/             # 55 test results (JSON, logs, summaries)
├── scripts/            # Shell scripts (reproduce bugs, run commands)
├── .cursor/
│   ├── rules/          # 3 AI rules (Jira, approval, test execution)
│   ├── plans/          # 21 structured plans
│   └── mcp.json        # MCP server configurations
├── bug_monitor.py      # Autonomous Jira→Jenkins monitor
├── qa_bugs.py          # Y.1731 bug verification suite
├── verify_sw*.py       # Bug-specific verification scripts
└── README.md           # Project overview
```
