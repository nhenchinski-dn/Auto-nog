---
name: bug-report-format
description: >-
  Format bug summaries and Jira bug descriptions using the team template.
  Use when the user asks to file a bug, write a bug report, draft a Jira
  bug description, or summarize a defect.
---

# Bug Report Format

When providing a bug summary, reporting a bug, or drafting a Jira bug description, ALWAYS use this exact template:

**Title:**
`<short, descriptive bug title suitable for a Jira summary field>`

**Issue Summary:**
`<concise description of the defect>`

**Environment Details:**
`<include hostname/IP, software version, platform, topology, and any relevant config>`

**Expected Results:**
`<what should have happened>`

**Actual Results:**
`<what actually happened — include CLI output, logs, or error messages>`

**Steps to Reproduce:**
1. `<step one>`
2. `<step two>`
3. ...

**Workaround:**
`<describe any known workaround, or "None known" if none exists>`

**Tech-support link:**
`<link to tech-support bundle or relevant logs, or "N/A" if not available>`

## Rules

- Never omit any section, even if a section must say "N/A" or "None known".
- Title must be short and descriptive, suitable for a Jira summary field.
- Environment Details must always include hostname or IP address.
- Steps to Reproduce must be numbered and specific enough for someone else to follow.
- Actual Results should include verbatim CLI output or log snippets when available.
- Workaround should describe a concrete workaround if one exists, otherwise state "None known".
- Tech-support link should point to the relevant tech-support bundle or log collection.
