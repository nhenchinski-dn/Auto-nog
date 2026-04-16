---
name: jira-testing-task-populate
description: >-
  Populate or draft Jira Testing Task Test Steps in this user's style.
  Use when the user asks to populate a testing task, draft Test Steps,
  fill customfield_11772 / Test Steps field, or write a Jira testing
  task body before execution.
---

# Jira Testing Task — populate / draft (not execution)

## When to use

- **Use this skill** when the user wants **Test Steps / task body drafted or populated** (planning/writing), including pasting into Jira or preparing an update payload.
- **Do not** substitute this for **running tests on a device** or appending live execution output — that is a separate workflow (execute on hardware, then fill *Test Results*).

## Jira MCP (tool names)

On **plugin-atlassian-atlassian** (or equivalent Jira MCP), use:

| Purpose | Tool |
|--------|------|
| Resolve site | `getAccessibleAtlassianResources` → `cloudId` |
| JQL search | `searchJiraIssuesUsingJql` (paginate with `nextPageToken` until `isLast`) |
| Issue detail | `getJiraIssue` |
| Update issue | `editJiraIssue` |

Some setups expose the same operations as `atlassian_jira_search`, `atlassian_jira_get_issue`, `atlassian_jira_update_issue` — use whichever names exist; behavior matches the table above.

**Fallback:** If Jira tools fail or are unavailable, state that clearly and draft only from issue text the user pastes.

## Mandatory pre-steps (before drafting)

Do these in order; skip only what is impossible without credentials.

1. **Target issue** — `getJiraIssue` on the Testing Task key (or the one the user named). Request at least: `summary`, `description`, `parent`, `subtasks`, `issuelinks`, `status`, **`customfield_11772`** (Test Steps on **SW**).
2. **Parent Test Category** — if `parent` exists, `getJiraIssue` on the parent for summary/description and **sibling scope** (e.g. other Testing Tasks under the same category via JQL `parent = <PARENT-KEY>` if needed).
3. **Epic (if applicable)** — if the Test Category has a `parent` of type **Epic**, fetch that Epic. Run a **small** JQL sample (e.g. `"Epic Link" = <EPIC-KEY>` or `parent = <EPIC-KEY>`, `maxResults` ≤ 10–15) to see how stories/bugs align with the test — **do not** dump the full backlog.
4. **Issue links** — read `issuelinks` on the target task for blocks/relates-to bugs or stories that should shape Pass Criteria, Variants, or Negative flows.
5. **Then draft** the Test Steps body (and short Description if needed).

## Field mapping (SW / DNOS)

- **Full structured body** → **Test Steps** field.
- On project **SW**, Test Steps is **`customfield_11772`** (verify in `getJiraIssue` / metadata if another project uses a different id; if so, note the id when updating).
- **Description** stays **short**: optional one-line objective, a CLI tree in `{noformat}` or fenced block when the task is hierarchy-focused, or numbered **conditions** for “valid/invalid” style — avoid duplicating the whole Test Steps.

## DNOS CLI

- **Never guess** Cisco/JunOS-style paths.
- Verify command hierarchy against RST under repo path: `cheetah/prod/dnos_monolith/dnos_cli/`.

## User’s writing conventions (from their SW Testing Tasks)

Style references (keys only): **SW-236664**, **SW-244073**, **SW-238848**, **SW-235927**.

### Summary / title

- Often **`Feature \| Area \| Specific`** with pipes (e.g. Ethernet OAM Y.1731 branches).
- Sometimes a **single descriptive line** with scope in parentheses (e.g. uRPF “per-interface + per-AFI”).
- Align wording with the **parent Test Category** title when the task sits under a category.

### Description

- Frequently **empty** when detail lives in Test Steps.
- Otherwise: **CLI tree** (tree or indented hierarchy) or **numbered conditions** (see SW-238848-style valid/invalid narratives).

### Test Steps field — section headers and order

Use these **exact bold header lines** (Jira wiki: `*Header:*`). Default block order:

1. `*Test Steps:*`
2. `*Pass Criteria:*`
3. `*Commands legend:*`
4. `*Show commands legend:*`
5. `*Variants:*`
6. `*Negative Testing Flows:*`
7. `*Test Results:*` — leave **empty** or placeholders for pre-execution drafts; execution fills with `{noformat}` CLI/show output.

**Variation:** In some tasks, **`*Commands legend:`** appears **after** *Negative Testing Flows* (still before *Test Results*). Use whichever ordering matches sibling tasks under the same Test Category.

### Lists, tone, markers

- **Test Steps:** Jira **ordered** list: lines start with `#` at column 0; sub-bullets with `#*`.
- Short, **action-oriented** steps; avoid per-step “Command:” / “Result:” labels — commands go in legends.
- Completed steps in executed tickets use a **check** (e.g. `(/)` or ✅ in the editor); when **drafting**, omit checks unless the user asks to mirror a completed task.
- **Pass Criteria**, **Variants**, **Negative Testing Flows:** bullet lists with leading `*` (Jira wiki bullets).
- **Placeholders:** angle brackets, e.g. `<IF>`, `<MD-NAME>`, `<SESSION>`.

### Commands / show output

- Prefer **`{noformat}...{noformat}`** for paste-safe CLI in legends (use `{code}` only if the user requests it).
- **Show commands** include `| no-more` where relevant for DNOS.

### *Test Results* (when filling after a run)

- Short **labels** (plain lines) then **verbatim** config diff / CLI / tables in `{noformat}` blocks (e.g. “using a mep-id:”, “mac-address:”, “negative:”).

## Pushing updates to Jira

- **Only** if the user explicitly asks to update Jira — workspace policy requires **explicit approval** before `editJiraIssue` / any write.
- Set the Test Steps field on the correct custom field id (`customfield_11772` on SW). Use the MCP’s supported format (`contentFormat`: `markdown` vs ADF) per tool/schema.

## Refreshing style from Jira (optional)

To re-calibrate against this user’s latest tasks, JQL (try in order until enough examples):

1. `reporter = currentUser() AND issuetype = "Testing Task" ORDER BY updated DESC`
2. `creator = currentUser() AND issuetype = "Testing Task" ORDER BY updated DESC`
3. `assignee = currentUser() AND issuetype = "Testing Task" ORDER BY updated DESC`

Pick **8–15** representative keys; deep-fetch **`customfield_11772`** on the richest ones.

## More layout examples

See [examples.md](examples.md) for short anonymized structural snippets.
