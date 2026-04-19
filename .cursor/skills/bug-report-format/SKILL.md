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
`<link to tech-support bundle on MinIO>`

**Git Commit:**
`<contents of /.gitcommit from the device — identifies the exact build>`

## Git Commit Collection

Before finalizing the bug report, **always collect the `.gitcommit` value** from
the DNOS device where the bug was observed. This identifies the exact build the
defect was reproduced on and must be included in every bug report.

To collect it, SSH into the device (or use an existing SSH session) and run:

1. `start shell` — drop into the underlying Linux shell.
2. When prompted for a password, enter: `dnroot`
3. `cat /.gitcommit` — print the build commit hash.
4. Copy the output verbatim into the **Git Commit** field of the bug report.

If you are running commands on the device programmatically (e.g. via the
`dnos-ssh-connection` skill), automate the same sequence and capture the
`cat /.gitcommit` output. Never leave this field as "N/A" without first
attempting to collect it.

## Tech-Support Collection

Before finalizing the bug report, **always offer to collect a tech-support** from
the device where the bug was observed. If the user agrees:

1. Follow the `dnos-techsupport` skill workflow — ask for a tech-support name
   (suggest using the Jira ticket key or a short bug identifier, e.g. `sw123456_acl_drop`).
2. Use the same device from the Environment Details section.
3. Once the tech-support is uploaded to MinIO, fill in the **Tech-support link**
   field with the MinIO URL (e.g. `http://minioio.dev.drivenets.net:9000/minio/techsupport/ts_<name>_<timestamp>.tar`).

If the user declines or already has a tech-support link, use what they provide.
Never leave this field as "N/A" without first offering to collect one.

## Rules

- Never omit any section, even if a section must say "N/A" or "None known".
- Title must be short and descriptive, suitable for a Jira summary field.
- Environment Details must always include hostname or IP address.
- Steps to Reproduce must be numbered and specific enough for someone else to follow.
- Actual Results should include verbatim CLI output or log snippets when available.
- Workaround should describe a concrete workaround if one exists, otherwise state "None known".
- Tech-support link: always offer to collect a tech-support using the `dnos-techsupport` skill before leaving this field empty.
- Git Commit: always collect `/.gitcommit` from the device (`start shell` → password `dnroot` → `cat /.gitcommit`) and include the output before submitting the bug.
