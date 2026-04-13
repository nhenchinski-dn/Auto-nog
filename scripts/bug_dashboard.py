#!/usr/bin/env python3
"""
Multi-user QA bug dashboard. Each team member logs in with their own
Jira email + API token. Sessions are stored server-side in SQLite.

Required env vars:
    JENKINS_USER   - Jenkins username (shared)
    JENKINS_TOKEN  - Jenkins API token (shared)

Optional env vars:
    DASHBOARD_PORT   - Port to run on (default: 5000)
    DASHBOARD_SECRET - Fernet key for encrypting stored tokens
                       (auto-generated on first run)
"""

import os
import re
import json
import uuid
import sqlite3
import hashlib
import requests
from pathlib import Path
from datetime import datetime
from urllib.parse import unquote, quote
from flask import Flask, jsonify, request, render_template_string, redirect, make_response
from cryptography.fernet import Fernet

app = Flask(__name__)

JIRA_BASE = "https://drivenets.atlassian.net"
JENKINS_BASE = "https://jenkins.dev.drivenets.net"
SESSION_DB = Path.home() / ".bug_dashboard_sessions.db"
STATE_DIR = Path.home() / ".bug_dashboard_state"

FIX_VERSION_TO_BRANCH = {
    "v26.1": "dev_v26_1",
    "v26.2": "dev_v26_2",
    "v25.4": "dev_v25_4",
    "v25.4.1": "dev_v25_4_1",
}
Y1731_EPIC = "SW-141523"
Y1731_FEATURE_BRANCH = (
    "feature%2Fv26.2%2FSW-141523-y1731-proactive-initiator-performance-monitoring"
)

CLOSED_STATUSES = {
    "Done", "Closed", "Resolved", "Cancelled", "Won't Fix", "Duplicate", "Verified",
}

STATUS_ORDER = {
    "Open": 0, "Under Investigation": 1, "In Progress": 2, "In Review": 3,
    "Pending Merge": 4, "Closed": 5, "Done": 5, "Resolved": 5,
    "Verified": 6, "Cancelled": 7, "Won't Fix": 7, "Duplicate": 7,
}


# ── Encryption ────────────────────────────────────────────────────────────────

def _get_fernet():
    secret = os.environ.get("DASHBOARD_SECRET")
    if not secret:
        secret_file = Path.home() / ".bug_dashboard_secret"
        if secret_file.exists():
            secret = secret_file.read_text().strip()
        else:
            secret = Fernet.generate_key().decode()
            secret_file.write_text(secret)
            secret_file.chmod(0o600)
    return Fernet(secret.encode() if isinstance(secret, str) else secret)


_fernet = _get_fernet()


# ── SQLite session store ──────────────────────────────────────────────────────

def _init_db():
    conn = sqlite3.connect(str(SESSION_DB))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            email TEXT NOT NULL,
            token_enc TEXT NOT NULL,
            display_name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            limited INTEGER NOT NULL DEFAULT 0
        )
    """)
    # Migration: add limited column if missing (existing DBs)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()]
    if "limited" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN limited INTEGER NOT NULL DEFAULT 0")
    conn.commit()
    conn.close()


_init_db()


def _create_session(email, token, display_name, limited=False):
    sid = uuid.uuid4().hex
    token_enc = _fernet.encrypt(token.encode()).decode()
    conn = sqlite3.connect(str(SESSION_DB))
    conn.execute("DELETE FROM sessions WHERE email = ?", (email,))
    conn.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
        (sid, email, token_enc, display_name,
         datetime.utcnow().isoformat(), 1 if limited else 0),
    )
    conn.commit()
    conn.close()
    return sid


def _get_session(sid):
    if not sid:
        return None
    conn = sqlite3.connect(str(SESSION_DB))
    row = conn.execute(
        "SELECT email, token_enc, display_name, limited FROM sessions WHERE session_id = ?",
        (sid,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    try:
        token = _fernet.decrypt(row[1].encode()).decode()
    except Exception:
        return None
    return {"email": row[0], "token": token, "display_name": row[2],
            "limited": bool(row[3])}


def _delete_session(sid):
    conn = sqlite3.connect(str(SESSION_DB))
    conn.execute("DELETE FROM sessions WHERE session_id = ?", (sid,))
    conn.commit()
    conn.close()


def _service_auth():
    """Return (email, token) from env vars for the shared service account, or None."""
    e = os.environ.get("JIRA_EMAIL")
    t = os.environ.get("JIRA_TOKEN")
    if e and t:
        return (e, t)
    return None


def _get_user_auth():
    sid = request.cookies.get("session_id")
    session = _get_session(sid)
    if not session:
        return None
    return (session["email"], session["token"])


def _is_limited():
    sid = request.cookies.get("session_id")
    session = _get_session(sid)
    return session.get("limited", False) if session else False


def _require_auth():
    """Return (email, token) for Jira calls. Limited users get service auth."""
    sid = request.cookies.get("session_id")
    session = _get_session(sid)
    if not session:
        return None
    if session["limited"]:
        sa = _service_auth()
        if not sa:
            return None
        return sa
    return (session["email"], session["token"])


def _get_user_email():
    """Return the logged-in user's email regardless of limited status."""
    sid = request.cookies.get("session_id")
    session = _get_session(sid)
    return session["email"] if session else None


def _jenkins_auth():
    return (os.environ["JENKINS_USER"], os.environ["JENKINS_TOKEN"])


def _user_hash():
    email = _get_user_email()
    if not email:
        return None
    return hashlib.md5(email.lower().encode()).hexdigest()[:12]


def _load_state():
    h = _user_hash()
    if not h:
        return {}
    STATE_DIR.mkdir(exist_ok=True)
    state_file = STATE_DIR / f"{h}.json"
    if state_file.exists():
        with open(state_file) as f:
            return json.load(f)
    old = Path.home() / ".bug_monitor_state.json"
    if old.exists():
        with open(old) as f:
            return json.load(f)
    return {}


def _load_user_prefs():
    h = _user_hash()
    if not h:
        return {}
    STATE_DIR.mkdir(exist_ok=True)
    pf = STATE_DIR / f"{h}_prefs.json"
    if pf.exists():
        with open(pf) as f:
            return json.load(f)
    return {}


def _save_user_prefs(prefs):
    h = _user_hash()
    if not h:
        return
    STATE_DIR.mkdir(exist_ok=True)
    pf = STATE_DIR / f"{h}_prefs.json"
    with open(pf, "w") as f:
        json.dump(prefs, f)


def _fetch_live_bugs(auth, reporter_email=None):
    """Fetch all bugs reported by a user from Jira."""
    if reporter_email:
        jql = (
            f'reporter = "{reporter_email}" AND issuetype = Bug '
            "AND created >= -180d ORDER BY updated DESC"
        )
    else:
        jql = (
            "reporter = currentUser() AND issuetype = Bug "
            "AND created >= -180d ORDER BY updated DESC"
        )
    issues = []
    next_token = None

    while True:
        body = {
            "jql": jql,
            "fields": [
                "status", "summary", "assignee", "priority",
                "fixVersions", "created", "updated",
            ],
            "maxResults": 50,
        }
        if next_token:
            body["nextPageToken"] = next_token

        resp = requests.post(
            f"{JIRA_BASE}/rest/api/3/search/jql",
            auth=auth, json=body, timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        for issue in data.get("issues", []):
            f = issue["fields"]
            assignee = "Unassigned"
            if f.get("assignee"):
                assignee = f["assignee"].get("displayName", "Unassigned")
            priority = "None"
            if f.get("priority"):
                priority = f["priority"].get("name", "None")
            fix_versions = [
                fv.get("name", "") for fv in (f.get("fixVersions") or [])
            ]
            issues.append({
                "key": issue["key"],
                "summary": f.get("summary", "N/A"),
                "status": f["status"]["name"],
                "assignee": assignee,
                "priority": priority,
                "fix_versions": fix_versions,
                "created": f.get("created", ""),
                "updated": f.get("updated", ""),
                "jira_url": f"{JIRA_BASE}/browse/{issue['key']}",
            })

        if data.get("isLast", True):
            break
        next_token = data.get("nextPageToken")

    return issues


def _detect_branch(bug_key, auth):
    url = (
        f"{JIRA_BASE}/rest/api/2/issue/{bug_key}"
        f"?fields=fixVersions,comment,issuelinks,parent"
    )
    try:
        resp = requests.get(url, auth=auth, timeout=30)
        resp.raise_for_status()
        fields = resp.json()["fields"]

        jenkins_re = re.compile(
            r"jenkins\.dev\.drivenets\.net/job/drivenets/job/cheetah/job/([^/]+)/"
        )
        for c in reversed(fields.get("comment", {}).get("comments", [])):
            m = jenkins_re.search(c.get("body", ""))
            if m:
                return m.group(1)

        for fv in fields.get("fixVersions") or []:
            name = fv.get("name", "")
            if name in FIX_VERSION_TO_BRANCH:
                return FIX_VERSION_TO_BRANCH[name]

        for link in fields.get("issuelinks") or []:
            for d in ("outwardIssue", "inwardIssue"):
                if link.get(d, {}).get("key") == Y1731_EPIC:
                    return Y1731_FEATURE_BRANCH

        parent = fields.get("parent") or {}
        if parent.get("key") == Y1731_EPIC:
            return Y1731_FEATURE_BRANCH
    except Exception:
        pass
    return None


def _parse_description_sections(description):
    """
    Parse a bug description into its template sections. Handles the standard
    Jira wiki markup format:
        *Issue Summary:*
        *Environment details:* (include hostname/IP)
        *Steps to Reproduce:*
        *Actual Results*:
    etc.

    Returns a dict of {section_name: content} for every section found,
    plus a 'full_description' key with the raw text.
    """
    if not description:
        return {"full_description": ""}

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

    sections["full_description"] = description.strip()
    return sections


def _infer_topology(summary, description=""):
    """Infer the DNOS test topology from bug summary/description."""
    text = (summary + " " + (description or "")).lower()

    platform = "DUT"
    for tag, label in [
        ("q3d", "Q3D"), ("ncp9", "NCP9"), ("ncp6", "NCP6-S"),
        ("ncp3", "NCP3-SA"), ("ncpl", "NCPL"), ("me100", "ME100"),
        ("me10", "ME10"), ("mse", "MSE"), ("ase", "ASE"),
        ("agg", "AGG"), ("nc-ai", "NC-AI"), ("dn-ai", "DN-AI"),
    ]:
        if tag in text:
            platform = label
            break

    needs_traffic = any(k in text for k in (
        "traffic", "forwarding", "packet", "frame", "throughput",
        "ixia", "spirent", "trex", "wire-rate", "line-rate",
        "bps", "pps", "data-plane", "dataplane", "tx counter",
        "rx counter", "rx drop", "tx drop",
    ))

    verify_snmp = any(k in text for k in (
        "snmp", "mib", "oid", "trap", "snmpwalk", "snmpget",
    ))
    verify_show = "show " in text or "sh " in text
    verify_cli = any(k in text for k in (
        "(cfg)", "commit", "no services", "admin-state",
    ))
    verify_netconf = any(k in text for k in (
        "netconf", "gnmi", "restconf", "yang", "oper-items",
    ))
    verify_syslog = any(k in text for k in (
        "syslog", "system_event", "system event", "log message",
    ))

    verify = []
    if verify_snmp:
        verify.append("snmpwalk")
    if verify_show:
        verify.append("show cmd")
    if verify_cli:
        verify.append("CLI config")
    if verify_netconf:
        verify.append("NETCONF/gNMI")
    if verify_syslog:
        verify.append("syslog/events")
    if needs_traffic:
        verify.append("traffic")

    base = {"platform": platform, "traffic": needs_traffic, "verify": verify}

    if any(k in text for k in ("y.1731", "y1731", "cfm", "oam",
                                "eth-lb", "eth-lt", "linktrace",
                                "loopback")):
        is_pm = any(k in text for k in (
            "slm", "dm ", "delay", "loss", "performance-monitoring",
            "proactive", "on-demand",
        ))
        return {**base, "type": "cfm", "subtype": "pm" if is_pm else "fault"}

    if any(k in text for k in ("multicast", "igmp", "pim", "mld",
                                "mrib", "forwarding-table")):
        return {**base, "type": "multicast", "traffic": True}

    if any(k in text for k in ("evpn", "vxlan", "vpls",
                                "bridge-domain", "seamless")):
        return {**base, "type": "evpn"}

    if any(k in text for k in ("l2xc", "l2-cross-connect", "vpws",
                                "cross-connect", "pseudowire")):
        return {**base, "type": "l2xc"}

    if any(k in text for k in ("qos", "dscp", "cos", "policer",
                                "scheduler", "shaper", "queuing",
                                "pcp ", "ecn ", "wred")):
        return {**base, "type": "qos", "traffic": True}

    if any(k in text for k in ("acl", "access-list", "i-acl",
                                "e-acl")):
        return {**base, "type": "acl"}

    if any(k in text for k in ("bgp", "local-as", "remote-as",
                                "ebgp", "ibgp", "rib-install",
                                "zebra", "fibmgr")):
        return {**base, "type": "bgp"}

    if any(k in text for k in ("ospf", "isis", "is-is", "ldp",
                                "mpls", "sr-mpls", "segment-routing",
                                "bfd")):
        return {**base, "type": "routing"}

    if any(k in text for k in ("ha ", "high-availability", "failover",
                                "switchover", "redundancy", "nse",
                                "safemode", "safe mode", "recovery")):
        return {**base, "type": "ha"}

    if any(k in text for k in ("interface", "port ", "link down",
                                "link up", "optic", "transceiver",
                                "sfp", "qsfp", "400g", "100g",
                                "10g ", "lldp", "lacp", "lag",
                                "bundle", "sub-interface")):
        return {**base, "type": "interface"}

    if any(k in text for k in ("thermal", "temperature", "fan", "rpm",
                                "power supply", "psu", "voltage",
                                "sensor", "ipmi", "usb", "hardware")):
        return {**base, "type": "hw"}

    if any(k in text for k in ("crash", "core dump", "segfault",
                                "memory leak", "oom", "watchdog",
                                "kernel panic", "traceback",
                                "restart", "safemode")):
        return {**base, "type": "crash"}

    if any(k in text for k in ("upgrade", "downgrade", "issu",
                                "install", "firmware", "image",
                                "golden-image", "baseimage")):
        return {**base, "type": "upgrade"}

    if verify_snmp and not needs_traffic:
        return {**base, "type": "snmp"}

    if verify_syslog and not needs_traffic:
        return {**base, "type": "syslog"}

    if verify_netconf and not needs_traffic:
        return {**base, "type": "management"}

    if (verify_cli or verify_show) and not needs_traffic:
        return {**base, "type": "cli"}

    return {**base, "type": "generic"}


def _extract_verification(bug_key, auth):
    """
    Extract verification info from description, test steps field, and comments.
    When explicit steps aren't found, provides the full bug context so the QA
    always has enough info to verify.
    """
    url = (
        f"{JIRA_BASE}/rest/api/2/issue/{bug_key}"
        f"?fields=description,summary,comment,reporter"
    )
    try:
        resp = requests.get(url, auth=auth, timeout=30)
        resp.raise_for_status()
        fields = resp.json()["fields"]
    except Exception:
        return {
            "steps_to_reproduce": None,
            "dev_comments": [], "bug_context": None,
        }

    desc = fields.get("description") or ""
    sections = _parse_description_sections(desc)

    steps = sections.get("steps to reproduce")

    bug_context = None
    if not steps:
        context_parts = []
        for label, key in [
            ("Issue Summary", "issue summary"),
            ("Environment Details", "environment details"),
            ("Expected Results", "expected results"),
            ("Actual Results", "actual results"),
            ("Workaround", "workaround"),
        ]:
            val = sections.get(key)
            if val:
                context_parts.append(f"{label}:\n{val}")

        if context_parts:
            bug_context = "\n\n".join(context_parts)
        elif desc.strip():
            bug_context = desc.strip()

    mention_re = re.compile(r"\[~accountid:[a-f0-9:-]+\]")
    account_names = {}
    for c in (fields.get("comment") or {}).get("comments", []):
        aid = c.get("author", {}).get("accountId", "")
        dname = c.get("author", {}).get("displayName", "")
        if aid and dname:
            account_names[aid] = dname

    def _clean_mentions(text):
        def _repl(m):
            aid = m.group(0)[len("[~accountid:"):-1]
            return "@" + account_names.get(aid, "user")
        return mention_re.sub(_repl, text)

    reporter_id = (fields.get("reporter") or {}).get("accountId", "")
    comments = (fields.get("comment") or {}).get("comments", [])
    dev_comments = []
    for c in reversed(comments):
        aid = c.get("author", {}).get("accountId", "")
        if aid != reporter_id:
            body = _clean_mentions(c.get("body", "").strip())
            if body:
                name = c.get("author", {}).get("displayName", "Unknown")
                dev_comments.append({"author": name, "body": body})
        if len(dev_comments) >= 3:
            break
    dev_comments.reverse()

    env_details = sections.get("environment details") or ""
    hostnames = _extract_hostnames(env_details + " " + desc)

    return {
        "steps_to_reproduce": steps,
        "dev_comments": dev_comments,
        "bug_context": bug_context,
        "hostnames": hostnames,
    }


def _extract_hostnames(text):
    """Pull device hostnames and management IPs from text for SSH quick-links."""
    hosts = []

    mgmt_re = re.compile(
        r"(?:management|mgmt|ssh|host(?:name)?|device|dut|ip)\s*"
        r"[:\-=\s]+\s*(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b",
        re.IGNORECASE,
    )
    for m in mgmt_re.finditer(text):
        ip = m.group(1)
        if ip not in hosts:
            hosts.append(ip)

    ip_re = re.compile(r"\b(10\.\d{1,3}\.\d{1,3}\.\d{1,3})\b")
    for m in ip_re.finditer(text):
        ip = m.group(1)
        if ip not in hosts:
            hosts.append(ip)

    device_re = re.compile(
        r"\b([a-z][a-z0-9_-]*(?:nog|ncp|q[23][cd]|dut|leaf|spine|agg|mse|ase)"
        r"[a-z0-9_-]*)\b",
        re.IGNORECASE,
    )
    seen = {h.lower() for h in hosts}
    for m in device_re.finditer(text):
        name = m.group(1)
        if name.lower() not in seen and len(name) >= 4:
            hosts.append(name)
            seen.add(name.lower())

    return hosts[:5]


def _fetch_linked_bugs(bug_key, auth):
    url = f"{JIRA_BASE}/rest/api/2/issue/{bug_key}?fields=issuelinks"
    try:
        resp = requests.get(url, auth=auth, timeout=15)
        resp.raise_for_status()
        links = resp.json()["fields"].get("issuelinks") or []
    except Exception:
        return []
    result = []
    for link in links:
        rel = link.get("type", {}).get("outward", "relates to")
        for direction in ("outwardIssue", "inwardIssue"):
            issue = link.get(direction)
            if issue:
                if direction == "inwardIssue":
                    rel = link.get("type", {}).get("inward", "relates to")
                result.append({
                    "key": issue["key"],
                    "summary": issue.get("fields", {}).get("summary", ""),
                    "status": issue.get("fields", {}).get("status", {}).get("name", ""),
                    "relation": rel,
                    "url": f"{JIRA_BASE}/browse/{issue['key']}",
                })
    return result


def _jenkins_build_status(branch):
    """Get latest build status for a Jenkins branch."""
    if not branch:
        return None
    branch_encoded = branch if "%2F" in branch else quote(branch, safe="")
    url = (
        f"{JENKINS_BASE}/job/drivenets/job/cheetah/job/{branch_encoded}"
        f"/lastBuild/api/json?tree=result,building,number,timestamp,duration"
    )
    try:
        resp = requests.get(url, auth=_jenkins_auth(), timeout=10)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        return {
            "number": data.get("number"),
            "building": data.get("building", False),
            "result": data.get("result"),
            "timestamp": data.get("timestamp"),
            "duration": data.get("duration"),
        }
    except Exception:
        return None


# ── API routes ────────────────────────────────────────────────────────────────

def _branch_from_fix_versions(fix_versions):
    """Derive Jenkins branch from fixVersions without an extra API call."""
    for v in fix_versions or []:
        if v in FIX_VERSION_TO_BRANCH:
            return FIX_VERSION_TO_BRANCH[v]
    return None


# ── Auth API routes ───────────────────────────────────────────────────────────

@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    token = (data.get("token") or "").strip()
    if not email:
        return jsonify({"error": "Email is required"}), 400

    if token:
        # Full login: validate the user's own Jira token
        try:
            resp = requests.get(
                f"{JIRA_BASE}/rest/api/3/myself",
                auth=(email, token), timeout=15,
            )
            if resp.status_code == 401:
                return jsonify({"error": "Invalid email or token"}), 401
            resp.raise_for_status()
            me = resp.json()
            display_name = me.get("displayName", email.split("@")[0])
        except requests.RequestException as e:
            return jsonify({"error": f"Could not reach Jira: {e}"}), 502

        sid = _create_session(email, token, display_name, limited=False)
        resp_out = make_response(jsonify({
            "ok": True, "display_name": display_name, "limited": False,
        }))
    else:
        # Limited login: verify email exists via service account
        sa = _service_auth()
        if not sa:
            return jsonify({
                "error": "Viewer mode unavailable — no service account configured",
            }), 400
        try:
            resp = requests.get(
                f"{JIRA_BASE}/rest/api/3/user/search?query={email}",
                auth=sa, timeout=15,
            )
            resp.raise_for_status()
            users = resp.json()
            match = next(
                (u for u in users
                 if (u.get("emailAddress") or "").lower() == email.lower()),
                None,
            )
            if not match:
                return jsonify({"error": "Email not found in Jira"}), 404
            display_name = match.get("displayName", email.split("@")[0])
        except requests.RequestException as e:
            return jsonify({"error": f"Could not reach Jira: {e}"}), 502

        placeholder_token = "VIEWER_MODE"
        sid = _create_session(email, placeholder_token, display_name, limited=True)
        resp_out = make_response(jsonify({
            "ok": True, "display_name": display_name, "limited": True,
        }))

    resp_out.set_cookie("session_id", sid, httponly=True, samesite="Lax", max_age=30*86400)
    return resp_out


@app.route("/api/logout", methods=["POST"])
def api_logout():
    sid = request.cookies.get("session_id")
    if sid:
        _delete_session(sid)
    resp = make_response(jsonify({"ok": True}))
    resp.delete_cookie("session_id")
    return resp


@app.route("/api/me")
def api_me():
    sid = request.cookies.get("session_id")
    session = _get_session(sid)
    if not session:
        return jsonify({"error": "Not logged in"}), 401
    return jsonify({
        "email": session["email"],
        "display_name": session["display_name"],
        "limited": session.get("limited", False),
    })


# ── Bug API routes ───────────────────────────────────────────────────────────

@app.route("/api/bugs")
def api_bugs():
    auth = _require_auth()
    if not auth:
        return jsonify({"error": "Not logged in"}), 401
    state = _load_state()
    try:
        email = _get_user_email()
        live = _fetch_live_bugs(auth, reporter_email=email if _is_limited() else None)
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    bugs = []
    for bug in live:
        local = state.get(bug["key"], {})
        branch = (
            _branch_from_fix_versions(bug.get("fix_versions"))
            or local.get("branch")
        )
        if branch:
            bug["branch"] = unquote(branch)
            bug["jenkins_url"] = (
                f"{JENKINS_BASE}/job/drivenets/job/cheetah/job/{branch}/"
            )
        else:
            bug["branch"] = None
            bug["jenkins_url"] = None
        bug["last_checked"] = local.get("last_checked")
        bugs.append(bug)

    bugs.sort(key=lambda b: STATUS_ORDER.get(b["status"], 99))
    return jsonify(bugs)


@app.route("/api/bugs/<key>/details")
def api_bug_details(key):
    auth = _require_auth()
    if not auth:
        return jsonify({"error": "Not logged in"}), 401

    branch = _detect_branch(key, auth)
    if not branch:
        state = _load_state()
        branch = state.get(key, {}).get("branch")

    verification = _extract_verification(key, auth)
    linked = _fetch_linked_bugs(key, auth)
    build_status = _jenkins_build_status(branch) if branch else None

    bug = None
    email = _get_user_email()
    for b in _fetch_live_bugs(auth, reporter_email=email if _is_limited() else None):
        if b["key"] == key:
            bug = b
            break
    summary = bug["summary"] if bug else key
    desc = verification.get("bug_context") or verification.get("steps_to_reproduce") or ""
    topology = _infer_topology(summary, desc)

    jenkins_url = None
    if branch:
        jenkins_url = f"{JENKINS_BASE}/job/drivenets/job/cheetah/job/{branch}/"
    return jsonify({
        "branch": unquote(branch) if branch else None,
        "jenkins_url": jenkins_url,
        "verification": verification,
        "topology": topology,
        "linked_bugs": linked,
        "build_status": build_status,
    })


@app.route("/api/bugs/<key>/build", methods=["POST"])
def api_trigger_build(key):
    auth = _require_auth()
    if not auth:
        return jsonify({"error": "Not logged in"}), 401
    data = request.get_json(silent=True) or {}
    branch = data.get("branch")

    if not branch:
        branch_raw = _detect_branch(key, auth)
        if branch_raw:
            branch = branch_raw
        else:
            return jsonify({"error": "No branch detected for this bug"}), 400

    if "/" in branch and "%2F" not in branch:
        branch_encoded = quote(branch, safe="")
    else:
        branch_encoded = branch

    job_path = f"job/drivenets/job/cheetah/job/{branch_encoded}"
    url = f"{JENKINS_BASE}/{job_path}/buildWithParameters"
    params = {
        "SHOULD_LINT": "Yes",
        "SHOULD_BUILD_DNOS_CONTAINERS": "Yes",
        "SHOULD_BUILD_TARBALLS": "Yes",
        "SHOULD_BUILD_BASEOS_CONTAINERS": "Yes",
        "SHOULD_RUN_SMOKE_TESTS": "Yes",
        "SHOULD_ALLOW_DELTA_BUILD": "No",
        "TESTS_TO_RUN": "No Tests",
        "HTML_ADDITIONS": f"Auto-triggered from dashboard: {key}",
    }
    try:
        resp = requests.post(
            url, auth=_jenkins_auth(), data=params, timeout=30,
        )
        if resp.status_code == 201:
            jenkins_url = f"{JENKINS_BASE}/{job_path}/"
            return jsonify({
                "ok": True,
                "branch": unquote(branch_encoded),
                "jenkins_url": jenkins_url,
            })
        return jsonify({
            "error": f"Jenkins returned HTTP {resp.status_code}",
        }), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/bugs/<key>/comment", methods=["POST"])
def api_add_comment(key):
    auth = _require_auth()
    if not auth:
        return jsonify({"error": "Not logged in"}), 401
    if _is_limited():
        return jsonify({"error": "Adding comments requires full sign-in with an API token"}), 403
    data = request.get_json(silent=True) or {}
    body = (data.get("body") or "").strip()
    if not body:
        return jsonify({"error": "Comment body required"}), 400
    try:
        resp = requests.post(
            f"{JIRA_BASE}/rest/api/2/issue/{key}/comment",
            auth=auth,
            json={"body": body},
            timeout=15,
        )
        resp.raise_for_status()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 502


# ── User preferences API ─────────────────────────────────────────────────────

@app.route("/api/user/prefs")
def api_get_prefs():
    if not _require_auth():
        return jsonify({"error": "Not logged in"}), 401
    return jsonify(_load_user_prefs())


@app.route("/api/user/prefs", methods=["POST"])
def api_save_prefs():
    if not _require_auth():
        return jsonify({"error": "Not logged in"}), 401
    data = request.get_json(silent=True) or {}
    prefs = _load_user_prefs()
    for k in ("pinned", "notes", "checklist", "theme"):
        if k in data:
            prefs[k] = data[k]
    _save_user_prefs(prefs)
    return jsonify({"ok": True})


# ── Team overview API ────────────────────────────────────────────────────────

@app.route("/api/team")
def api_team():
    if not _require_auth():
        return jsonify({"error": "Not logged in"}), 401
    conn = sqlite3.connect(str(SESSION_DB))
    rows = conn.execute("SELECT email, display_name FROM sessions").fetchall()
    conn.close()
    sa = _service_auth()
    team = []
    for email, dname in rows:
        counts = {"open": 0, "active": 0, "verify": 0, "closed": 0, "total": 0}
        try:
            auth = sa if sa else _require_auth()
            if not auth:
                continue
            jql = (
                f'reporter = "{email}" AND issuetype = Bug '
                "AND created >= -180d"
            )
            resp = requests.post(
                f"{JIRA_BASE}/rest/api/3/search/jql",
                auth=auth,
                json={"jql": jql, "fields": ["status"], "maxResults": 200},
                timeout=30,
            )
            if resp.ok:
                issues = resp.json().get("issues", [])
                for iss in issues:
                    st = iss["fields"]["status"]["name"]
                    counts["total"] += 1
                    if st in ("Done", "Closed", "Resolved"):
                        counts["verify"] += 1
                        counts["closed"] += 1
                    elif st in CLOSED_STATUSES:
                        counts["closed"] += 1
                    elif st in ("In Progress", "In Review", "Pending Merge",
                                "Under Investigation"):
                        counts["active"] += 1
                    else:
                        counts["open"] += 1
        except Exception:
            pass
        team.append({"email": email, "display_name": dname, "counts": counts})
    return jsonify(team)


@app.route("/api/team/groups")
def api_team_groups():
    """Search Jira groups. Pass ?q=term to search, or omit for default DN groups."""
    auth = _require_auth()
    if not auth:
        return jsonify({"error": "Not logged in"}), 401
    query = request.args.get("q", "").strip()
    try:
        if query:
            resp = requests.get(
                f"{JIRA_BASE}/rest/api/3/groups/picker",
                auth=auth,
                params={"query": query, "maxResults": 30},
                timeout=15,
            )
            if not resp.ok:
                return jsonify([])
            groups = [
                {"name": g["name"], "groupId": g.get("groupId", "")}
                for g in resp.json().get("groups", [])
            ]
            return jsonify(groups)

        groups = []
        seen = set()
        for prefix in ("DN DevTest", "DN-Devtest", "DN-DP", "DN-CS",
                        "DN MW", "DN Routing", "DN Infra", "DN Data",
                        "DN Hardware", "DN SysArch", "DN Operations",
                        "DN Research", "QA", "E2E", "DevTest",
                        "Data-Path", "Routing", "Team leader",
                        "Team Lead"):
            resp = requests.get(
                f"{JIRA_BASE}/rest/api/3/groups/picker",
                auth=auth,
                params={"query": prefix, "maxResults": 25},
                timeout=15,
            )
            if resp.ok:
                for g in resp.json().get("groups", []):
                    if g["name"] not in seen:
                        seen.add(g["name"])
                        groups.append({"name": g["name"],
                                       "groupId": g.get("groupId", "")})
        groups.sort(key=lambda g: g["name"])
        return jsonify(groups)
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/team/report")
def api_team_report():
    """Return bug counts for every member of a Jira group."""
    auth = _require_auth()
    if not auth:
        return jsonify({"error": "Not logged in"}), 401
    group_name = request.args.get("group", "").strip()
    if not group_name:
        return jsonify({"error": "group parameter required"}), 400
    try:
        members = []
        start = 0
        while True:
            resp = requests.get(
                f"{JIRA_BASE}/rest/api/3/group/member",
                auth=auth,
                params={"groupname": group_name, "startAt": start, "maxResults": 50},
                timeout=15,
            )
            if not resp.ok:
                return jsonify({"error": f"Jira returned {resp.status_code}"}), 502
            data = resp.json()
            for m in data.get("values", []):
                members.append({
                    "email": m.get("emailAddress", ""),
                    "display_name": m.get("displayName", ""),
                    "account_id": m.get("accountId", ""),
                    "active": m.get("active", True),
                })
            if data.get("isLast", True):
                break
            start += 50

        sa = _service_auth()
        query_auth = sa if sa else auth
        print(f"[team-report] group={group_name}, members={len(members)}, "
              f"using_service_acct={sa is not None}, auth_email={query_auth[0] if query_auth else 'NONE'}")

        from concurrent.futures import ThreadPoolExecutor, as_completed

        active_members = [m for m in members if m.get("active", True)]

        def _fetch_member_bugs(m):
            counts = {"open": 0, "active": 0, "verify": 0, "closed": 0, "total": 0}
            try:
                aid = m["account_id"]
                jql = (
                    f"reporter = '{aid}' AND issuetype = Bug "
                    "AND created >= -365d"
                )
                r = requests.post(
                    f"{JIRA_BASE}/rest/api/3/search/jql",
                    auth=query_auth,
                    json={"jql": jql, "fields": ["status"],
                          "maxResults": 100},
                    timeout=30,
                )
                if r.ok:
                    data = r.json()
                    issues = data.get("issues", [])
                    api_total = data.get("total", 0)
                    fetched = len(issues)
                    if fetched == 0 and api_total == 0:
                        pass
                    else:
                        for iss in issues:
                            st = iss["fields"]["status"]["name"]
                            counts["total"] += 1
                            if st in ("Done", "Closed", "Resolved"):
                                counts["verify"] += 1
                                counts["closed"] += 1
                            elif st in CLOSED_STATUSES:
                                counts["closed"] += 1
                            elif st in ("In Progress", "In Review",
                                        "Pending Merge",
                                        "Under Investigation"):
                                counts["active"] += 1
                            else:
                                counts["open"] += 1
                        need = max(api_total, fetched)
                        start_at = fetched
                        while start_at < need:
                            r2 = requests.post(
                                f"{JIRA_BASE}/rest/api/3/search/jql",
                                auth=query_auth,
                                json={"jql": jql, "fields": ["status"],
                                      "maxResults": 100,
                                      "startAt": start_at},
                                timeout=30,
                            )
                            if not r2.ok:
                                break
                            d2 = r2.json()
                            page = d2.get("issues", [])
                            if not page:
                                break
                            for iss in page:
                                st = iss["fields"]["status"]["name"]
                                counts["total"] += 1
                                if st in ("Done", "Closed", "Resolved"):
                                    counts["verify"] += 1
                                    counts["closed"] += 1
                                elif st in CLOSED_STATUSES:
                                    counts["closed"] += 1
                                elif st in ("In Progress", "In Review",
                                            "Pending Merge",
                                            "Under Investigation"):
                                    counts["active"] += 1
                                else:
                                    counts["open"] += 1
                            start_at += len(page)
                            need = max(need,
                                       d2.get("total", 0))
            except Exception as exc:
                print(f"[team-report] Error for {m.get('display_name','?')}: {exc}")
            return {
                "display_name": m["display_name"],
                "email": m["email"],
                "counts": counts,
            }

        report = []
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(_fetch_member_bugs, m): m for m in active_members}
            for fut in as_completed(futures):
                report.append(fut.result())

        report.sort(key=lambda r: r["counts"]["total"], reverse=True)

        totals = {"open": 0, "active": 0, "verify": 0, "closed": 0, "total": 0}
        for r in report:
            for k in totals:
                totals[k] += r["counts"][k]

        return jsonify({"members": report, "totals": totals, "group": group_name})
    except Exception as e:
        return jsonify({"error": str(e)}), 502


# ── Page routes ──────────────────────────────────────────────────────────────

@app.route("/login")
def login_page():
    return render_template_string(LOGIN_HTML)


@app.route("/")
def dashboard():
    sid = request.cookies.get("session_id")
    if not _get_session(sid):
        return redirect("/login")
    return render_template_string(DASHBOARD_HTML)


# ── Login page template ───────────────────────────────────────────────────────

LOGIN_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>QA Bug Dashboard — Login</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><rect width='100' height='100' rx='20' fill='%233b82f6'/><text x='50' y='68' text-anchor='middle' font-size='60' fill='white'>🪲</text></svg>">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Inter',sans-serif;background:#0f1117;color:#e2e8f0;min-height:100vh;display:flex;align-items:center;justify-content:center}
.login-card{background:#1a1d2e;border:1px solid #2d3148;border-radius:16px;padding:48px 40px;width:100%;max-width:420px;box-shadow:0 20px 60px rgba(0,0,0,.4)}
.login-card h1{font-size:24px;font-weight:700;margin-bottom:8px;text-align:center}
.login-card p.sub{font-size:14px;color:#8b92a5;text-align:center;margin-bottom:32px}
.form-group{margin-bottom:20px}
.form-group label{display:block;font-size:13px;font-weight:500;color:#a0a8c0;margin-bottom:6px}
.form-group input{width:100%;padding:12px 14px;background:#131520;border:1px solid #2d3148;border-radius:8px;color:#e2e8f0;font-size:14px;outline:none;transition:border-color .2s}
.form-group input:focus{border-color:#6366f1}
.form-group input::placeholder{color:#4a5068}
.login-btn{width:100%;padding:12px;background:#6366f1;color:#fff;border:none;border-radius:8px;font-size:15px;font-weight:600;cursor:pointer;transition:background .2s;margin-top:8px}
.login-btn:hover{background:#4f46e5}
.login-btn:disabled{opacity:.5;cursor:not-allowed}
.error-msg{color:#f87171;font-size:13px;text-align:center;margin-top:16px;min-height:20px}
.divider{display:flex;align-items:center;gap:12px;margin:20px 0 16px;color:#4a5068;font-size:12px}
.divider::before,.divider::after{content:'';flex:1;height:1px;background:#2d3148}
.viewer-btn{width:100%;padding:10px;background:transparent;color:#818cf8;border:1px solid #2d3148;border-radius:8px;font-size:13px;font-weight:500;cursor:pointer;transition:all .2s;font-family:inherit}
.viewer-btn:hover{border-color:#818cf8;background:#1e1e3a}
.viewer-btn:disabled{opacity:.5;cursor:not-allowed}
.viewer-note{font-size:11px;color:#4a5068;text-align:center;margin-top:6px}
.help-text{font-size:12px;color:#6b7280;margin-top:24px;text-align:center;line-height:1.6}
.help-text a{color:#818cf8;text-decoration:none}
.help-text a:hover{text-decoration:underline}
</style>
</head>
<body>
<div class="login-card">
  <div style="display:flex;justify-content:center;margin-bottom:20px">
    <div style="width:56px;height:56px;background:linear-gradient(135deg,#3b82f6,#8b5cf6);border-radius:16px;display:flex;align-items:center;justify-content:center;box-shadow:0 4px 16px rgba(59,130,246,0.3)">
      <svg width="30" height="30" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M8 2L6 4M16 2L18 4" stroke="white" stroke-width="2" stroke-linecap="round"/>
        <path d="M9 4h6a5 5 0 0 1 5 5v4a7 7 0 0 1-7 7h-2a7 7 0 0 1-7-7V9a5 5 0 0 1 5-5z" fill="white" fill-opacity="0.15" stroke="white" stroke-width="1.5"/>
        <path d="M12 4v16M8 10h8M8 14h8" stroke="white" stroke-width="1.5" stroke-linecap="round"/>
        <path d="M3 9h2M19 9h2M3 14h2M19 14h2" stroke="white" stroke-width="2" stroke-linecap="round"/>
      </svg>
    </div>
  </div>
  <h1>QA Bug Dashboard</h1>
  <p class="sub">Sign in with your Jira credentials</p>
  <form id="loginForm">
    <div class="form-group">
      <label for="email">Jira Email</label>
      <input type="email" id="email" placeholder="you@drivenets.com" required autocomplete="email">
    </div>
    <div class="form-group">
      <label for="token">API Token</label>
      <input type="password" id="token" placeholder="Your Jira API token" autocomplete="current-password">
    </div>
    <button type="submit" class="login-btn" id="loginBtn">Sign In</button>
  </form>
  <div class="error-msg" id="errorMsg"></div>
  <div class="divider">or</div>
  <button class="viewer-btn" id="viewerBtn" onclick="viewerLogin()">Continue with email only (view-only)</button>
  <div class="viewer-note">View your bugs without an API token — comments disabled</div>
  <div class="help-text">
    Need an API token for full access?<br>
    <a href="https://id.atlassian.com/manage-profile/security/api-tokens" target="_blank">Create one at Atlassian</a>
  </div>
</div>
<script>
const form = document.getElementById('loginForm');
const btn = document.getElementById('loginBtn');
const errEl = document.getElementById('errorMsg');

async function doLogin(email, token) {
  errEl.textContent = '';
  try {
    const res = await fetch('/api/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({email, token: token || ''}),
    });
    const data = await res.json();
    if (res.ok) {
      window.location.href = '/';
    } else {
      errEl.textContent = data.error || 'Login failed';
    }
  } catch (err) {
    errEl.textContent = 'Network error — try again';
  }
}

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  const email = document.getElementById('email').value.trim();
  const token = document.getElementById('token').value.trim();
  if (!email) { errEl.textContent = 'Email is required'; return; }
  if (!token) { errEl.textContent = 'Token is required for full sign-in. Use "email only" for view-only access.'; return; }
  btn.disabled = true; btn.textContent = 'Signing in…';
  await doLogin(email, token);
  btn.disabled = false; btn.textContent = 'Sign In';
});

async function viewerLogin() {
  const email = document.getElementById('email').value.trim();
  if (!email) { errEl.textContent = 'Enter your Jira email first'; return; }
  const vb = document.getElementById('viewerBtn');
  vb.disabled = true; vb.textContent = 'Signing in…';
  await doLogin(email, '');
  vb.disabled = false; vb.textContent = 'Continue with email only (view-only)';
}
</script>
</body>
</html>
"""

# ── Dashboard template ────────────────────────────────────────────────────────

DASHBOARD_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bug Tracker</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><rect width='100' height='100' rx='20' fill='%233b82f6'/><text x='50' y='68' text-anchor='middle' font-size='60' fill='white'>🪲</text></svg>">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #0a0c10;
  --surface: #13161d;
  --surface2: #1a1e28;
  --surface3: #222838;
  --border: #262d3d;
  --border-hover: #3a4460;
  --text: #e4e8f1;
  --text-secondary: #9098b0;
  --text-muted: #5c6480;
  --blue: #5b8af5;
  --blue-dim: #2a3f6e;
  --green: #3dd68c;
  --green-dim: #1a3d2e;
  --yellow: #f0c541;
  --yellow-dim: #3d3520;
  --orange: #f08c41;
  --red: #ef6461;
  --red-dim: #3d1f20;
  --purple: #a684f5;
  --purple-dim: #2a2040;
  --teal: #41d9c0;
  --teal-dim: #1a3534;
  --radius: 10px;
  --radius-sm: 6px;
}
* { margin:0; padding:0; box-sizing:border-box; }
body {
  font-family: 'DM Sans', -apple-system, BlinkMacSystemFont, sans-serif;
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
  line-height: 1.55;
}

/* ── Header ─────────────────────────────────────── */
.header {
  position: sticky; top: 0; z-index: 100;
  background: rgba(10,12,16,0.82);
  backdrop-filter: blur(20px) saturate(1.4);
  -webkit-backdrop-filter: blur(20px) saturate(1.4);
  border-bottom: 1px solid var(--border);
  padding: 0 32px;
  height: 56px;
  display: flex; align-items: center; justify-content: space-between;
}
.header-left { display:flex; align-items:center; gap:14px; }
.logo {
  width: 34px; height: 34px;
  background: linear-gradient(135deg, #3b82f6, #8b5cf6);
  border-radius: 10px;
  display: flex; align-items: center; justify-content: center;
  box-shadow: 0 2px 8px rgba(59,130,246,0.3);
}
.header h1 { font-size:16px; font-weight:600; color:var(--text); letter-spacing:-0.3px; }
.header h1 span { color:var(--text-muted); font-weight:400; }
.header-right { display:flex; align-items:center; gap:12px; }
.header .meta { font-size:12px; color:var(--text-muted); }
.search-box {
  background: var(--surface2); border: 1px solid var(--border); border-radius: var(--radius-sm);
  padding: 6px 12px; font-size: 13px; color: var(--text); font-family: inherit;
  width: 220px; outline: none; transition: border-color 0.2s;
}
.search-box:focus { border-color: var(--blue); }
.search-box::placeholder { color: var(--text-muted); }
.btn {
  padding: 6px 16px; font-size: 13px; font-family: inherit; font-weight: 500;
  border-radius: var(--radius-sm); cursor: pointer; transition: all 0.15s;
  border: 1px solid var(--border); background: var(--surface2); color: var(--text-secondary);
}
.btn:hover { border-color: var(--border-hover); color: var(--text); background: var(--surface3); }
.btn:disabled { opacity:0.35; cursor:wait; }
.btn-primary { background:var(--blue-dim); border-color:var(--blue); color:#fff; }
.btn-primary:hover { background:var(--blue); }
.user-pill {
  display:flex; align-items:center; gap:8px;
  background:var(--surface2); border:1px solid var(--border); border-radius:20px;
  padding:4px 14px 4px 10px; font-size:12px; color:var(--text-secondary);
}
.user-pill .avatar {
  width:24px; height:24px; border-radius:50%;
  background:linear-gradient(135deg,var(--purple),var(--blue));
  display:flex; align-items:center; justify-content:center;
  font-size:11px; font-weight:700; color:#fff;
}
.user-pill .uname { max-width:140px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.btn-logout {
  padding:4px 12px; font-size:11px; background:transparent;
  border:1px solid var(--border); border-radius:var(--radius-sm);
  color:var(--text-muted); cursor:pointer; font-family:inherit; transition:all .15s;
}
.btn-logout:hover { border-color:var(--red); color:var(--red); }

/* ── Stats ──────────────────────────────────────── */
.stats-row {
  display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px;
  padding: 20px 32px 0;
}
.stat-card {
  background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 16px 20px; transition: border-color 0.2s; cursor: pointer;
}
.stat-card:hover { border-color: var(--border-hover); }
.stat-card.active { border-color: var(--blue); background: var(--surface2); }
.stat-card .stat-val { font-size: 28px; font-weight: 700; letter-spacing: -1px; line-height: 1.1; }
.stat-card .stat-label { font-size: 12px; color: var(--text-muted); margin-top: 4px; font-weight: 500; }
.stat-open .stat-val { color: var(--blue); }
.stat-active .stat-val { color: var(--yellow); }
.stat-verify .stat-val { color: var(--orange); }
.stat-verify { border-color: #5e3a15; }
.stat-verify.active { border-color: var(--orange); background: #1e1510; }
.stat-closed .stat-val { color: var(--green); }
.stat-all .stat-val { color: var(--text-secondary); }

/* ── Verification queue ─────────────────────────── */
.verify-section { padding: 16px 32px 0; }
.verify-banner {
  background: linear-gradient(135deg, #1e1510 0%, #1a1208 100%);
  border: 1px solid #5e3a15; border-radius: var(--radius);
  padding: 20px 24px; margin-bottom: 4px;
}
.verify-banner-header {
  display:flex; align-items:center; justify-content:space-between; margin-bottom:14px;
}
.verify-banner-title {
  font-size:14px; font-weight:700; color:var(--orange);
  display:flex; align-items:center; gap:8px;
}
.verify-banner-title svg { width:18px; height:18px; }
.verify-count {
  font-size:11px; font-weight:600; padding:2px 10px; border-radius:20px;
  background:rgba(240,140,65,0.15); color:var(--orange);
}
.verify-cards { display:grid; grid-template-columns:repeat(auto-fill, minmax(340px, 1fr)); gap:10px; }
.verify-card {
  background:var(--surface2); border:1px solid var(--border); border-radius:var(--radius-sm);
  padding:14px 16px; display:flex; align-items:flex-start; gap:14px;
  transition: border-color 0.15s;
}
.verify-card:hover { border-color: var(--border-hover); }
.verify-card-body { flex:1; min-width:0; }
.verify-card-top {
  display:flex; align-items:center; gap:8px; margin-bottom:4px;
}
.verify-card-key {
  font-family:'JetBrains Mono',monospace; font-size:12px; font-weight:600;
  color:var(--orange); text-decoration:none;
}
.verify-card-key:hover { text-decoration:underline; }
.verify-card-summary { font-size:13px; color:var(--text); line-height:1.4; margin-bottom:6px;
  display:-webkit-box; -webkit-line-clamp:1; -webkit-box-orient:vertical; overflow:hidden; }
.verify-card-meta { font-size:11px; color:var(--text-muted); display:flex; gap:12px; flex-wrap:wrap; }
.verify-card-meta span { display:flex; align-items:center; gap:4px; }
.verify-card-actions { display:flex; flex-direction:column; gap:4px; flex-shrink:0; }
.verify-empty { color:var(--text-muted); font-size:13px; text-align:center; padding:12px 0; }
.verify-card.selected { border-color:var(--orange); background:#1e1510; }
.verify-detail-panel {
  display:none; margin-top:12px; background:var(--surface2); border:1px solid var(--orange);
  border-radius:var(--radius); padding:20px 24px; animation: slideDown 0.2s ease;
}
.verify-detail-panel.open { display:block; }
.verify-detail-header {
  display:flex; align-items:center; justify-content:space-between; margin-bottom:16px;
  padding-bottom:12px; border-bottom:1px solid var(--border);
}
.verify-detail-header h3 {
  font-size:13px; font-weight:600; color:var(--orange);
  font-family:'JetBrains Mono',monospace;
}
.verify-detail-close {
  background:none; border:none; color:var(--text-muted); cursor:pointer;
  font-size:18px; line-height:1; padding:4px 8px; border-radius:4px;
}
.verify-detail-close:hover { color:var(--text); background:var(--surface3); }

/* ── Table ──────────────────────────────────────── */
.table-wrap { padding: 16px 32px 32px; }
.bug-table { width:100%; border-collapse:separate; border-spacing:0; }
.bug-table thead th {
  text-align:left; font-size:11px; font-weight:600; text-transform:uppercase;
  letter-spacing:0.6px; color:var(--text-muted); padding:10px 14px;
  border-bottom:1px solid var(--border);
  position:sticky; top:56px; background:var(--bg); z-index:10;
}
.bug-table tbody td {
  padding: 12px 14px; font-size: 13px; border-bottom: 1px solid var(--border);
  vertical-align: middle;
}
.bug-table tbody tr { transition: background 0.12s; }
.bug-table tbody tr:hover { background: var(--surface); }
.bug-table tbody tr.expanded { background: var(--surface2); }
.bug-table tbody tr.focused { outline: 1px solid var(--blue); outline-offset: -1px; background: var(--surface); }

.key-link {
  font-family: 'JetBrains Mono', monospace; font-size: 12px; font-weight: 500;
  color: var(--blue); text-decoration: none;
}
.key-link:hover { text-decoration: underline; }
.summary-cell { max-width:400px; }
.summary-text {
  display:-webkit-box; -webkit-line-clamp:1; -webkit-box-orient:vertical;
  overflow:hidden; line-height:1.4;
}

.badge {
  display:inline-flex; align-items:center; gap:5px;
  padding:3px 10px; font-size:11px; font-weight:600; border-radius:20px;
  white-space:nowrap; letter-spacing:0.2px;
}
.badge::before { content:''; width:6px; height:6px; border-radius:50%; }
.badge-open { background:#172554; color:#93c5fd; } .badge-open::before { background:#3b82f6; }
.badge-investigation { background:var(--yellow-dim); color:var(--yellow); } .badge-investigation::before { background:var(--yellow); }
.badge-progress { background:#1a2e1a; color:#86efac; } .badge-progress::before { background:#22c55e; }
.badge-review { background:var(--purple-dim); color:var(--purple); } .badge-review::before { background:var(--purple); }
.badge-merge { background:var(--teal-dim); color:var(--teal); } .badge-merge::before { background:var(--teal); }
.badge-closed { background:var(--green-dim); color:var(--green); } .badge-closed::before { background:var(--green); }
.badge-verified { background:var(--green-dim); color:var(--green); border:1px solid #155e3e; } .badge-verified::before { background:var(--green); }
.badge-cancelled { background:var(--red-dim); color:var(--red); } .badge-cancelled::before { background:var(--red); }

.priority-highest,.priority-high { color:var(--red); font-weight:600; }
.priority-medium { color:var(--yellow); }
.priority-low,.priority-lowest { color:var(--text-muted); }

.branch-link {
  font-family:'JetBrains Mono',monospace; font-size:11px; color:var(--teal);
  text-decoration:none; max-width:140px; display:inline-block;
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap; vertical-align:middle;
}
.branch-link:hover { text-decoration:underline; }
.fix-tag {
  font-family:'JetBrains Mono',monospace; font-size:10px; font-weight:500;
  padding:2px 8px; background:var(--surface3); border:1px solid var(--border);
  border-radius:20px; color:var(--text-secondary);
}
.updated-cell { font-size:12px; color:var(--text-muted); white-space:nowrap; }
.actions-cell { display:flex; gap:5px; align-items:center; }
.btn-sm {
  padding:4px 12px; font-size:11px; font-weight:500; font-family:inherit;
  border-radius:var(--radius-sm); cursor:pointer; transition:all 0.15s; border:1px solid var(--border);
  background:transparent; color:var(--text-secondary);
}
.btn-sm:hover { border-color:var(--border-hover); background:var(--surface3); color:var(--text); }
.btn-build { border-color:#155e3e; color:var(--green); }
.btn-build:hover { background:var(--green-dim); border-color:var(--green); }
.btn-build:disabled { opacity:0.35; cursor:wait; }
.btn-build.done { background:var(--green-dim); border-color:var(--green); }

/* ── Detail panel ───────────────────────────────── */
.detail-tr td { padding:0!important; border-bottom:none!important; }
.detail-panel {
  display:none; background:var(--surface); border-bottom:2px solid var(--blue-dim);
  animation: slideDown 0.2s ease;
}
.detail-panel.open { display:block; }
@keyframes slideDown { from{opacity:0;transform:translateY(-8px)} to{opacity:1;transform:translateY(0)} }
.detail-inner { padding:20px 28px 24px; }
.detail-grid { display:grid; grid-template-columns:1fr 1fr; gap:20px; }
.detail-card {
  background:var(--surface2); border:1px solid var(--border); border-radius:var(--radius);
  padding:16px; overflow:hidden;
}
.detail-card h4 {
  font-size:11px; font-weight:600; text-transform:uppercase; letter-spacing:0.5px;
  color:var(--text-muted); margin-bottom:10px; display:flex; align-items:center; gap:6px;
}
.detail-card h4 svg { width:14px; height:14px; opacity:0.6; }
.detail-card pre {
  font-family:'JetBrains Mono',monospace; font-size:12px; color:var(--text);
  white-space:pre-wrap; word-break:break-word; line-height:1.65;
  background:var(--surface); border:1px solid var(--border); border-radius:var(--radius-sm);
  padding:14px; max-height:280px; overflow-y:auto;
}
.detail-card a { color:var(--blue); text-decoration:none; font-size:13px; }
.detail-card a:hover { text-decoration:underline; }
.detail-loading { color:var(--text-muted); font-size:12px; padding:16px; }
.comment-item {
  padding:12px; background:var(--surface); border:1px solid var(--border);
  border-radius:var(--radius-sm); margin-bottom:8px;
}
.comment-item:last-child { margin-bottom:0; }
.comment-who { font-size:11px; font-weight:600; color:var(--purple); margin-bottom:4px; }
.comment-text { font-size:12px; white-space:pre-wrap; word-break:break-word; color:var(--text); line-height:1.5; }
.ctx-badge {
  display:inline-block; font-size:10px; font-weight:600; padding:2px 8px;
  border-radius:20px; background:var(--yellow-dim); color:var(--yellow);
  margin-bottom:8px; letter-spacing:0.3px;
}
.muted { color:var(--text-muted); font-size:12px; font-style:italic; }

/* ── Topology diagram ──────────────────────────── */
.topo-card {
  background:var(--surface2); border:1px solid var(--border); border-radius:var(--radius);
  padding:16px; margin-bottom:16px;
}
.topo-card h4 {
  font-size:11px; font-weight:600; text-transform:uppercase; letter-spacing:0.5px;
  color:var(--text-muted); margin-bottom:12px; display:flex; align-items:center; gap:6px;
}
.topo-card h4 svg { width:14px; height:14px; opacity:0.6; }
.topo-svg { width:100%; display:block; }

.loading-state { text-align:center; padding:80px 0; color:var(--text-muted); font-size:14px; }
.spinner {
  display:inline-block; width:24px; height:24px; border:2.5px solid var(--border);
  border-top-color:var(--blue); border-radius:50%; animation:spin 0.7s linear infinite;
  margin-bottom:14px;
}
@keyframes spin { to{transform:rotate(360deg)} }
.empty-state { text-align:center; padding:60px 0; color:var(--text-muted); font-size:14px; }

/* ── Sortable columns ─────────────────────────── */
.bug-table thead th.sortable { cursor:pointer; user-select:none; }
.bug-table thead th.sortable:hover { color:var(--text); }
.sort-arrow { font-size:9px; margin-left:2px; opacity:0.5; white-space:nowrap; }
.sort-arrow.active { opacity:1; color:var(--blue); }
.bug-table thead th.sortable { white-space:nowrap; }

/* ── Build status dot ─────────────────────────── */
.build-dot { display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:5px; vertical-align:middle; }
.build-dot.success { background:var(--green); }
.build-dot.failure { background:var(--red); }
.build-dot.building { background:var(--yellow); animation: pulse 1s ease infinite; }
.build-dot.unknown { background:var(--text-muted); }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }

/* ── Bug age ──────────────────────────────────── */
.age-stale { color:var(--red); font-weight:600; }
.age-warn { color:var(--orange); }
.age-ok { color:var(--text-muted); }

/* ── SSH links ────────────────────────────────── */
.ssh-link {
  font-family:'JetBrains Mono',monospace; font-size:11px; color:var(--teal);
  text-decoration:none; display:inline-flex; align-items:center; gap:4px;
  padding:3px 8px; background:var(--teal-dim); border:1px solid #1a4540;
  border-radius:var(--radius-sm); margin-right:4px; margin-bottom:4px;
}
.ssh-link:hover { border-color:var(--teal); background:#1a3d38; }
.ssh-links-row { display:flex; flex-wrap:wrap; margin-bottom:10px; }

/* ── Linked bugs ──────────────────────────────── */
.linked-item {
  display:flex; align-items:center; gap:8px; padding:6px 0;
  border-bottom:1px solid var(--border); font-size:12px;
}
.linked-item:last-child { border-bottom:none; }
.linked-rel { color:var(--text-muted); font-size:10px; min-width:70px; }
.linked-key { font-family:'JetBrains Mono',monospace; color:var(--blue); text-decoration:none; font-weight:500; }
.linked-key:hover { text-decoration:underline; }
.linked-summary { color:var(--text-secondary); flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }

/* ── Copy button ──────────────────────────────── */
.copy-btn {
  background:none; border:1px solid var(--border); color:var(--text-muted);
  cursor:pointer; padding:3px 8px; border-radius:4px; font-size:10px;
  font-family:inherit; margin-left:auto; transition:all 0.15s;
}
.copy-btn:hover { border-color:var(--border-hover); color:var(--text); }
.copy-btn.copied { border-color:var(--green); color:var(--green); }

/* ── Comment form ─────────────────────────────── */
.comment-form { margin-top:12px; display:flex; gap:8px; }
.comment-input {
  flex:1; background:var(--surface); border:1px solid var(--border); border-radius:var(--radius-sm);
  padding:8px 12px; font-size:12px; color:var(--text); font-family:inherit;
  resize:none; outline:none; min-height:36px;
}
.comment-input:focus { border-color:var(--blue); }
.comment-input::placeholder { color:var(--text-muted); }

/* ── Countdown ────────────────────────────────── */
.countdown { font-size:11px; color:var(--text-muted); font-family:'JetBrains Mono',monospace; }

/* ── Toast notification ───────────────────────── */
.toast {
  position:fixed; bottom:24px; right:24px; z-index:999;
  background:var(--surface2); border:1px solid var(--green); border-radius:var(--radius);
  padding:12px 20px; color:var(--green); font-size:13px; font-weight:500;
  box-shadow:0 8px 30px rgba(0,0,0,0.4); animation: toastIn 0.3s ease, toastOut 0.3s ease 2.7s forwards;
}
@keyframes toastIn { from{opacity:0;transform:translateY(20px)} to{opacity:1;transform:translateY(0)} }
@keyframes toastOut { from{opacity:1} to{opacity:0} }

/* ── Keyboard hint ────────────────────────────── */
.kbd-hint {
  position:fixed; bottom:24px; left:24px; z-index:50;
  font-size:10px; color:var(--text-muted); background:var(--surface);
  border:1px solid var(--border); border-radius:var(--radius-sm);
  padding:6px 10px; display:flex; gap:12px;
}
.kbd-hint kbd {
  background:var(--surface3); border:1px solid var(--border); border-radius:3px;
  padding:1px 5px; font-family:'JetBrains Mono',monospace; font-size:10px;
}

/* ── Export button ────────────────────────────── */
.btn-export { font-size:12px; padding:5px 12px; }

/* ── Toolbar row ─────────────────────────────── */
.toolbar {
  display:flex; align-items:center; gap:10px; padding:12px 32px 0; flex-wrap:wrap;
}
.toolbar select, .toolbar .tool-btn {
  background:var(--surface2); border:1px solid var(--border); border-radius:var(--radius-sm);
  padding:5px 10px; font-size:12px; color:var(--text-secondary); font-family:inherit;
  cursor:pointer; outline:none; transition:border-color .15s;
}
.toolbar select:hover, .toolbar .tool-btn:hover { border-color:var(--border-hover); color:var(--text); }
.toolbar select:focus { border-color:var(--blue); }
.toolbar .tool-btn.active { border-color:var(--blue); color:var(--blue); background:var(--blue-dim); }
.toolbar .tool-btn svg { width:14px; height:14px; vertical-align:-2px; margin-right:4px; }
.toolbar .spacer { flex:1; }
.bulk-bar {
  display:none; align-items:center; gap:10px; padding:8px 32px;
  background:var(--surface2); border-bottom:1px solid var(--border); font-size:13px;
}
.bulk-bar.visible { display:flex; }
.bulk-bar .count { font-weight:600; color:var(--blue); }
.bulk-bar .btn-sm { padding:5px 14px; }
.bulk-cb { width:15px; height:15px; accent-color:var(--blue); cursor:pointer; }

/* ── Activity feed ───────────────────────────── */
.feed-section { padding:12px 32px 0; }
.feed-bar {
  background:var(--surface); border:1px solid var(--border); border-radius:var(--radius);
  padding:10px 16px; display:flex; align-items:center; gap:14px; overflow-x:auto;
  scrollbar-width:none;
}
.feed-bar::-webkit-scrollbar { display:none; }
.feed-item {
  display:flex; align-items:center; gap:6px; font-size:11px; white-space:nowrap;
  color:var(--text-secondary); flex-shrink:0;
}
.feed-item .feed-key { font-family:'JetBrains Mono',monospace; color:var(--blue); font-weight:500; }
.feed-item .feed-arrow { color:var(--text-muted); }
.feed-item .feed-time { color:var(--text-muted); font-size:10px; }

/* ── Team overview ───────────────────────────── */
.team-overlay {
  display:none; position:fixed; inset:0; z-index:200;
  background:rgba(0,0,0,0.6); backdrop-filter:blur(4px);
  align-items:center; justify-content:center;
}
.team-overlay.open { display:flex; }
.team-modal {
  background:var(--surface); border:1px solid var(--border); border-radius:12px;
  padding:28px; width:90%; max-width:700px; max-height:80vh; overflow-y:auto;
  box-shadow:0 20px 60px rgba(0,0,0,.5);
}
.team-modal h2 { font-size:18px; font-weight:700; margin-bottom:16px; display:flex; align-items:center; gap:10px; }
.team-modal .close-btn { margin-left:auto; background:none; border:none; color:var(--text-muted); cursor:pointer; font-size:22px; }
.team-modal .close-btn:hover { color:var(--text); }
.team-grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(200px, 1fr)); gap:12px; }
.team-card {
  background:var(--surface2); border:1px solid var(--border); border-radius:var(--radius);
  padding:14px; text-align:center;
}
.team-card .t-name { font-size:14px; font-weight:600; margin-bottom:8px; }
.team-card .t-counts { display:flex; justify-content:center; gap:12px; font-size:11px; color:var(--text-muted); }
.team-card .t-counts span { display:flex; flex-direction:column; align-items:center; }
.team-card .t-counts .num { font-size:20px; font-weight:700; color:var(--text); }
.team-card .t-counts .num.c-open { color:var(--blue); }
.team-card .t-counts .num.c-active { color:var(--yellow); }
.team-card .t-counts .num.c-verify { color:var(--orange); }

/* ── Team lead report ────────────────────────── */
.team-tabs { display:flex; gap:0; margin-bottom:16px; border-bottom:2px solid var(--border); }
.team-tab {
  padding:8px 20px; font-size:13px; font-weight:500; cursor:pointer;
  border-bottom:2px solid transparent; margin-bottom:-2px; color:var(--text-muted);
  background:none; border-top:none; border-left:none; border-right:none; font-family:inherit;
  transition:all .15s;
}
.team-tab:hover { color:var(--text); }
.team-tab.active { color:var(--blue); border-bottom-color:var(--blue); }
.team-group-picker {
  display:flex; align-items:center; gap:10px; margin-bottom:16px;
}
.team-group-picker .group-search-wrap {
  flex:1; position:relative;
}
.team-group-picker .group-search-input {
  width:100%; background:var(--surface2); border:1px solid var(--border);
  border-radius:var(--radius-sm); padding:8px 12px; font-size:13px;
  color:var(--text); font-family:inherit; outline:none;
}
.team-group-picker .group-search-input:focus { border-color:var(--blue); }
.team-group-picker .group-search-input::placeholder { color:var(--text-muted); }
.group-dropdown {
  display:none; position:absolute; top:100%; left:0; right:0; z-index:10;
  background:var(--surface); border:1px solid var(--border); border-radius:var(--radius-sm);
  max-height:220px; overflow-y:auto; margin-top:4px;
  box-shadow:0 8px 24px rgba(0,0,0,0.2);
}
.group-dropdown.open { display:block; }
.group-dropdown .gd-item {
  padding:8px 12px; font-size:13px; cursor:pointer; transition:background .1s;
  color:var(--text);
}
.group-dropdown .gd-item:hover { background:var(--surface2); }
.group-dropdown .gd-item.selected { background:var(--blue-dim); color:var(--blue); font-weight:600; }
.group-dropdown .gd-empty { padding:12px; font-size:12px; color:var(--text-muted); text-align:center; }
.team-group-picker button {
  padding:8px 18px; font-size:13px; font-weight:500; font-family:inherit;
  border-radius:var(--radius-sm); cursor:pointer; transition:all .15s;
  background:var(--blue-dim); border:1px solid var(--blue); color:var(--blue);
}
.team-group-picker button:hover { background:var(--blue); color:#fff; }
.team-group-picker button:disabled { opacity:.4; cursor:wait; }
.team-report-table { width:100%; border-collapse:separate; border-spacing:0; margin-top:8px; }
.team-report-table th {
  text-align:left; font-size:11px; font-weight:600; text-transform:uppercase;
  letter-spacing:.5px; color:var(--text-muted); padding:8px 12px;
  border-bottom:1px solid var(--border);
}
.team-report-table th.num-col { text-align:center; width:70px; }
.team-report-table td { padding:10px 12px; font-size:13px; border-bottom:1px solid var(--border); }
.team-report-table td.num-cell { text-align:center; font-weight:600; font-family:'JetBrains Mono',monospace; }
.team-report-table tr.totals-row td { font-weight:700; border-top:2px solid var(--border); background:var(--surface2); }
.team-report-table tbody tr:hover { background:var(--surface2); }
.team-report-progress {
  display:flex; height:6px; border-radius:3px; overflow:hidden; background:var(--surface);
  width:100%; min-width:80px;
}
.team-report-progress .seg { height:100%; transition:width .3s; }
.team-report-progress .seg-open { background:var(--blue); }
.team-report-progress .seg-active { background:var(--yellow); }
.team-report-progress .seg-verify { background:var(--orange); }
.team-report-progress .seg-closed { background:var(--green); }
.team-totals {
  display:flex; gap:20px; padding:14px 0; margin-bottom:12px; border-bottom:1px solid var(--border);
}
.team-totals .tt { text-align:center; }
.team-totals .tt .num { font-size:24px; font-weight:700; }
.team-totals .tt .lbl { font-size:10px; color:var(--text-muted); margin-top:2px; }

/* ── Verification checklist ──────────────────── */
.checklist { margin-top:12px; }
.checklist label {
  display:flex; align-items:center; gap:8px; padding:6px 0; font-size:12px;
  color:var(--text-secondary); cursor:pointer; border-bottom:1px solid var(--border);
}
.checklist label:last-child { border-bottom:none; }
.checklist input[type=checkbox] { accent-color:var(--green); width:15px; height:15px; cursor:pointer; }
.checklist label.done { color:var(--text-muted); text-decoration:line-through; }

/* ── Personal notes ──────────────────────────── */
.notes-area {
  width:100%; min-height:60px; background:var(--surface); border:1px solid var(--border);
  border-radius:var(--radius-sm); padding:8px 12px; font-size:12px; color:var(--text);
  font-family:inherit; resize:vertical; outline:none; margin-top:8px;
}
.notes-area:focus { border-color:var(--blue); }
.notes-area::placeholder { color:var(--text-muted); }

/* ── Pinned star ─────────────────────────────── */
.pin-star {
  cursor:pointer; font-size:14px; color:var(--text-muted); background:none;
  border:none; padding:2px; transition:color .15s;
}
.pin-star:hover { color:var(--yellow); }
.pin-star.pinned { color:var(--yellow); }

/* ── Version group headers ───────────────────── */
.group-header td {
  padding:10px 14px!important; font-size:12px; font-weight:700; color:var(--text-secondary);
  background:var(--surface2); border-bottom:1px solid var(--border)!important;
  letter-spacing:0.3px;
}

/* ── Stats chart ─────────────────────────────── */
.stats-section { padding:12px 32px 0; }
.stats-panel {
  background:var(--surface); border:1px solid var(--border); border-radius:var(--radius);
  padding:16px 20px;
}
.stats-panel h3 { font-size:13px; font-weight:600; color:var(--text-secondary); margin-bottom:12px; }
.stats-row-inner { display:flex; gap:24px; flex-wrap:wrap; }
.stat-mini { text-align:center; }
.stat-mini .num { font-size:22px; font-weight:700; }
.stat-mini .lbl { font-size:10px; color:var(--text-muted); margin-top:2px; }
.bar-chart { display:flex; align-items:flex-end; gap:3px; height:60px; margin-top:8px; }
.bar-chart .bar {
  flex:1; min-width:3px; border-radius:2px 2px 0 0; transition:height .3s;
  background:var(--blue); opacity:0.7;
}
.bar-chart .bar:hover { opacity:1; }

/* ── Compact mode ────────────────────────────── */
body.compact .bug-table tbody td { padding:6px 14px; font-size:12px; }
body.compact .bug-table thead th { padding:6px 14px; }
body.compact .badge { padding:2px 8px; font-size:10px; }
body.compact .summary-text { font-size:12px; }

/* ── Light theme ─────────────────────────────── */
body.light {
  --bg: #f0f2f5;
  --surface: #ffffff;
  --surface2: #f7f8fa;
  --surface3: #eceef2;
  --border: #dce0e8;
  --border-hover: #b8c0d0;
  --text: #1c2030;
  --text-secondary: #505870;
  --text-muted: #8892a8;
  --blue: #2563eb;
  --blue-dim: #eff4ff;
  --green: #16a34a;
  --green-dim: #ecfdf3;
  --yellow: #b45309;
  --yellow-dim: #fefce8;
  --orange: #c2410c;
  --red: #dc2626;
  --red-dim: #fef2f2;
  --purple: #7c3aed;
  --purple-dim: #f5f0ff;
  --teal: #0f766e;
  --teal-dim: #f0fdfa;
}
body.light .header {
  background:rgba(255,255,255,0.88);
  border-bottom-color:#dce0e8;
  box-shadow:0 1px 3px rgba(0,0,0,0.04);
}
body.light .logo { box-shadow:0 2px 6px rgba(37,99,235,0.2); }
body.light .stat-card {
  box-shadow:0 1px 3px rgba(0,0,0,0.04), 0 0 0 1px rgba(0,0,0,0.03);
  border-color:transparent;
}
body.light .stat-card:hover { box-shadow:0 2px 8px rgba(0,0,0,0.07); border-color:var(--border); }
body.light .stat-card.active { box-shadow:0 0 0 2px var(--blue); border-color:transparent; }
body.light .stat-verify { border-color:transparent; box-shadow:0 1px 3px rgba(0,0,0,0.04), inset 0 0 0 1px rgba(194,65,12,0.15); }
body.light .stat-verify.active { box-shadow:0 0 0 2px var(--orange); }
body.light .search-box { background:#fff; border-color:#dce0e8; }
body.light .search-box:focus { border-color:var(--blue); box-shadow:0 0 0 3px rgba(37,99,235,0.1); }
body.light .btn { background:#fff; border-color:#dce0e8; color:var(--text-secondary); }
body.light .btn:hover { background:var(--surface3); border-color:var(--border-hover); }
body.light .btn-primary { background:var(--blue); border-color:var(--blue); color:#fff; }
body.light .btn-primary:hover { background:#1d4ed8; }
body.light .user-pill { background:#fff; border-color:#dce0e8; }
body.light .btn-logout { border-color:#dce0e8; }
body.light .verify-banner {
  background:linear-gradient(135deg, #fff7ed 0%, #fffbeb 100%);
  border-color:#fed7aa;
  box-shadow:0 1px 4px rgba(194,65,12,0.06);
}
body.light .verify-card { background:#fff; border-color:#fde0c8; }
body.light .verify-card:hover { border-color:#fdba74; box-shadow:0 2px 6px rgba(194,65,12,0.06); }
body.light .verify-card.selected { background:#fff7ed; border-color:var(--orange); }
body.light .verify-detail-panel { background:#fff; border-color:var(--orange); }
body.light .bug-table thead th { background:var(--bg); border-bottom-color:#d1d5de; }
body.light .bug-table tbody tr:hover { background:#f8f9fc; }
body.light .bug-table tbody tr.expanded { background:#f0f4ff; }
body.light .bug-table tbody tr.focused { outline-color:var(--blue); background:#f0f4ff; }
body.light .bug-table tbody td { border-bottom-color:#eceef2; }
body.light .detail-panel { background:#fff; border-bottom-color:#dce0e8; }
body.light .detail-card { background:var(--surface2); border-color:#e4e8ee; }
body.light .detail-card pre { background:#fff; border-color:#e4e8ee; }
body.light .comment-item { background:#fff; border-color:#e4e8ee; }
body.light .topo-card { background:var(--surface2); border-color:#e4e8ee; }
body.light .badge-open { background:#eff6ff; color:#1d4ed8; } body.light .badge-open::before { background:#3b82f6; }
body.light .badge-investigation { background:#fefce8; color:#92400e; } body.light .badge-investigation::before { background:#f59e0b; }
body.light .badge-progress { background:#ecfdf5; color:#065f46; } body.light .badge-progress::before { background:#10b981; }
body.light .badge-review { background:#f5f3ff; color:#5b21b6; } body.light .badge-review::before { background:#8b5cf6; }
body.light .badge-merge { background:#f0fdfa; color:#115e59; } body.light .badge-merge::before { background:#14b8a6; }
body.light .badge-closed { background:#ecfdf5; color:#065f46; } body.light .badge-closed::before { background:#16a34a; }
body.light .badge-verified { background:#ecfdf5; color:#065f46; border-color:#86efac; } body.light .badge-verified::before { background:#16a34a; }
body.light .badge-cancelled { background:#fef2f2; color:#991b1b; } body.light .badge-cancelled::before { background:#ef4444; }
body.light .key-link { color:#2563eb; }
body.light .branch-link { color:#0f766e; }
body.light .fix-tag { background:#f0f4ff; border-color:#dbeafe; color:#1e40af; }
body.light .toolbar select, body.light .toolbar .tool-btn { background:#fff; border-color:#dce0e8; }
body.light .toolbar .tool-btn.active { background:#eff4ff; border-color:var(--blue); }
body.light .feed-bar { background:#fff; border-color:#dce0e8; box-shadow:0 1px 3px rgba(0,0,0,0.03); }
body.light .team-modal { background:#fff; box-shadow:0 20px 60px rgba(0,0,0,.12); }
body.light .team-card { background:var(--surface2); }
body.light .stats-panel { background:#fff; box-shadow:0 1px 3px rgba(0,0,0,0.04); }
body.light .bar-chart .bar { background:var(--blue); opacity:0.6; }
body.light .bar-chart .bar:hover { opacity:0.9; }
body.light .toast { background:#fff; border-color:var(--green); color:#065f46; box-shadow:0 8px 30px rgba(0,0,0,.1); }
body.light .kbd-hint { background:#fff; border-color:#dce0e8; }
body.light .kbd-hint kbd { background:var(--surface3); border-color:#d1d5de; }
body.light .notes-area { background:#fff; border-color:#dce0e8; }
body.light .comment-input { background:#fff; border-color:#dce0e8; }
body.light .btn-sm { border-color:#dce0e8; }
body.light .btn-sm:hover { background:#f0f4ff; border-color:var(--border-hover); }
body.light .btn-build { border-color:#86efac; color:#065f46; }
body.light .btn-build:hover { background:#ecfdf5; border-color:#16a34a; }
body.light .group-header td { background:#f0f4ff; border-bottom-color:#dbeafe!important; color:#1e40af; }
body.light .bulk-bar { background:#fff; border-bottom-color:#dce0e8; }
body.light .checklist label { border-bottom-color:#eceef2; }
body.light .linked-item { border-bottom-color:#eceef2; }
body.light .pin-star { color:#d1d5de; }
body.light .pin-star:hover, body.light .pin-star.pinned { color:#f59e0b; }
body.light .priority-highest, body.light .priority-high { color:#dc2626; }
body.light .priority-medium { color:#b45309; }
body.light .age-stale { color:#dc2626; }
body.light .age-warn { color:#c2410c; }
body.light .team-tab { color:var(--text-muted); }
body.light .team-tab.active { color:var(--blue); }
body.light .team-tabs { border-bottom-color:#dce0e8; }
body.light .team-group-picker select { background:#fff; border-color:#dce0e8; }
body.light .team-group-picker button { background:#eff4ff; }
body.light .team-group-picker button:hover { background:var(--blue); }
body.light .team-report-table th { border-bottom-color:#dce0e8; }
body.light .team-report-table td { border-bottom-color:#eceef2; }
body.light .team-report-table tr.totals-row td { background:#f7f8fa; border-top-color:#dce0e8; }
body.light .team-report-table tbody tr:hover { background:#f7f8fa; }
body.light .team-report-progress { background:#eceef2; }
body.light .team-totals { border-bottom-color:#dce0e8; }
</style>
</head>
<body>

<div class="header">
  <div class="header-left">
    <div class="logo">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M8 2L6 4M16 2L18 4" stroke="white" stroke-width="2" stroke-linecap="round"/>
        <path d="M9 4h6a5 5 0 0 1 5 5v4a7 7 0 0 1-7 7h-2a7 7 0 0 1-7-7V9a5 5 0 0 1 5-5z" fill="white" fill-opacity="0.15" stroke="white" stroke-width="1.5"/>
        <path d="M12 4v16M8 10h8M8 14h8" stroke="white" stroke-width="1.5" stroke-linecap="round"/>
        <path d="M3 9h2M19 9h2M3 14h2M19 14h2" stroke="white" stroke-width="2" stroke-linecap="round"/>
      </svg>
    </div>
    <h1>Bug Tracker <span id="header-subtitle">/ My Reported Bugs</span></h1>
  </div>
  <div class="header-right">
    <input class="search-box" type="text" placeholder="Search bugs... (/ to focus)" id="search-input" oninput="onSearch()">
    <button class="btn btn-export" onclick="exportCSV()">CSV</button>
    <span class="countdown" id="countdown"></span>
    <span class="meta" id="last-refresh"></span>
    <button class="btn btn-primary" onclick="loadBugs()" id="refresh-btn">Refresh</button>
    <div class="user-pill" id="user-pill" style="display:none">
      <div class="avatar" id="user-avatar"></div>
      <span class="uname" id="user-name"></span>
    </div>
    <button class="btn-logout" id="logout-btn" style="display:none" onclick="doLogout()">Logout</button>
  </div>
</div>

<div class="stats-row" id="stats-row"></div>
<div class="feed-section" id="feed-section"></div>
<div class="toolbar" id="toolbar">
  <select id="version-filter" onchange="onVersionFilter()"><option value="">All Versions</option></select>
  <button class="tool-btn" id="group-toggle" onclick="toggleGrouping()">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>Group
  </button>
  <button class="tool-btn" id="compact-toggle" onclick="toggleCompact()">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/></svg>Compact
  </button>
  <button class="tool-btn" id="theme-toggle" onclick="toggleTheme()">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>Theme
  </button>
  <div class="spacer"></div>
  <button class="tool-btn" onclick="openTeamOverview()">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>Team
  </button>
  <button class="tool-btn" id="stats-toggle" onclick="toggleStatsPanel()">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg>Stats
  </button>
</div>
<div class="bulk-bar" id="bulk-bar">
  <span>Selected: <span class="count" id="bulk-count">0</span></span>
  <button class="btn-sm btn-build" onclick="bulkBuild()">Build All Selected</button>
  <button class="btn-sm" onclick="clearBulk()">Clear</button>
</div>
<div class="stats-section" id="stats-section" style="display:none"></div>
<div class="verify-section" id="verify-section"></div>
<div class="table-wrap">
  <div id="content">
    <div class="loading-state"><div class="spinner"></div><br>Loading bugs from Jira...</div>
  </div>
</div>

<div class="team-overlay" id="team-overlay" onclick="if(event.target===this)closeTeamOverview()">
  <div class="team-modal">
    <h2>
      <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>
      Team Overview
      <button class="close-btn" onclick="closeTeamOverview()">&times;</button>
    </h2>
    <div class="team-tabs">
      <button class="team-tab active" id="tab-my-team" onclick="switchTeamTab('my')">Dashboard Users</button>
      <button class="team-tab" id="tab-group-report" onclick="switchTeamTab('group')">Team Lead Report</button>
    </div>
    <div id="team-tab-my"><div class="team-grid" id="team-grid"><div class="loading-state"><div class="spinner"></div></div></div></div>
    <div id="team-tab-group" style="display:none">
      <div class="team-group-picker">
        <div class="group-search-wrap">
          <input id="group-search-input" class="group-search-input" type="text" placeholder="Search for a Jira group..." autocomplete="off">
          <div id="group-dropdown" class="group-dropdown"></div>
          <input type="hidden" id="group-select-value" value="">
        </div>
        <button onclick="loadGroupReport()" id="group-load-btn">Load Report</button>
      </div>
      <div id="group-report"></div>
    </div>
  </div>
</div>

<div class="kbd-hint">
  <span><kbd>j</kbd><kbd>k</kbd> navigate</span>
  <span><kbd>Enter</kbd> details</span>
  <span><kbd>b</kbd> build</span>
  <span><kbd>/</kbd> search</span>
  <span><kbd>Esc</kbd> close</span>
  <span><kbd>p</kbd> pin</span>
  <span><kbd>x</kbd> select</span>
</div>

<script>
let allBugs = [];
let activeFilter = 'all';
let searchQuery = '';
let detailsCache = {};
let focusedIdx = -1;
let prevBugStatuses = {};
let refreshTimer = null;
const REFRESH_INTERVAL = 5*60*1000;
let versionFilter = '';
let groupByVersion = false;
let compactMode = localStorage.getItem('compact') === '1';
let currentTheme = localStorage.getItem('theme') || 'dark';
let pinnedBugs = new Set();
let userNotes = {};
let userChecklist = {};
let bulkSelected = new Set();
let activityLog = [];
let statsPanelOpen = false;

async function authFetch(url, opts) {
  const resp = await fetch(url, opts);
  if (resp.status === 401) { window.location.href = '/login'; return null; }
  return resp;
}

let isLimited = false;

async function loadUserInfo() {
  try {
    const resp = await authFetch('/api/me');
    if (!resp) return;
    if (!resp.ok) { window.location.href = '/login'; return; }
    const u = await resp.json();
    isLimited = !!u.limited;
    const pill = document.getElementById('user-pill');
    const avatar = document.getElementById('user-avatar');
    const uname = document.getElementById('user-name');
    const logoutBtn = document.getElementById('logout-btn');
    const initials = (u.display_name||'').split(' ').map(w=>w[0]).join('').toUpperCase().slice(0,2);
    avatar.textContent = initials;
    uname.textContent = u.display_name + (isLimited ? ' (view-only)' : '');
    pill.style.display = 'flex';
    logoutBtn.style.display = '';
    if (isLimited) pill.style.borderColor = '#5e3a15';
    const sub = document.getElementById('header-subtitle');
    if (sub) sub.textContent = '/ ' + u.display_name + "'s Bugs";
  } catch(e) {}
  try {
    const r2 = await authFetch('/api/user/prefs');
    if (r2 && r2.ok) {
      const p = await r2.json();
      pinnedBugs = new Set(p.pinned || []);
      userNotes = p.notes || {};
      userChecklist = p.checklist || {};
      if (p.theme) { currentTheme = p.theme; }
    }
  } catch(e) {}
  applyTheme();
  if (compactMode) document.body.classList.add('compact');
  updateCompactBtn();
}

async function doLogout() {
  await fetch('/api/logout', {method:'POST'});
  window.location.href = '/login';
}

const STATUS_BADGE = {
  'Open':['badge-open','Open'],'Under Investigation':['badge-investigation','Investigating'],
  'In Progress':['badge-progress','In Progress'],'In Review':['badge-review','In Review'],
  'Pending Merge':['badge-merge','Pending Merge'],'Closed':['badge-closed','Closed'],
  'Done':['badge-closed','Done'],'Resolved':['badge-closed','Resolved'],
  'Verified':['badge-verified','Verified'],'Cancelled':['badge-cancelled','Cancelled'],
  "Won't Fix":['badge-cancelled',"Won't Fix"],'Duplicate':['badge-cancelled','Duplicate'],
};
const CLOSED = new Set(['Done','Closed','Resolved','Cancelled',"Won't Fix",'Duplicate','Verified']);
const NEEDS_VERIFY = new Set(['Done','Closed','Resolved']);
const ACTIVE = new Set(['In Progress','In Review','Pending Merge','Under Investigation']);

function esc(s) { const d=document.createElement('div'); d.textContent=s; return d.innerHTML; }

function relTime(iso) {
  if (!iso) return '';
  const diff = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (diff < 60) return 'now';
  if (diff < 3600) return Math.floor(diff/60)+'m';
  if (diff < 86400) return Math.floor(diff/3600)+'h';
  return Math.floor(diff/86400)+'d';
}

function priorityClass(p) {
  const l=(p||'').toLowerCase();
  if (l==='highest'||l==='critical') return 'priority-highest';
  if (l==='high') return 'priority-high';
  if (l==='medium') return 'priority-medium';
  return 'priority-low';
}

function statusBadge(status) {
  const [cls,label] = STATUS_BADGE[status] || ['badge-open',status];
  return `<span class="badge ${cls}">${label}</span>`;
}

function getCounts() {
  const c = {all:allBugs.length, open:0, active:0, verify:0, closed:0};
  allBugs.forEach(b => {
    if (NEEDS_VERIFY.has(b.status)) { c.verify++; c.closed++; }
    else if (CLOSED.has(b.status)) c.closed++;
    else if (ACTIVE.has(b.status)) c.active++;
    else c.open++;
  });
  return c;
}

function renderStats() {
  const c = getCounts();
  const cards = [
    ['all','Total Bugs', c.all,'stat-all'],
    ['open','Open', c.open,'stat-open'],
    ['active','In Progress', c.active,'stat-active'],
    ['verify','Needs Verification', c.verify,'stat-verify'],
    ['closed','Closed / Verified', c.closed,'stat-closed'],
  ];
  document.getElementById('stats-row').innerHTML = cards.map(([id,label,val,cls]) =>
    `<div class="stat-card ${cls} ${activeFilter===id?'active':''}" onclick="setFilter('${id}')">
      <div class="stat-val">${val}</div>
      <div class="stat-label">${label}</div>
    </div>`
  ).join('');
}

function setFilter(f) { activeFilter=f; renderStats(); renderVerifySection(); renderTable(); }
function onSearch() { searchQuery=document.getElementById('search-input').value.toLowerCase(); renderVerifySection(); renderTable(); }

function filteredBugs() {
  return allBugs.filter(b => {
    let pass = true;
    if (activeFilter==='open') pass = !CLOSED.has(b.status) && !ACTIVE.has(b.status);
    else if (activeFilter==='active') pass = ACTIVE.has(b.status);
    else if (activeFilter==='verify') pass = NEEDS_VERIFY.has(b.status);
    else if (activeFilter==='closed') pass = CLOSED.has(b.status);
    if (pass && versionFilter) {
      pass = (b.fix_versions||[]).includes(versionFilter);
    }
    if (pass && searchQuery) {
      pass = (b.key+' '+b.summary+' '+b.assignee+' '+(b.branch||'')).toLowerCase().includes(searchQuery);
    }
    return pass;
  });
}

function getVerifyBugs() {
  return allBugs.filter(b => {
    let pass = NEEDS_VERIFY.has(b.status);
    if (pass && searchQuery) {
      pass = (b.key+' '+b.summary+' '+b.assignee+' '+(b.branch||'')).toLowerCase().includes(searchQuery);
    }
    return pass;
  });
}

function renderVerifySection() {
  const sec = document.getElementById('verify-section');
  if (activeFilter==='verify') { sec.innerHTML=''; return; }
  const vBugs = getVerifyBugs();
  if (!vBugs.length) { sec.innerHTML=''; return; }
  let cards = vBugs.map(b => {
    const ver = (b.fix_versions||[]).join(', ') || '-';
    const br = b.branch ? esc(b.branch) : '-';
    const buildBtn = b.branch
      ? `<button class="btn-sm btn-build" id="vbbtn-${b.key}" onclick="triggerBuild('${b.key}','${esc(b.branch)}','vbbtn-')">Build</button>` : '';
    return `<div class="verify-card" id="vc-${b.key}">
      <div class="verify-card-body">
        <div class="verify-card-top">
          <a class="verify-card-key" href="https://drivenets.atlassian.net/browse/${b.key}" target="_blank">${b.key}</a>
          ${statusBadge(b.status)}
        </div>
        <div class="verify-card-summary">${esc(b.summary)}</div>
        <div class="verify-card-meta">
          <span><strong>Assignee:</strong> ${esc(b.assignee)}</span>
          <span><strong>Priority:</strong> <span class="${priorityClass(b.priority)}">${esc(b.priority)}</span></span>
          <span><strong>Version:</strong> ${esc(ver)}</span>
          <span><strong>Branch:</strong> ${esc(br)}</span>
        </div>
      </div>
      <div class="verify-card-actions">
        <button class="btn-sm" onclick="toggleVerifyDetail('${b.key}')">Details</button>
        ${buildBtn}
      </div>
    </div>`;
  }).join('');
  sec.innerHTML = `<div class="verify-banner">
    <div class="verify-banner-header">
      <div class="verify-banner-title"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 9v2m0 4h.01M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0z"/></svg>Needs Verification</div>
      <span class="verify-count">${vBugs.length} bug${vBugs.length!==1?'s':''}</span>
    </div>
    <div class="verify-cards">${cards}</div>
    <div class="verify-detail-panel" id="vdp"></div>
  </div>`;
}

function renderTopology(topo) {
  if (!topo) return '';
  const t = topo.type, p = topo.platform || 'DUT', hasTG = topo.traffic;
  const sub = topo.subtype || '';
  const verifyMethods = (topo.verify || []);
  const W = 700, H = 180;
  const COL = {
    box: '#1a1e28', text: '#e4e8f1', muted: '#5c6480',
    link: '#5b8af5', linkLabel: '#9098b0', accent: '#5b8af5', green: '#3dd68c',
    orange: '#f0c541', purple: '#a684f5', red: '#f06565', cyan: '#56d4e8',
    mgmt: '#8b9dc3',
  };
  const F = "'JetBrains Mono',monospace";
  const FD = "'DM Sans',sans-serif";

  function box(x, y, label, sub, color, w) {
    w = w || 104; color = color || COL.accent;
    return `<rect x="${x-w/2}" y="${y-20}" width="${w}" height="40" rx="8"
        fill="${COL.box}" stroke="${color}" stroke-width="1.5"/>
      <text x="${x}" y="${y+1}" text-anchor="middle" fill="${COL.text}"
        font-size="11" font-weight="600" font-family="${FD}">${label}</text>
      ${sub ? `<text x="${x}" y="${y+14}" text-anchor="middle" fill="${COL.muted}"
        font-size="9" font-family="${F}">${sub}</text>` : ''}`;
  }
  function line(x1, y1, x2, y2, label, dashed) {
    const dash = dashed ? ' stroke-dasharray="6,4"' : '';
    const mx = (x1+x2)/2, my = (y1+y2)/2;
    return `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}"
        stroke="${COL.link}" stroke-width="1.5"${dash}/>
      <circle cx="${x2}" cy="${y2}" r="3" fill="${COL.link}"/>
      ${label ? `<text x="${mx}" y="${my-8}" text-anchor="middle" fill="${COL.linkLabel}"
        font-size="9" font-family="${F}">${label}</text>` : ''}`;
  }
  function arrow(x1, y1, x2, y2, label, color) {
    color = color || COL.link;
    const id = 'ah' + Math.random().toString(36).slice(2,6);
    const mx = (x1+x2)/2, my = (y1+y2)/2;
    return `<defs><marker id="${id}" markerWidth="8" markerHeight="6" refX="8" refY="3"
        orient="auto"><path d="M0,0 L8,3 L0,6" fill="${color}"/></marker></defs>
      <line x1="${x1}" y1="${y1}" x2="${x2-8}" y2="${y2}"
        stroke="${color}" stroke-width="1.5" marker-end="url(#${id})"/>
      ${label ? `<text x="${mx}" y="${my-8}" text-anchor="middle" fill="${COL.linkLabel}"
        font-size="9" font-family="${F}">${label}</text>` : ''}`;
  }
  function badge(x, y, text, color) {
    const tw = text.length * 5.5 + 14;
    return `<rect x="${x-tw/2}" y="${y-9}" width="${tw}" height="18" rx="9"
        fill="${color}20" stroke="${color}" stroke-width="1"/>
      <text x="${x}" y="${y+3}" text-anchor="middle" fill="${color}"
        font-size="8" font-weight="600" font-family="${F}">${text}</text>`;
  }
  function sshLine(x1, y1, x2, y2) {
    return `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}"
        stroke="${COL.mgmt}" stroke-width="1" stroke-dasharray="3,3"/>
      <circle cx="${x1}" cy="${y1}" r="2" fill="${COL.mgmt}"/>`;
  }
  function verifyTag(x, y, methods) {
    if (!methods || !methods.length) return '';
    const txt = methods.slice(0,3).join(' · ');
    const tw = txt.length * 4.5 + 16;
    return `<rect x="${x-tw/2}" y="${y-8}" width="${tw}" height="16" rx="8"
        fill="${COL.muted}18" stroke="${COL.muted}" stroke-width="0.5"/>
      <text x="${x}" y="${y+3}" text-anchor="middle" fill="${COL.muted}"
        font-size="7.5" font-family="${F}">verify: ${txt}</text>`;
  }

  let svg = '';
  const cy = 80;

  if (t === 'cfm') {
    const dutLabel = sub === 'pm' ? 'MEP-1' : 'MEP-Local';
    const peerLabel = sub === 'pm' ? 'MEP-2' : 'MEP-Remote';
    const linkLabel = sub === 'pm' ? 'DM / SLM probes' : 'CCM / LBM / LTM';
    if (hasTG) {
      svg = box(80,cy,'Traffic Gen','Ixia / Spirent',COL.green)
        + line(132,cy,178,cy,'')
        + box(230,cy,dutLabel,p,COL.accent)
        + line(282,cy,368,cy,linkLabel)
        + box(420,cy,peerLabel,p,COL.accent)
        + line(472,cy,518,cy,'')
        + box(570,cy,'Traffic Gen','Ixia / Spirent',COL.green);
    } else {
      svg = box(220,cy,dutLabel,p,COL.accent)
        + line(272,cy,388,cy,linkLabel)
        + box(440,cy,peerLabel,p,COL.accent);
    }
    svg += sshLine(230,cy+20,230,H-12)
      + badge(230,H-12,'SSH: show cfm / snmpwalk',COL.cyan);

  } else if (t === 'multicast') {
    svg = box(90,cy,'Source','Traffic Gen',COL.green)
      + arrow(142,cy,208,cy,'')
      + box(260,cy,'PIM Router',p,COL.accent)
      + arrow(312,cy-12,398,42,'OIF sub-intf')
      + arrow(312,cy+12,398,H-58,'OIF sub-intf')
      + box(450,42,'Receiver-1','sub-intf .1',COL.purple)
      + box(450,H-58,'Receiver-2','sub-intf .2',COL.purple);
    svg += sshLine(260,cy+20,260,H-12)
      + badge(260,H-12,'show multicast fwd-table',COL.cyan);

  } else if (t === 'evpn') {
    if (hasTG) {
      svg = box(80,cy,'CE-1','Traffic Gen',COL.green)
        + line(132,cy,178,cy,'');
    } else {
      svg = box(80,cy,'CE-1','',COL.muted)
        + line(132,cy,178,cy,'');
    }
    svg += box(230,cy,'PE-1',p,COL.accent)
      + line(282,cy,368,cy,'EVPN Fabric')
      + box(420,cy,'PE-2',p,COL.accent);
    if (hasTG) {
      svg += line(472,cy,518,cy,'') + box(570,cy,'CE-2','Traffic Gen',COL.green);
    } else {
      svg += line(472,cy,518,cy,'') + box(570,cy,'CE-2','',COL.muted);
    }
    svg += sshLine(230,cy+20,230,H-12)
      + badge(325,H-12,'show evpn / show bgp l2vpn',COL.cyan);

  } else if (t === 'l2xc') {
    if (hasTG) {
      svg = box(80,cy,'CE-1','Traffic Gen',COL.green)
        + line(132,cy,178,cy,'');
    } else {
      svg = box(80,cy,'CE-1','',COL.muted)
        + line(132,cy,178,cy,'');
    }
    svg += box(230,cy,'PE-1',p,COL.accent)
      + line(282,cy,368,cy,'VPWS / L2XC')
      + box(420,cy,'PE-2',p,COL.accent);
    if (hasTG) {
      svg += line(472,cy,518,cy,'') + box(570,cy,'CE-2','Traffic Gen',COL.green);
    } else {
      svg += line(472,cy,518,cy,'') + box(570,cy,'CE-2','',COL.muted);
    }
    svg += sshLine(230,cy+20,230,H-12)
      + badge(325,H-12,'show l2vpn xconnect',COL.cyan);

  } else if (t === 'qos') {
    svg = box(130,cy,'Traffic Gen','marked pkts',COL.green)
      + arrow(182,cy,268,cy,'ingress')
      + box(320,cy,p,'QoS Policy',COL.accent)
      + arrow(372,cy,458,cy,'shaped')
      + box(510,cy,'Traffic Gen','counters',COL.green)
      + badge(320,cy+35,'DSCP / PCP / ECN',COL.orange)
      + sshLine(320,cy-20,320,18)
      + badge(320,12,'show qos / counters',COL.cyan);

  } else if (t === 'acl') {
    svg = box(130,cy,hasTG?'Traffic Gen':'Client',hasTG?'Ixia / Spirent':'',hasTG?COL.green:COL.muted)
      + arrow(182,cy,268,cy,'ingress')
      + box(320,cy,p,'DUT',COL.accent)
      + arrow(372,cy,458,cy,'egress')
      + box(510,cy,hasTG?'Traffic Gen':'Peer',hasTG?'Ixia / Spirent':'',hasTG?COL.green:COL.muted)
      + badge(320,cy+35,'ACL applied',COL.orange)
      + sshLine(320,cy-20,320,18)
      + badge(320,12,'show acl / counters',COL.cyan);

  } else if (t === 'bgp') {
    if (hasTG) {
      svg = box(70,cy,'Traffic Gen','',COL.green)
        + line(122,cy,178,cy,'')
        + box(230,cy,'Router A',p,COL.accent)
        + line(282,cy,388,cy,'eBGP / iBGP')
        + box(440,cy,'Router B',p,COL.accent)
        + line(492,cy,538,cy,'')
        + box(590,cy,'Traffic Gen','',COL.green);
    } else {
      svg = box(230,cy,'Router A',p,COL.accent)
        + line(282,cy,388,cy,'eBGP / iBGP')
        + box(440,cy,'Router B',p,COL.accent);
    }
    svg += sshLine(230,cy+20,230,H-12)
      + badge(335,H-12,'show bgp / show route',COL.cyan);

  } else if (t === 'routing') {
    if (hasTG) {
      svg = box(60,cy,'Traffic Gen','',COL.green)
        + line(112,cy,158,cy,'')
        + box(210,cy,'Router A',p,COL.accent)
        + line(262,cy,318,cy,'IGP')
        + box(370,cy,'Router B',p,COL.accent)
        + line(422,cy,478,cy,'IGP')
        + box(530,cy,'Router C',p,COL.accent)
        + line(582,cy,618,cy,'')
        + box(670,cy,'Traffic Gen','',COL.green);
    } else {
      svg = box(150,cy,'Router A',p,COL.accent)
        + line(202,cy,288,cy,'IGP')
        + box(340,cy,'Router B',p,COL.accent)
        + line(392,cy,478,cy,'IGP')
        + box(530,cy,'Router C',p,COL.accent);
    }
    svg += sshLine(hasTG?210:150,cy+20,hasTG?210:150,H-12)
      + badge(hasTG?210:340,H-12,'show isis/ospf route',COL.cyan);

  } else if (t === 'ha') {
    svg = box(130,cy,hasTG?'Traffic Gen':'Peer',hasTG?'':'',hasTG?COL.green:COL.muted)
      + line(182,cy,248,cy,'')
      + box(310,50,'Active',p,COL.accent)
      + box(310,H-60,'Standby',p,COL.muted)
      + `<line x1="310" y1="70" x2="310" y2="${H-80}"
          stroke="${COL.orange}" stroke-width="1.5" stroke-dasharray="6,4"/>
        <text x="330" y="${cy}" fill="${COL.orange}"
          font-size="9" font-family="${F}">HA pair</text>`
      + line(362,50,448,50,'')
      + line(362,H-60,448,H-60,'',true)
      + box(510,cy,'Peer','',COL.muted)
      + badge(310,H-12,'switchover / show redundancy',COL.cyan);

  } else if (t === 'interface') {
    if (hasTG) {
      svg = box(130,cy,'Traffic Gen','Ixia / Spirent',COL.green)
        + line(182,cy,268,cy,'')
        + box(320,cy,p,'DUT',COL.accent)
        + line(372,cy,458,cy,'')
        + box(510,cy,'Peer',p,COL.muted);
    } else {
      svg = box(220,cy,'Peer','',COL.muted)
        + line(272,cy,368,cy,'link')
        + box(420,cy,p,'DUT',COL.accent);
    }
    svg += badge(hasTG?320:420,cy+35,'port / optic / bundle',COL.orange)
      + sshLine(hasTG?320:420,cy-20,hasTG?320:420,18)
      + badge(hasTG?320:420,12,'show interfaces counters',COL.cyan);

  } else if (t === 'hw') {
    svg = box(320,cy,p,'DUT',COL.accent)
      + badge(320,cy+35,'thermal / fan / PSU',COL.orange)
      + sshLine(320,cy-20,320,18)
      + badge(320,12,'show system platform / IPMI',COL.cyan);

  } else if (t === 'crash') {
    svg = box(320,cy,p,'DUT',COL.accent)
      + badge(320,cy+35,'process crash',COL.red)
      + sshLine(320,cy-20,320,18)
      + badge(320,12,'show system / journalctl',COL.cyan)
      + box(540,cy,'Logs','tech-support',COL.muted)
      + line(372,cy,488,cy,'core dump',true);

  } else if (t === 'upgrade') {
    svg = box(170,cy,'Image Server','',COL.muted)
      + arrow(222,cy,308,cy,'install')
      + box(370,cy,p,'DUT',COL.accent)
      + badge(370,cy+35,'ISSU / upgrade',COL.orange)
      + sshLine(370,cy-20,370,18)
      + badge(370,12,'show version / show install',COL.cyan);

  } else if (t === 'snmp') {
    svg = box(200,cy,'QA Workstation','snmpwalk / snmpget',COL.cyan)
      + arrow(252,cy-5,358,cy-5,'SNMP GET/WALK')
      + arrow(408,cy+5,302,cy+5,'TRAP/INFORM')
      + box(460,cy,p,'SNMP Agent',COL.accent)
      + badge(330,H-12,'verify OID values',COL.cyan);

  } else if (t === 'syslog') {
    svg = box(320,cy,p,'DUT',COL.accent)
      + badge(320,cy+35,'system events',COL.orange)
      + sshLine(320,cy-20,320,18)
      + badge(320,12,'check syslog / events',COL.cyan)
      + box(540,cy,'Syslog','collector',COL.muted)
      + arrow(372,cy,488,cy,'events',COL.orange);

  } else if (t === 'management') {
    svg = box(200,cy,'QA Workstation','NETCONF / gNMI',COL.cyan)
      + arrow(252,cy,358,cy,'query / config')
      + box(420,cy,p,'DUT',COL.accent)
      + badge(420,cy+35,'YANG model',COL.orange)
      + badge(310,H-12,'verify oper-items',COL.cyan);

  } else if (t === 'cli') {
    svg = box(320,cy,p,'DUT',COL.accent)
      + sshLine(320,cy-20,320,18)
      + badge(320,12,'SSH: CLI (cfg)# commit',COL.cyan)
      + badge(320,cy+35,'show cmd output',COL.orange);

  } else {
    if (hasTG) {
      svg = box(130,cy,'Traffic Gen','Ixia / Spirent',COL.green)
        + arrow(182,cy,268,cy,'test traffic')
        + box(320,cy,p,'DUT',COL.accent)
        + arrow(372,cy,458,cy,'test traffic')
        + box(510,cy,'Traffic Gen','Ixia / Spirent',COL.green);
    } else {
      svg = box(320,cy,p,'DUT',COL.accent)
        + sshLine(320,cy-20,320,18)
        + badge(320,12,'SSH to DUT',COL.cyan);
    }
  }

  svg += verifyTag(W/2, H-2, verifyMethods);

  return `<div class="topo-card">
    <h4><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <circle cx="5" cy="12" r="2"/><circle cx="19" cy="5" r="2"/><circle cx="19" cy="19" r="2"/>
      <line x1="7" y1="12" x2="17" y2="5"/><line x1="7" y1="12" x2="17" y2="19"/>
    </svg>Test Topology</h4>
    <svg class="topo-svg" viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg">${svg}</svg>
  </div>`;
}

// ── Sorting ──────────────────────────────────────
let sortCol = null, sortAsc = true;
const PRIORITY_ORDER = {Highest:0,Critical:0,High:1,Medium:2,Low:3,Lowest:4,None:5};

function setSort(col) {
  if (sortCol === col) sortAsc = !sortAsc;
  else { sortCol = col; sortAsc = true; }
  renderTable();
}

function sortedBugs(bugs) {
  if (!sortCol) return bugs;
  const arr = [...bugs];
  arr.sort((a,b) => {
    let va, vb;
    if (sortCol==='key') { va=a.key; vb=b.key; }
    else if (sortCol==='summary') { va=a.summary.toLowerCase(); vb=b.summary.toLowerCase(); }
    else if (sortCol==='status') { va=STATUS_ORDER[a.status]||99; vb=STATUS_ORDER[b.status]||99; }
    else if (sortCol==='assignee') { va=a.assignee.toLowerCase(); vb=b.assignee.toLowerCase(); }
    else if (sortCol==='priority') { va=PRIORITY_ORDER[a.priority]||5; vb=PRIORITY_ORDER[b.priority]||5; }
    else if (sortCol==='updated') { va=a.updated||''; vb=b.updated||''; }
    else if (sortCol==='age') { va=a.created||''; vb=b.created||''; }
    else return 0;
    if (va < vb) return sortAsc ? -1 : 1;
    if (va > vb) return sortAsc ? 1 : -1;
    return 0;
  });
  return arr;
}

const STATUS_ORDER_SORT = {Open:0,'Under Investigation':1,'In Progress':2,'In Review':3,
  'Pending Merge':4,Closed:5,Done:5,Resolved:5,Verified:6,Cancelled:7,"Won't Fix":7,Duplicate:7};

function sortArrow(col) {
  if (sortCol !== col) return '<span class="sort-arrow">&#x25B4;&#x25BE;</span>';
  return `<span class="sort-arrow active">${sortAsc?'&#x25B4;':'&#x25BE;'}</span>`;
}

// ── Bug age ──────────────────────────────────────
function bugAge(created) {
  if (!created) return {text:'-',cls:'age-ok'};
  const days = Math.floor((Date.now() - new Date(created).getTime()) / 86400000);
  let cls = 'age-ok';
  if (days > 60) cls = 'age-stale';
  else if (days > 30) cls = 'age-warn';
  let text;
  if (days < 1) text = '<1d';
  else if (days < 30) text = days+'d';
  else text = Math.floor(days/30)+'mo';
  return {text, cls};
}

// ── Copy to clipboard ────────────────────────────
async function copyText(id) {
  const el = document.getElementById(id);
  if (!el) return;
  const text = el.textContent || el.innerText;
  try {
    await navigator.clipboard.writeText(text);
    showToast('Copied to clipboard');
  } catch(e) {
    const ta = document.createElement('textarea');
    ta.value = text; document.body.appendChild(ta); ta.select();
    document.execCommand('copy'); document.body.removeChild(ta);
    showToast('Copied to clipboard');
  }
}

function showToast(msg) {
  const t = document.createElement('div');
  t.className = 'toast'; t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 3000);
}

// ── CSV export ───────────────────────────────────
function exportCSV() {
  const bugs = filteredBugs();
  const rows = [['Key','Summary','Status','Assignee','Priority','Version','Branch','Created','Updated']];
  bugs.forEach(b => {
    rows.push([b.key, '"'+b.summary.replace(/"/g,'""')+'"', b.status, b.assignee, b.priority,
      (b.fix_versions||[]).join(';'), b.branch||'', b.created||'', b.updated||'']);
  });
  const csv = rows.map(r => r.join(',')).join('\n');
  const blob = new Blob([csv], {type:'text/csv'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'bugs_'+new Date().toISOString().slice(0,10)+'.csv';
  a.click();
  showToast('Exported '+bugs.length+' bugs to CSV');
}

// ── Auto-refresh countdown ───────────────────────
let nextRefresh = 0;
function startCountdown() {
  nextRefresh = Date.now() + REFRESH_INTERVAL;
  if (refreshTimer) clearInterval(refreshTimer);
  refreshTimer = setInterval(updateCountdown, 1000);
}
function updateCountdown() {
  const remaining = Math.max(0, Math.floor((nextRefresh - Date.now()) / 1000));
  const m = Math.floor(remaining/60), s = remaining%60;
  const el = document.getElementById('countdown');
  if (el) el.textContent = m+':'+(s<10?'0':'')+s;
}

// ── Browser notifications ────────────────────────
function requestNotifPermission() {
  if ('Notification' in window && Notification.permission === 'default') {
    Notification.requestPermission();
  }
}
function checkStatusChanges(newBugs) {
  if (!('Notification' in window) || Notification.permission !== 'granted') return;
  newBugs.forEach(b => {
    const prev = prevBugStatuses[b.key];
    if (prev && prev !== b.status) {
      new Notification('Bug Status Changed', {
        body: b.key+': '+prev+' → '+b.status+'\n'+b.summary,
        icon: 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><text y=".9em" font-size="90">🐛</text></svg>',
      });
    }
  });
  newBugs.forEach(b => { prevBugStatuses[b.key] = b.status; });
}
requestNotifPermission();

// ── Theme toggle ─────────────────────────────────
function applyTheme() {
  document.body.classList.toggle('light', currentTheme === 'light');
  const btn = document.getElementById('theme-toggle');
  if (btn) {
    btn.classList.toggle('active', currentTheme === 'light');
    btn.innerHTML = currentTheme === 'light'
      ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>Dark'
      : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>Light';
  }
}
function toggleTheme() {
  currentTheme = currentTheme === 'dark' ? 'light' : 'dark';
  localStorage.setItem('theme', currentTheme);
  applyTheme();
  savePrefs();
}

// ── Compact toggle ───────────────────────────────
function updateCompactBtn() {
  const btn = document.getElementById('compact-toggle');
  if (btn) btn.classList.toggle('active', compactMode);
}
function toggleCompact() {
  compactMode = !compactMode;
  localStorage.setItem('compact', compactMode ? '1' : '0');
  document.body.classList.toggle('compact', compactMode);
  updateCompactBtn();
}

// ── Version filter ───────────────────────────────
function populateVersionFilter() {
  const sel = document.getElementById('version-filter');
  const versions = new Set();
  allBugs.forEach(b => (b.fix_versions||[]).forEach(v => versions.add(v)));
  const sorted = [...versions].sort();
  sel.innerHTML = '<option value="">All Versions</option>' +
    sorted.map(v => `<option value="${v}" ${v===versionFilter?'selected':''}>${v}</option>`).join('');
}
function onVersionFilter() {
  versionFilter = document.getElementById('version-filter').value;
  renderVerifySection(); renderTable();
}

// ── Grouping toggle ──────────────────────────────
function toggleGrouping() {
  groupByVersion = !groupByVersion;
  const btn = document.getElementById('group-toggle');
  if (btn) btn.classList.toggle('active', groupByVersion);
  renderTable();
}

// ── Pinned bugs ──────────────────────────────────
function togglePin(key) {
  if (pinnedBugs.has(key)) pinnedBugs.delete(key); else pinnedBugs.add(key);
  savePrefs();
  renderTable();
}

// ── Bulk select ──────────────────────────────────
function toggleBulk(key) {
  if (bulkSelected.has(key)) bulkSelected.delete(key); else bulkSelected.add(key);
  updateBulkBar();
}
function updateBulkBar() {
  const bar = document.getElementById('bulk-bar');
  const cnt = document.getElementById('bulk-count');
  cnt.textContent = bulkSelected.size;
  bar.classList.toggle('visible', bulkSelected.size > 0);
}
function clearBulk() { bulkSelected.clear(); updateBulkBar(); renderTable(); }
async function bulkBuild() {
  const bugs = allBugs.filter(b => bulkSelected.has(b.key) && b.branch);
  if (!bugs.length) { showToast('No selected bugs have a branch'); return; }
  for (const b of bugs) {
    await triggerBuild(b.key, b.branch, 'bbtn-');
  }
  showToast('Triggered ' + bugs.length + ' builds');
  clearBulk();
}

// ── Save preferences ─────────────────────────────
let prefsSaveTimeout = null;
function savePrefs() {
  clearTimeout(prefsSaveTimeout);
  prefsSaveTimeout = setTimeout(async () => {
    await authFetch('/api/user/prefs', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({
        pinned:[...pinnedBugs], notes:userNotes, checklist:userChecklist, theme:currentTheme,
      }),
    });
  }, 500);
}

// ── Personal notes ───────────────────────────────
function onNoteChange(key, el) {
  userNotes[key] = el.value;
  savePrefs();
}

// ── Verification checklist ───────────────────────
const CHECKLIST_ITEMS = ['Reproduced original issue','Applied fix build','Verified fix works','Regression check passed'];
function renderChecklist(key) {
  const checks = userChecklist[key] || [false,false,false,false];
  return `<div class="checklist">${CHECKLIST_ITEMS.map((item,i) =>
    `<label class="${checks[i]?'done':''}"><input type="checkbox" ${checks[i]?'checked':''} onchange="onChecklistChange('${key}',${i},this.checked)">${item}</label>`
  ).join('')}</div>`;
}
function onChecklistChange(key, idx, val) {
  if (!userChecklist[key]) userChecklist[key] = [false,false,false,false];
  userChecklist[key][idx] = val;
  savePrefs();
  const label = document.querySelector(`input[onchange*="'${key}',${idx}"]`);
  if (label) label.parentElement.classList.toggle('done', val);
}

// ── Activity feed ────────────────────────────────
function updateActivityFeed(newBugs) {
  newBugs.forEach(b => {
    const prev = prevBugStatuses[b.key];
    if (prev && prev !== b.status) {
      activityLog.unshift({key:b.key, from:prev, to:b.status, time:new Date().toISOString()});
      if (activityLog.length > 20) activityLog.length = 20;
    }
  });
  renderActivityFeed();
}
function renderActivityFeed() {
  const sec = document.getElementById('feed-section');
  if (!activityLog.length) { sec.innerHTML = ''; return; }
  const items = activityLog.slice(0, 10).map(a =>
    `<div class="feed-item">
      <span class="feed-key">${a.key}</span>
      ${statusBadge(a.from)}
      <span class="feed-arrow">&rarr;</span>
      ${statusBadge(a.to)}
      <span class="feed-time">${relTime(a.time)}</span>
    </div>`
  ).join('');
  sec.innerHTML = `<div class="feed-bar">${items}</div>`;
}

// ── Team overview ────────────────────────────────
let teamGroupsLoaded = false;

function switchTeamTab(tab) {
  document.getElementById('tab-my-team').classList.toggle('active', tab === 'my');
  document.getElementById('tab-group-report').classList.toggle('active', tab === 'group');
  document.getElementById('team-tab-my').style.display = tab === 'my' ? '' : 'none';
  document.getElementById('team-tab-group').style.display = tab === 'group' ? '' : 'none';
  if (tab === 'group' && !teamGroupsLoaded) {
    loadGroupList();
    setupGroupSearch();
  }
}

async function openTeamOverview() {
  document.getElementById('team-overlay').classList.add('open');
  const grid = document.getElementById('team-grid');
  grid.innerHTML = '<div class="loading-state"><div class="spinner"></div></div>';
  try {
    const resp = await authFetch('/api/team');
    if (!resp) return;
    const team = await resp.json();
    if (!team.length) { grid.innerHTML = '<div class="empty-state">No team members found</div>'; return; }
    grid.innerHTML = team.map(m => `<div class="team-card">
      <div class="t-name">${esc(m.display_name)}</div>
      <div class="t-counts">
        <span><span class="num c-open">${m.counts.open}</span>Open</span>
        <span><span class="num c-active">${m.counts.active}</span>Active</span>
        <span><span class="num c-verify">${m.counts.verify}</span>Verify</span>
        <span><span class="num">${m.counts.total}</span>Total</span>
      </div>
    </div>`).join('');
  } catch(e) { grid.innerHTML = '<div class="empty-state">Failed to load team data</div>'; }
}
function closeTeamOverview() { document.getElementById('team-overlay').classList.remove('open'); }

let groupSearchTimer = null;
let groupAllResults = [];
let selectedGroupName = '';

async function loadGroupList() {
  const input = document.getElementById('group-search-input');
  const dd = document.getElementById('group-dropdown');
  input.value = '';
  selectedGroupName = '';
  document.getElementById('group-select-value').value = '';
  dd.classList.remove('open');
  try {
    const resp = await authFetch('/api/team/groups');
    if (!resp) return;
    groupAllResults = await resp.json();
    teamGroupsLoaded = true;
  } catch(e) { groupAllResults = []; }
}

function setupGroupSearch() {
  const input = document.getElementById('group-search-input');
  const dd = document.getElementById('group-dropdown');
  if (!input) return;

  input.addEventListener('focus', () => {
    renderGroupDropdown(input.value.trim());
    dd.classList.add('open');
  });

  input.addEventListener('input', () => {
    clearTimeout(groupSearchTimer);
    const q = input.value.trim();
    if (q.length >= 2) {
      groupSearchTimer = setTimeout(async () => {
        try {
          const resp = await authFetch('/api/team/groups?q=' + encodeURIComponent(q));
          if (!resp) return;
          const results = await resp.json();
          renderGroupDropdown(q, results);
        } catch(e) {}
      }, 300);
    } else {
      renderGroupDropdown(q);
    }
  });

  document.addEventListener('click', (e) => {
    if (!e.target.closest('.group-search-wrap')) {
      dd.classList.remove('open');
    }
  });
}

function renderGroupDropdown(query, searchResults) {
  const dd = document.getElementById('group-dropdown');
  let items = searchResults || groupAllResults;
  if (query && !searchResults) {
    const q = query.toLowerCase();
    items = groupAllResults.filter(g => g.name.toLowerCase().includes(q));
  }
  if (!items.length) {
    dd.innerHTML = '<div class="gd-empty">No groups found</div>';
    dd.classList.add('open');
    return;
  }
  dd.innerHTML = items.map(g =>
    `<div class="gd-item${g.name === selectedGroupName ? ' selected' : ''}" onclick="selectGroup('${esc(g.name).replace(/'/g, "\\\\'")}')">${esc(g.name)}</div>`
  ).join('');
  dd.classList.add('open');
}

function selectGroup(name) {
  selectedGroupName = name;
  document.getElementById('group-select-value').value = name;
  document.getElementById('group-search-input').value = name;
  document.getElementById('group-dropdown').classList.remove('open');
}

async function loadGroupReport() {
  const group = selectedGroupName || document.getElementById('group-select-value').value;
  if (!group) { showToast('Search and select a group first'); return; }
  const btn = document.getElementById('group-load-btn');
  const out = document.getElementById('group-report');
  btn.disabled = true; btn.textContent = 'Loading...';
  out.innerHTML = '<div class="loading-state"><div class="spinner"></div><br>Fetching bug data for all team members...</div>';
  try {
    const resp = await authFetch('/api/team/report?group=' + encodeURIComponent(group));
    if (!resp) return;
    if (!resp.ok) { const d = await resp.json(); out.innerHTML = `<div class="empty-state">Error: ${esc(d.error||'Unknown')}</div>`; return; }
    const data = await resp.json();
    renderGroupReport(data);
  } catch(e) { out.innerHTML = `<div class="empty-state">Failed: ${esc(e.message)}</div>`; }
  finally { btn.disabled = false; btn.textContent = 'Load Report'; }
}

let showAllGroupMembers = false;

function renderGroupReport(data) {
  const out = document.getElementById('group-report');
  const t = data.totals;
  const withBugs = data.members.filter(m => m.counts.total > 0);
  const noBugs = data.members.filter(m => m.counts.total === 0);
  const allMembers = data.members;

  let html = `<div class="team-totals">
    <div class="tt"><div class="num" style="color:var(--text)">${allMembers.length}</div><div class="lbl">Team Members</div></div>
    <div class="tt"><div class="num" style="color:var(--text)">${t.total}</div><div class="lbl">Total Bugs</div></div>
    <div class="tt"><div class="num" style="color:var(--blue)">${t.open}</div><div class="lbl">Open</div></div>
    <div class="tt"><div class="num" style="color:var(--yellow)">${t.active}</div><div class="lbl">In Progress</div></div>
    <div class="tt"><div class="num" style="color:var(--orange)">${t.verify}</div><div class="lbl">Needs Verify</div></div>
    <div class="tt"><div class="num" style="color:var(--green)">${t.closed}</div><div class="lbl">Closed</div></div>
  </div>`;

  if (!allMembers.length) {
    html += '<div class="empty-state">No members found in this group</div>';
    out.innerHTML = html;
    return;
  }

  const displayMembers = showAllGroupMembers ? allMembers : withBugs;
  const toggleLabel = showAllGroupMembers
    ? `Hide ${noBugs.length} members with no bugs`
    : `Show all ${allMembers.length} members (${noBugs.length} with no bugs)`;

  html += `<div style="margin-bottom:10px;display:flex;align-items:center;gap:12px">
    <button onclick="showAllGroupMembers=!showAllGroupMembers;renderGroupReport(window._lastGroupData)"
      style="background:var(--surface2);border:1px solid var(--border);border-radius:var(--radius-sm);padding:5px 12px;
      font-size:12px;color:var(--text-muted);cursor:pointer;font-family:inherit">${toggleLabel}</button>
    <span style="font-size:11px;color:var(--text-muted)">${withBugs.length} active reporters</span>
  </div>`;

  html += `<table class="team-report-table"><thead><tr>
    <th>Member</th>
    <th class="num-col">Open</th>
    <th class="num-col">Active</th>
    <th class="num-col">Verify</th>
    <th class="num-col">Closed</th>
    <th class="num-col">Total</th>
    <th style="min-width:100px">Distribution</th>
  </tr></thead><tbody>`;

  displayMembers.forEach(m => {
    const c = m.counts;
    const tot = c.total || 1;
    const dimClass = c.total === 0 ? ' style="opacity:.5"' : '';
    html += `<tr${dimClass}>
      <td><strong>${esc(m.display_name)}</strong><br><span style="font-size:11px;color:var(--text-muted)">${esc(m.email)}</span></td>
      <td class="num-cell" style="color:var(--blue)">${c.open}</td>
      <td class="num-cell" style="color:var(--yellow)">${c.active}</td>
      <td class="num-cell" style="color:var(--orange)">${c.verify}</td>
      <td class="num-cell" style="color:var(--green)">${c.closed}</td>
      <td class="num-cell">${c.total}</td>
      <td><div class="team-report-progress">
        ${c.total ? `<div class="seg seg-open" style="width:${(c.open/tot*100).toFixed(1)}%"></div>
        <div class="seg seg-active" style="width:${(c.active/tot*100).toFixed(1)}%"></div>
        <div class="seg seg-verify" style="width:${(c.verify/tot*100).toFixed(1)}%"></div>
        <div class="seg seg-closed" style="width:${((c.closed-c.verify)/tot*100).toFixed(1)}%"></div>` : '<div style="text-align:center;font-size:11px;color:var(--text-muted)">—</div>'}
      </div></td>
    </tr>`;
  });

  html += `<tr class="totals-row">
    <td>Total (${allMembers.length} members)</td>
    <td class="num-cell" style="color:var(--blue)">${t.open}</td>
    <td class="num-cell" style="color:var(--yellow)">${t.active}</td>
    <td class="num-cell" style="color:var(--orange)">${t.verify}</td>
    <td class="num-cell" style="color:var(--green)">${t.closed}</td>
    <td class="num-cell">${t.total}</td>
    <td></td>
  </tr></tbody></table>`;

  out.innerHTML = html;
  window._lastGroupData = data;
}

// ── Stats panel ──────────────────────────────────
function toggleStatsPanel() {
  statsPanelOpen = !statsPanelOpen;
  const sec = document.getElementById('stats-section');
  const btn = document.getElementById('stats-toggle');
  if (btn) btn.classList.toggle('active', statsPanelOpen);
  if (!statsPanelOpen) { sec.style.display='none'; return; }
  sec.style.display='block';
  renderStatsPanel();
}
function renderStatsPanel() {
  const sec = document.getElementById('stats-section');
  if (!statsPanelOpen || !allBugs.length) { return; }
  const now = Date.now();
  let totalAge = 0, closedCount = 0, closedAge = 0;
  const weekBuckets = {};
  allBugs.forEach(b => {
    const created = new Date(b.created).getTime();
    const ageDays = Math.floor((now - created) / 86400000);
    totalAge += ageDays;
    if (CLOSED.has(b.status)) {
      closedCount++;
      const updated = new Date(b.updated).getTime();
      closedAge += Math.floor((updated - created) / 86400000);
    }
    const week = new Date(b.created).toISOString().slice(0, 10);
    const wk = week.slice(0, 7);
    weekBuckets[wk] = (weekBuckets[wk] || 0) + 1;
  });
  const avgAge = allBugs.length ? Math.round(totalAge / allBugs.length) : 0;
  const avgResolve = closedCount ? Math.round(closedAge / closedCount) : 0;
  const weeks = Object.keys(weekBuckets).sort().slice(-8);
  const maxW = Math.max(...weeks.map(w => weekBuckets[w]), 1);
  const bars = weeks.map(w => {
    const h = Math.round((weekBuckets[w] / maxW) * 50);
    return `<div class="bar" style="height:${h}px" title="${w}: ${weekBuckets[w]}"></div>`;
  }).join('');
  sec.innerHTML = `<div class="stats-panel">
    <h3>Bug Analytics</h3>
    <div class="stats-row-inner">
      <div class="stat-mini"><div class="num" style="color:var(--blue)">${avgAge}d</div><div class="lbl">Avg Age</div></div>
      <div class="stat-mini"><div class="num" style="color:var(--green)">${avgResolve}d</div><div class="lbl">Avg Resolution</div></div>
      <div class="stat-mini"><div class="num" style="color:var(--yellow)">${closedCount}</div><div class="lbl">Resolved</div></div>
      <div class="stat-mini"><div class="num" style="color:var(--text-secondary)">${allBugs.length}</div><div class="lbl">Total (180d)</div></div>
      <div style="flex:1;min-width:200px">
        <div style="font-size:10px;color:var(--text-muted);margin-bottom:2px">Bugs filed by month</div>
        <div class="bar-chart">${bars}</div>
        <div style="display:flex;justify-content:space-between;font-size:9px;color:var(--text-muted);margin-top:2px">
          <span>${weeks[0]||''}</span><span>${weeks[weeks.length-1]||''}</span>
        </div>
      </div>
    </div>
  </div>`;
}

// ── Jira comment ─────────────────────────────────
async function addComment(key) {
  const input = document.getElementById('ci-'+key);
  if (!input) return;
  const body = input.value.trim();
  if (!body) return;
  const btn = input.nextElementSibling;
  btn.disabled = true; btn.textContent = 'Posting...';
  try {
    const resp = await authFetch('/api/bugs/'+key+'/comment', {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({body}),
    });
    if (!resp) return;
    if (resp.ok) {
      input.value = '';
      showToast('Comment added to '+key);
      delete detailsCache[key];
    } else {
      const d = await resp.json();
      alert('Failed: '+(d.error||'Unknown'));
    }
  } catch(e) { alert('Error: '+e.message); }
  finally { btn.disabled = false; btn.textContent = 'Post'; }
}

function renderTable() {
  let bugs = sortedBugs(filteredBugs());
  if (!bugs.length) {
    document.getElementById('content').innerHTML = '<div class="empty-state">No bugs match this filter.</div>';
    return;
  }
  // Pin sort: pinned bugs first
  bugs = [...bugs].sort((a,b) => {
    const pa = pinnedBugs.has(a.key) ? 0 : 1;
    const pb = pinnedBugs.has(b.key) ? 0 : 1;
    return pa - pb;
  });
  const cols = 11;
  let h = `<table class="bug-table"><thead><tr>
    <th style="width:30px"><input class="bulk-cb" type="checkbox" onchange="toggleAllBulk(this.checked)"></th>
    <th style="width:28px"></th>
    <th class="sortable" style="width:100px" onclick="setSort('key')">Key ${sortArrow('key')}</th>
    <th class="sortable" onclick="setSort('summary')">Summary ${sortArrow('summary')}</th>
    <th class="sortable" style="width:120px" onclick="setSort('status')">Status ${sortArrow('status')}</th>
    <th class="sortable" style="width:140px" onclick="setSort('assignee')">Assignee ${sortArrow('assignee')}</th>
    <th class="sortable" style="width:80px" onclick="setSort('priority')">Priority ${sortArrow('priority')}</th>
    <th style="width:70px">Version</th><th style="width:150px">Branch</th>
    <th class="sortable" style="width:50px" onclick="setSort('age')">Age ${sortArrow('age')}</th>
    <th class="sortable" style="width:60px" onclick="setSort('updated')">Updated ${sortArrow('updated')}</th>
    <th style="width:120px"></th>
  </tr></thead><tbody>`;

  let lastGroup = null;
  bugs.forEach((b,i) => {
    if (groupByVersion) {
      const grp = (b.fix_versions||[]).join(', ') || 'No Version';
      if (grp !== lastGroup) {
        lastGroup = grp;
        h += `<tr class="group-header"><td colspan="${cols}">${esc(grp)}</td></tr>`;
      }
    }
    const ver = (b.fix_versions||[]).map(v=>`<span class="fix-tag">${esc(v)}</span>`).join(' ') || '<span class="muted">-</span>';
    const br = b.jenkins_url
      ? `<a class="branch-link" href="${b.jenkins_url}" target="_blank" title="${esc(b.branch)}">${esc(b.branch)}</a>`
      : '<span class="muted">-</span>';
    const buildBtn = b.branch
      ? `<button class="btn-sm btn-build" id="bbtn-${b.key}" onclick="event.stopPropagation();triggerBuild('${b.key}','${esc(b.branch)}','bbtn-')">Build</button>` : '';
    const age = bugAge(b.created);
    const isPinned = pinnedBugs.has(b.key);
    const isChecked = bulkSelected.has(b.key);
    h += `<tr id="row-${b.key}" data-idx="${i}">
      <td><input class="bulk-cb" type="checkbox" ${isChecked?'checked':''} onchange="toggleBulk('${b.key}')"></td>
      <td><button class="pin-star ${isPinned?'pinned':''}" onclick="togglePin('${b.key}')" title="Pin">${isPinned?'&#9733;':'&#9734;'}</button></td>
      <td><a class="key-link" href="${b.jira_url}" target="_blank">${b.key}</a></td>
      <td class="summary-cell"><span class="summary-text">${esc(b.summary)}</span></td>
      <td>${statusBadge(b.status)}</td><td>${esc(b.assignee)}</td>
      <td><span class="${priorityClass(b.priority)}">${esc(b.priority)}</span></td>
      <td>${ver}</td><td>${br}</td>
      <td><span class="${age.cls}">${age.text}</span></td>
      <td class="updated-cell">${relTime(b.updated)}</td>
      <td><div class="actions-cell">${buildBtn}<button class="btn-sm" onclick="toggleDetails('${b.key}')">Details</button></div></td>
    </tr><tr class="detail-tr" id="dtr-${b.key}"><td colspan="${cols}"><div class="detail-panel" id="dp-${b.key}"></div></td></tr>`;
  });
  h += '</tbody></table>';
  document.getElementById('content').innerHTML = h;
  focusedIdx = -1;
}

function toggleAllBulk(checked) {
  const bugs = sortedBugs(filteredBugs());
  bulkSelected.clear();
  if (checked) bugs.forEach(b => bulkSelected.add(b.key));
  updateBulkBar();
  renderTable();
}

async function toggleDetails(key) {
  const p = document.getElementById(`dp-${key}`);
  const r = document.getElementById(`row-${key}`);
  if (p.classList.contains('open')) { p.classList.remove('open'); r.classList.remove('expanded'); return; }
  p.classList.add('open'); r.classList.add('expanded');
  if (detailsCache[key]) { renderDetails(key, detailsCache[key]); return; }
  p.innerHTML = '<div class="detail-loading">Loading verification steps...</div>';
  try {
    const resp = await authFetch(`/api/bugs/${key}/details`);
    if (!resp) return;
    const data = await resp.json();
    detailsCache[key] = data;
    renderDetails(key, data);
  } catch(e) { p.innerHTML = '<div class="detail-loading">Failed to load details.</div>'; }
}

function buildStatusHtml(bs) {
  if (!bs) return '';
  let cls = 'unknown', label = 'Unknown';
  if (bs.building) { cls='building'; label='Building #'+bs.number; }
  else if (bs.result==='SUCCESS') { cls='success'; label='Passed #'+bs.number; }
  else if (bs.result==='FAILURE') { cls='failure'; label='Failed #'+bs.number; }
  else if (bs.result==='ABORTED') { cls='unknown'; label='Aborted #'+bs.number; }
  else if (bs.result) { cls='unknown'; label=bs.result+' #'+bs.number; }
  return ` <span class="build-dot ${cls}"></span><span style="font-size:11px;color:var(--text-muted)">${label}</span>`;
}

function sshLinksHtml(hosts) {
  if (!hosts || !hosts.length) return '';
  return '<div class="ssh-links-row">' + hosts.map(h =>
    `<a class="ssh-link" href="#" onclick="copyText(null);navigator.clipboard.writeText('ssh dnroot@${esc(h)}');showToast('Copied: ssh dnroot@${esc(h)}');return false;">
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="3" width="20" height="18" rx="2"/><path d="M7 15l3-3-3-3M13 15h4"/></svg>
      ${esc(h)}</a>`
  ).join('') + '</div>';
}

function linkedBugsHtml(linked) {
  if (!linked || !linked.length) return '';
  return `<h4 style="margin-top:16px"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>Linked Issues</h4>`
    + linked.map(l =>
      `<div class="linked-item"><span class="linked-rel">${esc(l.relation)}</span>
        <a class="linked-key" href="${l.url}" target="_blank">${l.key}</a>
        ${statusBadge(l.status)}
        <span class="linked-summary">${esc(l.summary)}</span></div>`
    ).join('');
}

function renderDetails(key, data) {
  const p = document.getElementById(`dp-${key}`);
  const v = data.verification || {};
  let jenkinsHtml = data.jenkins_url
    ? `<a href="${data.jenkins_url}" target="_blank">${esc(data.branch)}</a>`
    : '<span class="muted">No branch detected</span>';
  jenkinsHtml += buildStatusHtml(data.build_status);
  const stepsId = 'steps-'+key;
  let stepsHtml;
  if (v.steps_to_reproduce) stepsHtml = `<pre id="${stepsId}">${esc(v.steps_to_reproduce)}</pre>`;
  else if (v.bug_context) stepsHtml = `<span class="ctx-badge">From bug description</span><pre id="${stepsId}">${esc(v.bug_context)}</pre>`;
  else stepsHtml = '<span class="muted">No description available</span>';
  let commHtml;
  if (v.dev_comments && v.dev_comments.length) {
    commHtml = v.dev_comments.map(c =>
      `<div class="comment-item"><div class="comment-who">${esc(c.author)}</div><div class="comment-text">${esc(c.body)}</div></div>`
    ).join('');
  } else commHtml = '<span class="muted">No developer comments</span>';
  const title = v.steps_to_reproduce ? 'Steps to Reproduce' : 'Bug Context';
  const topoHtml = renderTopology(data.topology);
  const linkedHtml = linkedBugsHtml(data.linked_bugs);
  const copyBtn = (v.steps_to_reproduce || v.bug_context)
    ? `<button class="copy-btn" onclick="copyText('${stepsId}')">Copy</button>` : '';
  const checkHtml = renderChecklist(key);
  const noteVal = esc(userNotes[key] || '');
  p.innerHTML = `<div class="detail-inner">${topoHtml}<div class="detail-grid">
    <div class="detail-card">
      <h4><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/></svg>Jenkins Branch</h4>${jenkinsHtml}
      <h4 style="margin-top:16px"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>${title} ${copyBtn}</h4>${stepsHtml}
      ${linkedHtml}
      <h4 style="margin-top:16px"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>Verification Checklist</h4>${checkHtml}
    </div><div class="detail-card">
      <h4><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>Developer Notes</h4>${commHtml}
      ${isLimited ? '' : `<div class="comment-form">
        <input class="comment-input" id="ci-${key}" placeholder="Add a quick comment..." onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();addComment('${key}')}">
        <button class="btn-sm" onclick="addComment('${key}')">Post</button>
      </div>`}
      <h4 style="margin-top:16px"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>My Notes</h4>
      <textarea class="notes-area" placeholder="Personal notes for this bug..." oninput="onNoteChange('${key}',this)">${noteVal}</textarea>
    </div></div></div>`;
}

let activeVerifyKey = null;

async function toggleVerifyDetail(key) {
  const panel = document.getElementById('vdp');
  if (!panel) return;
  document.querySelectorAll('.verify-card.selected').forEach(c => c.classList.remove('selected'));
  if (activeVerifyKey === key) {
    panel.classList.remove('open');
    activeVerifyKey = null;
    return;
  }
  activeVerifyKey = key;
  const card = document.getElementById(`vc-${key}`);
  if (card) card.classList.add('selected');
  panel.classList.add('open');
  if (detailsCache[key]) { renderVerifyDetailContent(key, detailsCache[key]); return; }
  panel.innerHTML = '<div class="detail-loading">Loading verification steps...</div>';
  try {
    const resp = await authFetch(`/api/bugs/${key}/details`);
    if (!resp) return;
    const data = await resp.json();
    detailsCache[key] = data;
    renderVerifyDetailContent(key, data);
  } catch(e) { panel.innerHTML = '<div class="detail-loading">Failed to load details.</div>'; }
}

function closeVerifyDetail() {
  const panel = document.getElementById('vdp');
  if (panel) panel.classList.remove('open');
  document.querySelectorAll('.verify-card.selected').forEach(c => c.classList.remove('selected'));
  activeVerifyKey = null;
}

function renderVerifyDetailContent(key, data) {
  const panel = document.getElementById('vdp');
  if (!panel) return;
  const v = data.verification || {};
  const bug = allBugs.find(b => b.key === key);
  const summary = bug ? esc(bug.summary) : key;
  let jenkinsHtml = data.jenkins_url
    ? `<a href="${data.jenkins_url}" target="_blank">${esc(data.branch)}</a>`
    : '<span class="muted">No branch detected</span>';
  jenkinsHtml += buildStatusHtml(data.build_status);
  const stepsId = 'vsteps-'+key;
  let stepsHtml;
  if (v.steps_to_reproduce) stepsHtml = `<pre id="${stepsId}">${esc(v.steps_to_reproduce)}</pre>`;
  else if (v.bug_context) stepsHtml = `<span class="ctx-badge">From bug description</span><pre id="${stepsId}">${esc(v.bug_context)}</pre>`;
  else stepsHtml = '<span class="muted">No description available</span>';
  let commHtml;
  if (v.dev_comments && v.dev_comments.length) {
    commHtml = v.dev_comments.map(c =>
      `<div class="comment-item"><div class="comment-who">${esc(c.author)}</div><div class="comment-text">${esc(c.body)}</div></div>`
    ).join('');
  } else commHtml = '<span class="muted">No developer comments</span>';
  const title = v.steps_to_reproduce ? 'Steps to Reproduce' : 'Bug Context';
  const topoHtml = renderTopology(data.topology);
  const linkedHtml = linkedBugsHtml(data.linked_bugs);
  const copyBtn = (v.steps_to_reproduce || v.bug_context)
    ? `<button class="copy-btn" onclick="copyText('${stepsId}')">Copy</button>` : '';
  const checkHtml = renderChecklist(key);
  const noteVal = esc(userNotes[key] || '');
  panel.innerHTML = `
    <div class="verify-detail-header">
      <h3>${key} &mdash; ${summary}</h3>
      <button class="verify-detail-close" onclick="closeVerifyDetail()">&times;</button>
    </div>
    ${topoHtml}
    <div class="detail-grid">
      <div class="detail-card">
        <h4><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/></svg>Jenkins Branch</h4>${jenkinsHtml}
        <h4 style="margin-top:16px"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>${title} ${copyBtn}</h4>${stepsHtml}
        ${linkedHtml}
        <h4 style="margin-top:16px"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>Verification Checklist</h4>${checkHtml}
      </div><div class="detail-card">
        <h4><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>Developer Notes</h4>${commHtml}
        ${isLimited ? '' : `<div class="comment-form">
          <input class="comment-input" id="ci-${key}" placeholder="Add a quick comment..." onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();addComment('${key}')}">
          <button class="btn-sm" onclick="addComment('${key}')">Post</button>
        </div>`}
        <h4 style="margin-top:16px"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>My Notes</h4>
        <textarea class="notes-area" placeholder="Personal notes for this bug..." oninput="onNoteChange('${key}',this)">${noteVal}</textarea>
      </div>
    </div>`;
}

async function triggerBuild(key, branch, prefix) {
  prefix = prefix || 'bbtn-';
  const btn = document.getElementById(`${prefix}${key}`);
  if (!btn) return;
  btn.disabled = true; btn.textContent = 'Building...';
  try {
    const resp = await authFetch(`/api/bugs/${key}/build`, {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({branch}),
    });
    if (!resp) return;
    const data = await resp.json();
    if (resp.ok) { btn.textContent='Triggered'; btn.classList.add('done'); btn.disabled=true; }
    else { btn.textContent='Failed'; btn.disabled=false; alert('Build failed: '+(data.error||'Unknown')); setTimeout(()=>{btn.textContent='Build'},3000); }
  } catch(e) { btn.textContent='Error'; btn.disabled=false; setTimeout(()=>{btn.textContent='Build'},3000); }
}

async function loadBugs() {
  const btn = document.getElementById('refresh-btn');
  btn.disabled=true; btn.textContent='Loading...'; detailsCache={};
  try {
    const resp = await authFetch('/api/bugs');
    if (!resp) return;
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const newBugs = await resp.json();
    updateActivityFeed(newBugs);
    checkStatusChanges(newBugs);
    allBugs = newBugs;
    document.getElementById('last-refresh').textContent = new Date().toLocaleTimeString();
    populateVersionFilter();
    renderStats(); renderVerifySection(); renderTable(); renderStatsPanel();
  } catch(e) {
    document.getElementById('content').innerHTML = `<div class="empty-state">Error: ${esc(e.message)}</div>`;
  } finally { btn.disabled=false; btn.textContent='Refresh'; startCountdown(); }
}

// ── Keyboard shortcuts ───────────────────────────
document.addEventListener('keydown', e => {
  const tag = (e.target.tagName||'').toLowerCase();
  if (tag === 'input' || tag === 'textarea') {
    if (e.key === 'Escape') { e.target.blur(); return; }
    return;
  }
  const bugs = sortedBugs(filteredBugs());
  if (e.key === 'j' || e.key === 'ArrowDown') {
    e.preventDefault();
    if (focusedIdx < bugs.length-1) focusedIdx++;
    highlightRow(bugs);
  } else if (e.key === 'k' || e.key === 'ArrowUp') {
    e.preventDefault();
    if (focusedIdx > 0) focusedIdx--;
    highlightRow(bugs);
  } else if (e.key === 'Enter' && focusedIdx >= 0 && focusedIdx < bugs.length) {
    e.preventDefault();
    toggleDetails(bugs[focusedIdx].key);
  } else if (e.key === 'b' && focusedIdx >= 0 && focusedIdx < bugs.length) {
    const b = bugs[focusedIdx];
    if (b.branch) triggerBuild(b.key, b.branch, 'bbtn-');
  } else if (e.key === 'p' && focusedIdx >= 0 && focusedIdx < bugs.length) {
    e.preventDefault();
    togglePin(bugs[focusedIdx].key);
  } else if (e.key === 'x' && focusedIdx >= 0 && focusedIdx < bugs.length) {
    e.preventDefault();
    toggleBulk(bugs[focusedIdx].key);
    renderTable();
  } else if (e.key === '/') {
    e.preventDefault();
    document.getElementById('search-input').focus();
  } else if (e.key === 'Escape') {
    document.querySelectorAll('.detail-panel.open').forEach(p => p.classList.remove('open'));
    document.querySelectorAll('tr.expanded').forEach(r => r.classList.remove('expanded'));
    closeVerifyDetail();
    focusedIdx = -1;
    document.querySelectorAll('tr.focused').forEach(r => r.classList.remove('focused'));
  }
});

function highlightRow(bugs) {
  document.querySelectorAll('tr.focused').forEach(r => r.classList.remove('focused'));
  if (focusedIdx >= 0 && focusedIdx < bugs.length) {
    const row = document.getElementById('row-'+bugs[focusedIdx].key);
    if (row) {
      row.classList.add('focused');
      row.scrollIntoView({block:'nearest',behavior:'smooth'});
    }
  }
}

loadUserInfo().then(() => { loadBugs(); setInterval(loadBugs, REFRESH_INTERVAL); });
</script>
</body>
</html>
"""

if __name__ == "__main__":
    port = int(os.environ.get("DASHBOARD_PORT", "5000"))
    print(f"Bug Dashboard starting on http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
