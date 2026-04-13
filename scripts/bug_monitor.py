#!/usr/bin/env python3
"""
Unified bug monitor: polls Jira for bugs you reported, and when a bug
transitions to Closed/Done/Resolved:
  1. Triggers a Jenkins build on the detected branch
  2. Extracts verification steps from the ticket (description, Test Steps
     custom field, developer comments)
  3. Sends a Slack notification with the bug details, Jenkins build link,
     and verification steps so QA has everything ready

Required env vars:
    JIRA_EMAIL        - Jira email
    JIRA_TOKEN        - Jira API token
    JENKINS_USER      - Jenkins username
    JENKINS_TOKEN     - Jenkins API token
    SLACK_WEBHOOK_URL - Slack incoming webhook URL

Optional env vars:
    POLL_INTERVAL     - Seconds between polls (default: 300)
    JQL_FILTER        - Override the default JQL query
"""

import os
import re
import sys
import json
import time
import logging
import requests
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, quote as urlquote

# ── Config ────────────────────────────────────────────────────────────────────

Y1731_EPIC = "SW-141523"
Y1731_FEATURE_BRANCH = (
    "feature%2Fv26.2%2FSW-141523-y1731-proactive-initiator-performance-monitoring"
)

FIX_VERSION_TO_BRANCH = {
    "v26.1": "dev_v26_1",
    "v26.2": "dev_v26_2",
    "v25.4": "dev_v25_4",
    "v25.4.1": "dev_v25_4_1",
}

CLOSED_STATUSES = {
    "Done", "Closed", "Resolved", "Cancelled", "Won't Fix", "Duplicate",
}
ACTIONABLE_CLOSED = {"Done", "Closed", "Resolved"}

JIRA_BASE = "https://drivenets.atlassian.net"
JENKINS_BASE = "https://jenkins.dev.drivenets.net"

TEST_STEPS_FIELD = "customfield_11772"

STATE_FILE = Path.home() / ".bug_monitor_state.json"
LOG_FILE = Path.home() / "bug_monitor.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("bug_monitor")

# ── Credentials / config ─────────────────────────────────────────────────────


def get_config():
    required = {
        "JIRA_EMAIL": os.environ.get("JIRA_EMAIL"),
        "JIRA_TOKEN": os.environ.get("JIRA_TOKEN"),
        "JENKINS_USER": os.environ.get("JENKINS_USER"),
        "JENKINS_TOKEN": os.environ.get("JENKINS_TOKEN"),
        "SLACK_WEBHOOK_URL": os.environ.get("SLACK_WEBHOOK_URL"),
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        log.error(f"Missing environment variables: {', '.join(missing)}")
        sys.exit(1)

    return {
        **{k: v for k, v in required.items()},
        "poll_interval": int(os.environ.get("POLL_INTERVAL", "300")),
        "jql_filter": os.environ.get("JQL_FILTER"),
    }

# ── State persistence ─────────────────────────────────────────────────────────


def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ── Jira: dynamic bug discovery ──────────────────────────────────────────────


def fetch_my_bugs(jira_email, jira_token, jql_override=None):
    jql = jql_override or (
        "reporter = currentUser() AND issuetype = Bug "
        "AND status not in (Verified) "
        "AND created >= -180d "
        "ORDER BY updated DESC"
    )
    issues = []
    next_token = None

    while True:
        try:
            body = {
                "jql": jql,
                "fields": [
                    "status", "summary", "assignee", "priority", "fixVersions",
                ],
                "maxResults": 50,
            }
            if next_token:
                body["nextPageToken"] = next_token

            resp = requests.post(
                f"{JIRA_BASE}/rest/api/3/search/jql",
                auth=(jira_email, jira_token),
                json=body,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            for issue in data.get("issues", []):
                fields = issue["fields"]
                assignee = "Unassigned"
                if fields.get("assignee"):
                    assignee = fields["assignee"].get("displayName", "Unassigned")
                priority = "None"
                if fields.get("priority"):
                    priority = fields["priority"].get("name", "None")
                fix_versions = [
                    fv.get("name", "") for fv in (fields.get("fixVersions") or [])
                ]
                issues.append({
                    "key": issue["key"],
                    "summary": fields.get("summary", "N/A"),
                    "status": fields["status"]["name"],
                    "assignee": assignee,
                    "priority": priority,
                    "fix_versions": fix_versions,
                })
            if data.get("isLast", True):
                break
            next_token = data.get("nextPageToken")
        except Exception as e:
            log.warning(f"Failed to fetch bugs: {e}")
            break

    return issues

# ── Jira: branch detection ───────────────────────────────────────────────────


def detect_branch_from_jira(bug_key, jira_email, jira_token):
    """
    Detect the Jenkins branch by inspecting:
      1. Jira comments for Jenkins build URLs
      2. fixVersions field
      3. Parent epic links (Y.1731 feature branch)
    Returns the branch string or None.
    """
    url = (
        f"{JIRA_BASE}/rest/api/2/issue/{bug_key}"
        f"?fields=fixVersions,comment,issuelinks,parent"
    )
    try:
        resp = requests.get(url, auth=(jira_email, jira_token), timeout=30)
        resp.raise_for_status()
        fields = resp.json()["fields"]

        jenkins_pattern = re.compile(
            r"jenkins\.dev\.drivenets\.net/job/drivenets/job/cheetah/job/([^/]+)/"
        )
        for comment in reversed(fields.get("comment", {}).get("comments", [])):
            body = comment.get("body", "")
            match = jenkins_pattern.search(body)
            if match:
                branch = match.group(1)
                log.info(f"  Detected branch from comment: {unquote(branch)}")
                return branch

        for fv in fields.get("fixVersions") or []:
            version_name = fv.get("name", "")
            if version_name in FIX_VERSION_TO_BRANCH:
                branch = FIX_VERSION_TO_BRANCH[version_name]
                log.info(f"  Detected branch from fixVersion {version_name}: {branch}")
                return branch

        for link in fields.get("issuelinks") or []:
            for direction in ("outwardIssue", "inwardIssue"):
                if link.get(direction, {}).get("key") == Y1731_EPIC:
                    log.info(f"  Linked to Y.1731 epic, using feature branch")
                    return Y1731_FEATURE_BRANCH

        parent = fields.get("parent") or {}
        if parent.get("key") == Y1731_EPIC:
            log.info(f"  Child of Y.1731 epic, using feature branch")
            return Y1731_FEATURE_BRANCH

    except Exception as e:
        log.warning(f"  Branch detection failed for {bug_key}: {e}")

    return None

# ── Jira: verification steps extraction ──────────────────────────────────────


def _parse_description_sections(description):
    """
    Parse a bug description into its template sections (Issue Summary,
    Environment Details, Expected/Actual Results, Steps to Reproduce, etc.).
    Handles Jira wiki markup: *Section Name:*, *Section Name*:, etc.
    Returns a dict of {section_name: content}.
    """
    if not description:
        return {}

    SECTION_NAMES = (
        "issue summary", "environment details", "environment",
        "expected results", "actual results",
        "steps to reproduce", "workaround", "tech-support link",
    )

    section_pattern = re.compile(
        r"^[ \t]*\*?("
        + "|".join(re.escape(n) for n in SECTION_NAMES)
        + r")[\s:*]*(?:\(.*?\))?\s*$",
        re.IGNORECASE | re.MULTILINE,
    )

    sections = {}
    splits = list(section_pattern.finditer(description))

    for i, match in enumerate(splits):
        name = match.group(1).strip().lower()
        if name == "environment":
            name = "environment details"
        start = match.end()
        end = splits[i + 1].start() if i + 1 < len(splits) else len(description)
        content = description[start:end].strip()
        if content:
            sections[name] = content

    if not splits:
        numbered = re.findall(r"^\s*\d+\.\s+.+", description, re.MULTILINE)
        if len(numbered) >= 2:
            sections["steps to reproduce"] = "\n".join(numbered)

    return sections


def _extract_dev_comments(comments, reporter_id):
    """Get the last 3 comments from non-reporter users (developer notes)."""
    dev_comments = []
    for c in reversed(comments):
        author_id = c.get("author", {}).get("accountId", "")
        if author_id != reporter_id:
            body = c.get("body", "").strip()
            if body:
                author_name = c.get("author", {}).get("displayName", "Unknown")
                dev_comments.append(f"*{author_name}:*\n{body}")
        if len(dev_comments) >= 3:
            break
    dev_comments.reverse()
    return dev_comments


def extract_verification_info(bug_key, jira_email, jira_token):
    """
    Build a verification text block from:
      1. Steps to Reproduce from the description
      2. Test Steps custom field (customfield_11772)
      3. Recent developer comments
    When explicit steps are missing, falls back to the full bug context
    (issue summary, environment, expected/actual results) so QA always
    has enough info to verify.
    """
    url = (
        f"{JIRA_BASE}/rest/api/2/issue/{bug_key}"
        f"?fields=description,{TEST_STEPS_FIELD},comment,reporter"
    )
    try:
        resp = requests.get(url, auth=(jira_email, jira_token), timeout=30)
        resp.raise_for_status()
        fields = resp.json()["fields"]
    except Exception as e:
        log.warning(f"  Failed to fetch verification info for {bug_key}: {e}")
        return "Could not retrieve verification steps."

    desc = fields.get("description") or ""
    desc_sections = _parse_description_sections(desc)
    output = []

    steps = desc_sections.get("steps to reproduce")
    if steps:
        output.append(f"*Steps to Reproduce:*\n{steps}")

    test_steps = fields.get(TEST_STEPS_FIELD)
    if test_steps and isinstance(test_steps, str) and test_steps.strip():
        output.append(f"*Test Steps:*\n{test_steps.strip()}")
    elif test_steps and isinstance(test_steps, list):
        formatted = "\n".join(
            f"{i+1}. {s}" if isinstance(s, str) else f"{i+1}. {json.dumps(s)}"
            for i, s in enumerate(test_steps)
        )
        output.append(f"*Test Steps:*\n{formatted}")

    if not steps and not test_steps:
        for label, key in [
            ("Issue Summary", "issue summary"),
            ("Environment Details", "environment details"),
            ("Expected Results", "expected results"),
            ("Actual Results", "actual results"),
            ("Workaround", "workaround"),
        ]:
            val = desc_sections.get(key)
            if val:
                output.append(f"*{label}:*\n{val}")
        if not output and desc.strip():
            output.append(f"*Bug Description:*\n{desc.strip()}")

    reporter_id = (fields.get("reporter") or {}).get("accountId", "")
    comments = (fields.get("comment") or {}).get("comments", [])
    dev_notes = _extract_dev_comments(comments, reporter_id)
    if dev_notes:
        output.append("*Developer Notes:*\n" + "\n---\n".join(dev_notes))

    if not output:
        return "No verification info found. Check the Jira ticket."

    text = "\n\n".join(output)
    if len(text) > 2000:
        text = text[:1997] + "..."
    return text

# ── Jenkins ───────────────────────────────────────────────────────────────────


def trigger_jenkins_build(jenkins_user, jenkins_token, branch, bug_key, bug_summary):
    job_path = f"job/drivenets/job/cheetah/job/{branch}"
    url = f"{JENKINS_BASE}/{job_path}/buildWithParameters"
    params = {
        "SHOULD_LINT": "Yes",
        "SHOULD_BUILD_DNOS_CONTAINERS": "Yes",
        "SHOULD_BUILD_TARBALLS": "Yes",
        "SHOULD_BUILD_BASEOS_CONTAINERS": "Yes",
        "SHOULD_RUN_SMOKE_TESTS": "Yes",
        "SHOULD_ALLOW_DELTA_BUILD": "No",
        "TESTS_TO_RUN": "No Tests",
        "HTML_ADDITIONS": f"Auto-triggered: {bug_key} resolved ({bug_summary})",
    }
    try:
        resp = requests.post(
            url, auth=(jenkins_user, jenkins_token), data=params, timeout=30,
        )
        if resp.status_code == 201:
            log.info(f"Jenkins build triggered on {unquote(branch)} for {bug_key}")
            return True
        else:
            log.error(
                f"Jenkins trigger failed on {unquote(branch)}: "
                f"HTTP {resp.status_code}"
            )
            return False
    except Exception as e:
        log.error(f"Jenkins trigger error: {e}")
        return False

# ── Slack ─────────────────────────────────────────────────────────────────────


def send_slack_notification(webhook_url, bug_key, summary, assignee, old_status,
                            new_status, priority, fix_versions, branch,
                            verification_text):
    jira_url = f"{JIRA_BASE}/browse/{bug_key}"
    jenkins_url = (
        f"{JENKINS_BASE}/job/drivenets/job/cheetah/job/{branch}/"
        if branch else None
    )
    version_str = ", ".join(fix_versions) if fix_versions else "N/A"

    if new_status in ACTIONABLE_CLOSED:
        header_emoji = ":white_check_mark:"
        action_text = "Ready for verification"
    else:
        header_emoji = ":warning:"
        action_text = f"Closed as {new_status}"

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{header_emoji} Bug {new_status}: {bug_key}",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*<{jira_url}|{bug_key}>*: {summary}\n\n"
                    f"*Status:* {old_status} :arrow_right: *{new_status}*\n"
                    f"*Assignee:* {assignee}\n"
                    f"*Priority:* {priority}\n"
                    f"*Fix Version:* {version_str}\n\n"
                    f"_{action_text}_"
                ),
            },
        },
    ]

    if jenkins_url:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":jenkins: *Jenkins Build:* <{jenkins_url}|{unquote(branch)}>\n"
                    f"A build has been triggered automatically. "
                    f"Check the link above for build status."
                ),
            },
        })

    blocks.append({
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": f":clipboard: *Verification Steps:*\n\n{verification_text}",
        },
    })
    blocks.append({"type": "divider"})

    try:
        resp = requests.post(
            webhook_url,
            json={"blocks": blocks},
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        if resp.status_code == 200:
            log.info(f"Slack notification sent for {bug_key}")
            return True
        log.error(f"Slack webhook failed: HTTP {resp.status_code} - {resp.text}")
        return False
    except Exception as e:
        log.error(f"Slack webhook error: {e}")
        return False

# ── Main loop ─────────────────────────────────────────────────────────────────


def main():
    cfg = get_config()
    jira_email = cfg["JIRA_EMAIL"]
    jira_token = cfg["JIRA_TOKEN"]
    jenkins_user = cfg["JENKINS_USER"]
    jenkins_token = cfg["JENKINS_TOKEN"]
    slack_webhook = cfg["SLACK_WEBHOOK_URL"]
    poll_interval = cfg["poll_interval"]
    jql_filter = cfg["jql_filter"]

    state = load_state()

    log.info("=" * 60)
    log.info("Bug Monitor started (dynamic JQL + Jenkins + Slack)")
    log.info(f"User: {jira_email}")
    log.info(f"Poll interval: {poll_interval}s")
    log.info(f"State file: {STATE_FILE}")
    if jql_filter:
        log.info(f"Custom JQL: {jql_filter}")
    else:
        log.info("JQL: reporter = currentUser(), type = Bug, not Verified, last 180d")
    log.info("=" * 60)

    while True:
        bugs = fetch_my_bugs(jira_email, jira_token, jql_filter)
        log.info(f"Tracking {len(bugs)} bugs")

        for bug in bugs:
            key = bug["key"]
            current_status = bug["status"]
            prev = state.get(key, {})
            prev_status = prev.get("status")

            if prev_status and prev_status != current_status:
                log.info(
                    f"STATUS CHANGE: {key} [{bug['summary'][:60]}] "
                    f"{prev_status} -> {current_status}"
                )

                if (current_status in CLOSED_STATUSES
                        and prev_status not in CLOSED_STATUSES):
                    log.info(
                        f"BUG RESOLVED: {key} -> {current_status}. "
                        f"Detecting branch..."
                    )
                    branch = detect_branch_from_jira(key, jira_email, jira_token)

                    if branch:
                        log.info(f"  Using branch: {unquote(branch)}")
                        trigger_jenkins_build(
                            jenkins_user, jenkins_token, branch,
                            key, bug["summary"][:60],
                        )
                    else:
                        log.warning(
                            f"  No branch detected for {key}, "
                            f"skipping Jenkins trigger"
                        )

                    verification_text = extract_verification_info(
                        key, jira_email, jira_token,
                    )
                    send_slack_notification(
                        slack_webhook, key, bug["summary"], bug["assignee"],
                        prev_status, current_status, bug["priority"],
                        bug["fix_versions"], branch, verification_text,
                    )

            elif not prev_status:
                log.info(
                    f"Tracking: {key} = {current_status} "
                    f"({bug['assignee']}) | {bug['summary'][:60]}"
                )

            state[key] = {
                "status": current_status,
                "summary": bug["summary"],
                "assignee": bug["assignee"],
                "priority": bug["priority"],
                "fix_versions": bug["fix_versions"],
                "last_checked": datetime.now().isoformat(),
            }

        save_state(state)
        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
