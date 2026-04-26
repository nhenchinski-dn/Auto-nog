#!/usr/bin/env python3
"""
Bug Verifier — acts as a QA engineer for a Jira bug.

Flow (autonomous where possible, confirms before Jira writes):

  1. fetch    — pull the bug from Jira (REST) or load a JSON fed by an agent
  2. plan     — parse the bug description into a structured verification plan
                (setup commands, reproduction commands, assertions, cleanup)
  3. deploy   — load the Jenkins fix build onto the target device
                (upgrade in-place, or fresh deploy with config save/restore)
  4. verify   — SSH to the device, run setup/repro/assertions/cleanup,
                collect .gitcommit + `show system version`, emit a verdict
  5. report   — build a draft Jira comment describing PASS or "still broken"
  6. comment  — post the comment (and transition the ticket) with --confirm

Usage:
  python3 bug_verifier.py fetch   <BUG> [--from-file file.json]
  python3 bug_verifier.py plan    <BUG> [--edit]
  python3 bug_verifier.py deploy  <BUG> --device <ip> --build <jenkins-url>
                                         --mode {upgrade,deploy,deploy-with-config,skip}
  python3 bug_verifier.py verify  <BUG> --device <ip>
  python3 bug_verifier.py report  <BUG>
  python3 bug_verifier.py run     <BUG> --device <ip> --build <jenkins-url>
                                         --mode <mode>
  python3 bug_verifier.py comment <BUG> [--transition reopen|verified|none] --confirm

State directory (per bug):  /home/dn/bug_verifier_state/<BUG>/
  bug.json              fetched Jira issue (raw REST JSON)
  plan.json             structured verification plan
  deploy.log            deploy/upgrade output
  evidence.txt          verification CLI transcript
  gitcommit.txt         contents of /.gitcommit from the device
  verdict.json          PASS/FAIL with per-assertion detail
  proposed_comment.txt  Jira comment body (ready to post)

Jira auth (for `fetch` and `comment`) — optional; reads one of:
  - Env: JIRA_EMAIL + JIRA_API_TOKEN  (and optional JIRA_BASE_URL)
  - File: ~/.jira_token  {"email": "...", "token": "...", "base_url": "..."}
If no credentials, `fetch --from-file` and `comment --dry-run` still work.
"""

import argparse
import base64
import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

STATE_ROOT = "/home/dn/bug_verifier_state"
DEFAULT_JIRA_BASE = "https://drivenets.atlassian.net"
DEPLOY_SCRIPT = "/home/dn/dnos_deploy.py"
UPGRADE_SCRIPT = "/home/dn/dnos_upgrade.py"
UPGRADE_INSTALL_SCRIPT = "/home/dn/dnos_upgrade_install.py"

# --------------------------------------------------------------------------- #
# State helpers
# --------------------------------------------------------------------------- #

def state_dir(bug_key: str) -> str:
    d = os.path.join(STATE_ROOT, bug_key)
    os.makedirs(d, exist_ok=True)
    return d


def state_path(bug_key: str, name: str) -> str:
    return os.path.join(state_dir(bug_key), name)


def read_json(path):
    with open(path) as f:
        return json.load(f)


def write_json(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, default=str)


def now_iso() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Jira REST client (no third-party deps)
# --------------------------------------------------------------------------- #

class JiraClient:
    def __init__(self, email=None, token=None, base_url=None):
        email = email or os.environ.get("JIRA_EMAIL")
        token = token or os.environ.get("JIRA_API_TOKEN")
        base_url = base_url or os.environ.get("JIRA_BASE_URL") or DEFAULT_JIRA_BASE

        # Fall back to ~/.jira_token
        if (not email or not token) and os.path.exists(os.path.expanduser("~/.jira_token")):
            try:
                with open(os.path.expanduser("~/.jira_token")) as f:
                    data = json.load(f)
                email = email or data.get("email")
                token = token or data.get("token")
                base_url = data.get("base_url", base_url)
            except Exception:
                pass

        self.email = email
        self.token = token
        self.base_url = base_url.rstrip("/")

    def available(self) -> bool:
        return bool(self.email and self.token)

    def _headers(self):
        creds = f"{self.email}:{self.token}".encode()
        return {
            "Authorization": "Basic " + base64.b64encode(creds).decode(),
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _request(self, method, path, body=None):
        if not self.available():
            raise RuntimeError(
                "Jira credentials not configured. Set JIRA_EMAIL + JIRA_API_TOKEN "
                "or create ~/.jira_token, or use --from-file to feed bug data."
            )
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                text = resp.read().decode()
                return json.loads(text) if text else {}
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Jira {method} {path} failed: {e.code} {e.read().decode()[:400]}")

    def get_issue(self, key: str):
        return self._request("GET", f"/rest/api/3/issue/{key}?expand=renderedFields,names")

    def add_comment(self, key: str, body_markdown: str):
        # Use ADF with a single codeBlock-ish paragraph from raw text for simplicity.
        adf = {
            "body": {
                "type": "doc", "version": 1,
                "content": [
                    {"type": "paragraph",
                     "content": [{"type": "text", "text": body_markdown}]},
                ],
            }
        }
        return self._request("POST", f"/rest/api/3/issue/{key}/comment", adf)

    def get_transitions(self, key: str):
        return self._request("GET", f"/rest/api/3/issue/{key}/transitions")

    def transition(self, key: str, transition_id: str):
        return self._request("POST", f"/rest/api/3/issue/{key}/transitions",
                             {"transition": {"id": transition_id}})


# --------------------------------------------------------------------------- #
# ADF / description flattening
# --------------------------------------------------------------------------- #

def _flatten_adf(node) -> str:
    """Flatten an ADF node (or Jira wiki string) into plain text."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_flatten_adf(x) for x in node)
    if not isinstance(node, dict):
        return str(node)

    t = node.get("type")
    content = node.get("content", [])

    if t == "text":
        return node.get("text", "")
    if t in ("hardBreak", "break"):
        return "\n"
    if t in ("paragraph", "heading"):
        return _flatten_adf(content) + "\n"
    if t in ("bulletList", "orderedList"):
        out = []
        for i, li in enumerate(content, 1):
            prefix = f"{i}. " if t == "orderedList" else "- "
            out.append(prefix + _flatten_adf(li).strip())
        return "\n".join(out) + "\n"
    if t == "listItem":
        return _flatten_adf(content)
    if t in ("codeBlock", "code_block"):
        return "```\n" + _flatten_adf(content) + "\n```\n"
    if t == "rule":
        return "\n---\n"
    if t in ("table", "tableRow", "tableHeader", "tableCell"):
        return _flatten_adf(content) + "\n"
    return _flatten_adf(content)


def description_to_text(issue: dict) -> str:
    desc = issue.get("fields", {}).get("description")
    if desc is None:
        # sometimes renderedFields has an HTML string
        r = issue.get("renderedFields", {}).get("description")
        if isinstance(r, str):
            return re.sub(r"<[^>]+>", "", r)
        return ""
    if isinstance(desc, str):
        return desc
    return _flatten_adf(desc)


# --------------------------------------------------------------------------- #
# Bug description parser — produces a plan.json draft
# --------------------------------------------------------------------------- #

SECTION_ALIASES = {
    "steps": [r"steps?\s*to\s*reproduce", r"repro(?:duction)?\s*steps?", r"how\s*to\s*reproduce"],
    "expected": [r"expected\s*results?", r"expected\s*behaviou?r", r"expected"],
    "actual": [r"actual\s*results?", r"actual\s*behaviou?r", r"observed"],
    "environment": [r"environment\s*details?", r"setup", r"topology"],
    "workaround": [r"workarounds?"],
    "gitcommit": [r"git\s*commit"],
    "techsupport": [r"tech[-\s]*support\s*link"],
}


def _find_section(text: str, aliases):
    # Match "**Expected Results:**", "h3. Expected Results", "Expected Results:", etc.
    for alias in aliases:
        pattern = (
            rf"(?:\*\*|h[1-6]\.\s*|)\s*{alias}\s*[:：]?\s*(?:\*\*)?"
            r"\s*(?:\n|\r\n)+"
        )
        m = re.search(pattern, text, flags=re.IGNORECASE)
        if m:
            start = m.end()
            # Section ends at next heading-like line or EOF
            next_m = re.search(
                r"\n\s*(?:\*\*[^*\n]+\*\*|h[1-6]\.\s+\w|[A-Z][A-Za-z ]{2,40}:)\s*(?:\n|$)",
                text[start:],
            )
            end = start + next_m.start() if next_m else len(text)
            return text[start:end].strip()
    return None


def _extract_numbered_items(block: str):
    items = []
    # Match "1. foo" or "1) foo" or "# foo" (Jira wiki)
    lines = block.splitlines()
    buf = None
    for line in lines:
        m = re.match(r"^\s*(?:\d+[\.\)]|#)\s+(.*)$", line)
        if m:
            if buf is not None:
                items.append(buf.strip())
            buf = m.group(1)
        elif buf is not None and line.strip():
            buf += " " + line.strip()
        elif buf is not None and not line.strip():
            items.append(buf.strip())
            buf = None
    if buf is not None:
        items.append(buf.strip())
    if not items:
        # fall back to non-empty lines
        items = [ln.strip() for ln in lines if ln.strip()]
    return items


def _extract_code_blocks(text: str):
    """Pull CLI commands out of {code}, {noformat}, ```fenced```, or {{monospace}}."""
    commands = []
    for m in re.finditer(r"\{code(?::[^}]*)?\}(.*?)\{code\}", text, flags=re.DOTALL):
        commands.extend(_clean_command_lines(m.group(1)))
    for m in re.finditer(r"\{noformat\}(.*?)\{noformat\}", text, flags=re.DOTALL):
        commands.extend(_clean_command_lines(m.group(1)))
    for m in re.finditer(r"```(?:[a-z]*)\n(.*?)```", text, flags=re.DOTALL):
        commands.extend(_clean_command_lines(m.group(1)))
    for m in re.finditer(r"\{\{([^{}]+)\}\}", text):
        c = m.group(1).strip()
        if _looks_like_dnos_cmd(c):
            commands.append(c)
    # preserve order, drop duplicates
    seen = set()
    out = []
    for c in commands:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _clean_command_lines(block: str):
    out = []
    for ln in block.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#") or ln.startswith("//"):
            continue
        # strip shell-style prompt leaders
        ln = re.sub(r"^(?:[\w\-.]+)(?:\([^)]+\))?\s*[#>]\s*", "", ln)
        if _looks_like_dnos_cmd(ln):
            out.append(ln)
    return out


def _looks_like_dnos_cmd(s: str) -> bool:
    s = s.strip()
    if not s or len(s) > 300:
        return False
    bad = ("Saved", "Error", "error", "Warning", "ms ", "bytes", "[", "{")
    if any(s.startswith(b) for b in bad):
        return False
    first = s.split()[0].lower()
    vocab = {
        "show", "configure", "commit", "rollback", "exit", "top",
        "set", "delete", "request", "ping", "traceroute", "clear",
        "save", "load", "copy", "run", "protocols", "interfaces",
        "system", "services", "network-services", "routing-options",
    }
    return first in vocab or "|" in s


def _guess_device_ip(env_block: str):
    if not env_block:
        return None
    m = re.search(r"((?:\d{1,3}\.){3}\d{1,3})", env_block)
    return m.group(1) if m else None


def _guess_assertions(expected_block: str, commands):
    """Generate flexible assertions from expected-results text.

    Heuristics, in order of preference:
      * inner quoted substrings:  must contain "X"  /  must not contain "X"
      * inner backtick / {{mono}} snippets
      * whole-sentence hints (for manual review)
    """
    assertions = []
    if not expected_block:
        return assertions

    # Prefer the first `show` command as the check target
    primary_cmd = next((c for c in commands if c.strip().lower().startswith("show ")), "")
    if not primary_cmd and commands:
        primary_cmd = commands[0]

    # explicit positive quoted: contain(s) "foo", include(s) "foo", show(s) "foo"
    pos_quoted = re.findall(
        r"(?:must|should|shall|will|expected to)\s+(?:contain|include|show)s?\s+[\"'`]([^\"'`\n]{1,100})[\"'`]",
        expected_block, flags=re.IGNORECASE,
    )
    # explicit negative quoted: must not / should not / does not contain "foo"
    neg_quoted = re.findall(
        r"(?:must|should|shall|will|does|do|may)\s*not\s+(?:contain|include|show)s?\s+[\"'`]([^\"'`\n]{1,100})[\"'`]",
        expected_block, flags=re.IGNORECASE,
    )

    # Generic short literals (fallback) — only when no explicit pos/neg given
    short_backticks = [
        lit for lit in re.findall(r"`([^`\n]{1,80})`", expected_block)
        if len(lit) <= 80 and len(lit) >= 2
    ]

    for lit in pos_quoted[:6]:
        assertions.append({
            "name": f"Output contains \"{lit[:50]}\"",
            "command": primary_cmd,
            "expect_contains": lit,
            "fail_message": f"'{lit}' not found in output",
        })
    for lit in neg_quoted[:6]:
        assertions.append({
            "name": f"Output must not contain \"{lit[:50]}\"",
            "command": primary_cmd,
            "expect_not_contains": lit,
            "fail_message": f"Output unexpectedly contained '{lit}'",
        })

    # Only fall back to generic literals when no explicit assertion was derived
    if not assertions:
        for lit in short_backticks[:6]:
            assertions.append({
                "name": f"Output contains '{lit[:50]}'",
                "command": primary_cmd,
                "expect_contains": lit,
                "fail_message": f"'{lit}' not found in output",
            })

    # Always add a manual-review hint if we truly got nothing
    if not assertions and primary_cmd:
        assertions.append({
            "name": "Manual review — no assertion auto-derived",
            "command": primary_cmd,
            "expect_contains": "",
            "fail_message": "Expected-results text could not be auto-parsed",
            "_hint": expected_block.strip()[:300],
        })

    # Attach a short hint sentence for human review
    hint_sentences = re.split(r"(?<=[.!?])\s+", expected_block.strip())
    for a in assertions:
        if hint_sentences and "_hint" not in a:
            a["_hint"] = hint_sentences[0][:200]

    return assertions


def build_plan(issue: dict) -> dict:
    fields = issue.get("fields", {})
    summary = fields.get("summary", "")
    key = issue.get("key")
    text = description_to_text(issue)

    steps_block = _find_section(text, SECTION_ALIASES["steps"]) or ""
    expected_block = _find_section(text, SECTION_ALIASES["expected"]) or ""
    env_block = _find_section(text, SECTION_ALIASES["environment"]) or ""

    steps_items = _extract_numbered_items(steps_block)
    code_commands = _extract_code_blocks(text)

    # Split commands heuristically: config-mode chunks → setup, show commands → repro.
    # `commit` does NOT exit config mode; only `exit`/`top`/`end` does.
    setup = []
    repro = []
    cleanup = []
    in_configure = False
    for c in code_commands:
        low = c.lower().strip()
        if low == "configure":
            in_configure = True
            setup.append(c)
        elif low in ("exit", "top", "end") and in_configure:
            setup.append(c)
            in_configure = False
        elif low in ("commit", "commit full"):
            setup.append(c)
        elif low.startswith("show "):
            repro.append(c)
        elif low.startswith("delete ") or low.startswith("rollback"):
            cleanup.append(c)
        elif in_configure:
            setup.append(c)
        else:
            repro.append(c)

    assertions = _guess_assertions(expected_block, repro or code_commands)

    # epic link (parent) so we can pull epic context if needed
    parent = fields.get("parent") or {}
    parent_key = parent.get("key")
    epic_key = fields.get("customfield_10014") or parent_key  # common epic link field

    def _has_check(a):
        return bool(a.get("expect_contains") or a.get("expect_not_contains")
                    or a.get("expect_regex"))

    concrete = sum(1 for a in assertions if _has_check(a))
    confidence = "low"
    if steps_items and code_commands:
        confidence = "medium"
    if (steps_items and code_commands and expected_block
            and assertions and concrete >= max(1, len(assertions) // 2)):
        confidence = "high"

    plan = {
        "bug_key": key,
        "summary": summary,
        "epic": epic_key,
        "generated_at": now_iso(),
        "confidence": confidence,
        "needs_human_review": confidence != "high",
        "guessed_device": _guess_device_ip(env_block),
        "steps_from_description": steps_items,
        "setup_commands": setup,
        "reproduction_commands": repro or code_commands,
        "assertions": assertions,
        "cleanup_commands": cleanup,
        "raw_expected_results": expected_block.strip(),
        "raw_environment": env_block.strip(),
    }
    return plan


# --------------------------------------------------------------------------- #
# Jenkins artifact resolver
# --------------------------------------------------------------------------- #

def resolve_jenkins_artifacts(build_url: str):
    """Fetch the three artifact-URL text files and return (baseos, dnos, gi)."""
    base = build_url.rstrip("/")
    files = {
        "baseos": f"{base}/artifact/gi_base_os_artifact.txt",
        "dnos":   f"{base}/artifact/gi_DNOS_artifact.txt",
        "gi":     f"{base}/artifact/gi_GI_artifact.txt",
    }
    out = {}
    for name, url in files.items():
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                content = r.read().decode().strip()
        except Exception as e:
            raise RuntimeError(f"Could not fetch Jenkins artifact {url}: {e}")
        pkg = content.splitlines()[0].strip()
        if not pkg.startswith("http"):
            # sometimes the file contains the URL on a different line
            for line in content.splitlines():
                line = line.strip()
                if line.startswith("http"):
                    pkg = line
                    break
        out[name] = pkg
    return out["baseos"], out["dnos"], out["gi"]


# --------------------------------------------------------------------------- #
# Deploy / upgrade wrapper
# --------------------------------------------------------------------------- #

def run_stream(cmd, log_path, extra_env=None):
    """Run a subprocess, tee stdout+stderr to console and log file."""
    print(f"\n$ {' '.join(cmd)}", flush=True)
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    with open(log_path, "a") as logf:
        logf.write(f"\n\n## {' '.join(cmd)}\n## {now_iso()}\n\n")
        logf.flush()
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, env=env,
                             bufsize=1, text=True)
        for line in p.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            logf.write(line)
            logf.flush()
        rc = p.wait()
        logf.write(f"\n## exit={rc}\n")
    return rc


def do_deploy(bug_key: str, device: str, build_url: str, mode: str) -> int:
    log = state_path(bug_key, "deploy.log")
    with open(log, "a") as f:
        f.write(f"\n### Deploy request {now_iso()}\n"
                f"device={device} mode={mode} build={build_url}\n")

    if mode == "skip":
        print("Deploy mode 'skip' — no build load.", flush=True)
        return 0

    baseos, dnos, gi = resolve_jenkins_artifacts(build_url)
    print(f"Jenkins artifacts:\n  BaseOS: {baseos}\n  DNOS:   {dnos}\n  GI:     {gi}",
          flush=True)

    if mode == "upgrade":
        rc = run_stream(["python3", UPGRADE_SCRIPT, device, baseos, dnos, gi], log)
        if rc != 0:
            return rc
        # install step
        return run_stream(["python3", UPGRADE_INSTALL_SCRIPT, device], log)
    if mode in ("deploy", "deploy-with-config"):
        # dnos_deploy.py saves, deletes, loads, deploys, restores — "all" step
        return run_stream(["python3", DEPLOY_SCRIPT, device, baseos, dnos, gi, "all"], log)

    raise ValueError(f"Unknown deploy mode: {mode}")


# --------------------------------------------------------------------------- #
# Device verification (paramiko)
# --------------------------------------------------------------------------- #

def _strip_ansi(text: str) -> str:
    # Standard CSI sequences, incl. bracketed-paste (e.g. \x1b[?2004h / \x1b[?2004l)
    text = re.sub(r"\x1b\[[?0-9;]*[a-zA-Z]", "", text)
    text = re.sub(r"\r", "", text)
    text = re.sub(r"-- More -- \(Press q to quit\)\s*", "", text)
    return text


def _ensure_no_more(cmd: str) -> str:
    if cmd.strip().startswith("show ") and "|" not in cmd:
        return cmd + " | no-more"
    return cmd


def run_verification(bug_key: str, device: str) -> dict:
    try:
        import paramiko
    except ImportError:
        raise RuntimeError("paramiko is required for device verification (pip install paramiko)")

    plan = read_json(state_path(bug_key, "plan.json"))
    os.system(f"ssh-keygen -f ~/.ssh/known_hosts -R {device} 2>/dev/null")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(device, username="dnroot", password="dnroot",
                   look_for_keys=False, allow_agent=False, timeout=30)
    shell = client.invoke_shell(width=250, height=5000)
    time.sleep(6)
    if shell.recv_ready():
        shell.recv(65535)

    evidence_lines = [f"=== Verification for {bug_key} on {device} @ {now_iso()} ==="]
    gitcommit = ""
    version_text = ""

    def send(cmd, wait=8):
        shell.send(cmd + "\n")
        time.sleep(min(wait, 3))
        out = ""
        end = time.time() + wait
        while time.time() < end:
            while shell.recv_ready():
                out += shell.recv(65535).decode("utf-8", errors="replace")
            time.sleep(0.5)
            if out and not shell.recv_ready():
                # one more drain
                time.sleep(0.5)
                while shell.recv_ready():
                    out += shell.recv(65535).decode("utf-8", errors="replace")
                break
        cleaned = _strip_ansi(out)
        evidence_lines.append(f"\n$ {cmd}\n{cleaned}")
        return cleaned

    try:
        # capture version
        version_text = send("show system version | no-more", wait=10)
        # Capture git commit. DNOS entry to bash is `run start shell` (NOT
        # `start shell` — that yields "Unknown word: 'start'" on some builds).
        # Flow: send command, answer password prompt with 'dnroot', wait for
        # a bash prompt ('#' or '$' at EOL), then `cat /.gitcommit` and exit.
        shell.send("run start shell\n")
        time.sleep(1)
        try_buf = ""
        got_password_prompt = False
        end = time.time() + 15
        while time.time() < end:
            while shell.recv_ready():
                try_buf += shell.recv(65535).decode("utf-8", errors="replace")
            cleaned = _strip_ansi(try_buf)
            if not got_password_prompt and "assword" in cleaned:
                shell.send("dnroot\n")
                got_password_prompt = True
                try_buf = ""
                continue
            # Detect a bash prompt after password was entered
            tail = cleaned.splitlines()[-1] if cleaned.splitlines() else ""
            if got_password_prompt and re.search(r"[#$]\s*$", tail):
                break
            time.sleep(0.3)
        shell.send("cat /.gitcommit\n")
        time.sleep(2)
        cat_buf = ""
        cat_end = time.time() + 5
        while time.time() < cat_end:
            while shell.recv_ready():
                cat_buf += shell.recv(65535).decode("utf-8", errors="replace")
            time.sleep(0.3)
        cat_clean = _strip_ansi(cat_buf)
        # Parse: first 40-hex run found in the output (ignoring the echoed command)
        gitcommit = ""
        for line in cat_clean.splitlines():
            if "cat /.gitcommit" in line or not line.strip():
                continue
            m = re.search(r"\b([0-9a-f]{7,40})\b", line)
            if m:
                gitcommit = m.group(1)
                break
        if not gitcommit:
            lines = [l for l in cat_clean.splitlines()
                     if l.strip() and "cat /.gitcommit" not in l]
            gitcommit = lines[0].strip()[:80] if lines else ""
        evidence_lines.append(f"\n$ cat /.gitcommit\n{cat_clean}")
        shell.send("exit\n")
        time.sleep(2)
        while shell.recv_ready():
            shell.recv(65535)

        # setup
        for cmd in plan.get("setup_commands", []):
            send(cmd, wait=10)

        # reproduction
        repro_outputs = {}
        for cmd in plan.get("reproduction_commands", []):
            real = _ensure_no_more(cmd)
            repro_outputs[cmd] = send(real, wait=12)

        # assertions
        results = []
        for a in plan.get("assertions", []):
            cmd = _ensure_no_more(a.get("command", ""))
            if cmd in repro_outputs:
                output = repro_outputs[cmd]
            elif cmd:
                output = send(cmd, wait=12)
            else:
                output = ""

            pos = a.get("expect_contains", "") or ""
            neg = a.get("expect_not_contains", "") or ""
            regex = a.get("expect_regex", "") or ""

            passed = True
            reason = ""
            if pos and pos not in output:
                passed = False
                reason = f"expected to contain '{pos[:80]}'"
            if passed and neg and neg in output:
                passed = False
                reason = f"output unexpectedly contained '{neg[:80]}'"
            if passed and regex:
                if not re.search(regex, output):
                    passed = False
                    reason = f"expected regex '{regex[:80]}' did not match"
            if not (pos or neg or regex):
                passed = False
                reason = "assertion has no expect_* condition — manual review required"
            if not passed and not reason:
                reason = a.get("fail_message", "assertion failed")

            results.append({
                "name": a.get("name"),
                "command": cmd,
                "expect_contains": pos,
                "expect_not_contains": neg,
                "expect_regex": regex,
                "passed": passed,
                "reason": reason if not passed else "",
                "output_tail": output[-1200:],
            })

        # cleanup (best effort; errors are logged, not fatal)
        for cmd in plan.get("cleanup_commands", []):
            try:
                send(cmd, wait=10)
            except Exception as e:
                evidence_lines.append(f"\n# cleanup failed for '{cmd}': {e}")

    finally:
        try:
            client.close()
        except Exception:
            pass

    passed_count = sum(1 for r in results if r["passed"])
    total = len(results)
    has_any_real = any(r["expect_contains"] or r["expect_not_contains"] or r["expect_regex"]
                       for r in results)
    if not has_any_real or total == 0:
        overall = "MANUAL_REVIEW"
    elif passed_count == total:
        overall = "PASS"
    elif passed_count == 0:
        overall = "FAIL"
    else:
        overall = "PARTIAL"

    verdict = {
        "bug_key": bug_key,
        "device": device,
        "timestamp": now_iso(),
        "device_version": version_text.strip()[-600:],
        "gitcommit": gitcommit,
        "overall": overall,
        "passed": passed_count,
        "total": total,
        "assertions": results,
    }

    with open(state_path(bug_key, "evidence.txt"), "w") as f:
        f.write("\n".join(evidence_lines))
    with open(state_path(bug_key, "gitcommit.txt"), "w") as f:
        f.write(gitcommit + "\n")
    write_json(state_path(bug_key, "verdict.json"), verdict)
    return verdict


# --------------------------------------------------------------------------- #
# Report — builds the Jira comment
# --------------------------------------------------------------------------- #

def build_comment(bug_key: str, build_url: str = "") -> str:
    verdict = read_json(state_path(bug_key, "verdict.json"))
    plan_path = state_path(bug_key, "plan.json")
    plan = read_json(plan_path) if os.path.exists(plan_path) else {}

    overall = verdict["overall"]
    head = {
        "PASS": f"(/) Bug verification PASSED — {bug_key}",
        "FAIL": f"(x) Bug still reproduces — {bug_key}",
        "PARTIAL": f"(!) Bug verification PARTIAL — {bug_key}",
        "MANUAL_REVIEW": f"(!) Bug verification needs MANUAL REVIEW — {bug_key}",
    }[overall]

    lines = [
        f"h3. {head}",
        "",
        f"*Verified by:* Bug Verifier tool (AI-assisted)",
        f"*Verification timestamp:* {verdict['timestamp']}",
        f"*Device:* {verdict['device']}",
        f"*Build under test:* {build_url or 'see deploy.log'}",
        f"*Git commit on device:* {{{{{verdict.get('gitcommit', 'n/a')}}}}}",
        "",
        "h4. Device version",
        "{code}",
        verdict.get("device_version", ""),
        "{code}",
        "",
        "h4. Assertions",
        "|| # || Name || Result || Command || Reason ||",
    ]
    for i, r in enumerate(verdict["assertions"], 1):
        mark = "(/)" if r["passed"] else "(x)"
        reason = r["reason"] or "-"
        cmd = (r["command"] or "").replace("|", "\\|")
        name = (r["name"] or "").replace("|", "\\|")
        lines.append(f"| {i} | {name} | {mark} | {{{{{cmd}}}}} | {reason} |")

    # evidence tail
    evidence_path = state_path(bug_key, "evidence.txt")
    if os.path.exists(evidence_path):
        with open(evidence_path) as f:
            tail = f.read()[-3500:]
        lines += ["", "h4. Evidence (tail)", "{code}", tail, "{code}"]

    if overall == "FAIL":
        lines += [
            "",
            "This build does *not* resolve the reported defect. "
            "Proposing to reopen the ticket for re-investigation.",
        ]
    elif overall == "PASS":
        lines += [
            "",
            "All assertions matched the expected results on the build under test.",
        ]
    elif overall == "PARTIAL":
        failed = [r["name"] for r in verdict["assertions"] if not r["passed"]]
        lines += ["", f"Partially fixed. Failing assertions: {', '.join(failed)}"]
    else:
        lines += ["", "Plan could not be auto-derived with enough confidence. "
                      "Review the plan.json and re-run verify."]

    text = "\n".join(lines)
    with open(state_path(bug_key, "proposed_comment.txt"), "w") as f:
        f.write(text)
    return text


# --------------------------------------------------------------------------- #
# Subcommand handlers
# --------------------------------------------------------------------------- #

def cmd_fetch(args):
    if args.from_file:
        data = json.load(open(args.from_file))
        write_json(state_path(args.bug, "bug.json"), data)
        print(f"Loaded bug from {args.from_file} -> {state_path(args.bug, 'bug.json')}")
        return 0
    client = JiraClient()
    if not client.available():
        print("ERROR: No Jira credentials. Either:", file=sys.stderr)
        print("  - Set JIRA_EMAIL + JIRA_API_TOKEN env vars", file=sys.stderr)
        print("  - Create ~/.jira_token with {email, token, [base_url]}", file=sys.stderr)
        print("  - Or use --from-file path/to/issue.json "
              "(e.g. from the Atlassian MCP)", file=sys.stderr)
        return 2
    issue = client.get_issue(args.bug)
    write_json(state_path(args.bug, "bug.json"), issue)
    print(f"Fetched {args.bug} -> {state_path(args.bug, 'bug.json')}")
    print(f"Summary: {issue.get('fields', {}).get('summary')}")
    return 0


def cmd_plan(args):
    issue = read_json(state_path(args.bug, "bug.json"))
    plan = build_plan(issue)
    write_json(state_path(args.bug, "plan.json"), plan)
    print(f"Plan written to {state_path(args.bug, 'plan.json')}")
    print(f"Confidence: {plan['confidence']}  "
          f"(needs_human_review={plan['needs_human_review']})")
    print(f"Setup commands:       {len(plan['setup_commands'])}")
    print(f"Reproduction commands:{len(plan['reproduction_commands'])}")
    print(f"Assertions:           {len(plan['assertions'])}")
    if plan["needs_human_review"]:
        print("\n  ! Please review plan.json before running verify.")
        print("    Fill any empty expect_contains/expect_not_contains fields.")
    if args.edit:
        editor = os.environ.get("EDITOR", "vi")
        subprocess.call([editor, state_path(args.bug, "plan.json")])
    return 0


def cmd_deploy(args):
    rc = do_deploy(args.bug, args.device, args.build, args.mode)
    if rc != 0:
        print(f"Deploy exited with {rc}", file=sys.stderr)
    return rc


def cmd_verify(args):
    verdict = run_verification(args.bug, args.device)
    print(f"\nOVERALL: {verdict['overall']}  "
          f"({verdict['passed']}/{verdict['total']} assertions passed)")
    print(f"Verdict:  {state_path(args.bug, 'verdict.json')}")
    print(f"Evidence: {state_path(args.bug, 'evidence.txt')}")
    return 0 if verdict["overall"] in ("PASS",) else 1


def cmd_report(args):
    text = build_comment(args.bug, build_url=args.build or "")
    print(text)
    print(f"\n(comment saved to {state_path(args.bug, 'proposed_comment.txt')})")
    return 0


def cmd_run(args):
    rc = cmd_fetch(args) if args.from_file or JiraClient().available() else 0
    if os.path.exists(state_path(args.bug, "bug.json")):
        cmd_plan(args)
    rc = do_deploy(args.bug, args.device, args.build, args.mode)
    if rc != 0:
        print("Deploy failed — skipping verify.", file=sys.stderr)
        return rc
    run_verification(args.bug, args.device)
    text = build_comment(args.bug, build_url=args.build)
    print("\n" + text)
    return 0


def cmd_comment(args):
    if not os.path.exists(state_path(args.bug, "verdict.json")):
        print("No verdict.json — run verify first.", file=sys.stderr)
        return 2
    build_url = args.build or ""
    text = build_comment(args.bug, build_url=build_url)
    print(text)

    if not args.confirm:
        print("\n(dry-run; re-run with --confirm to post to Jira)")
        return 0

    client = JiraClient()
    if not client.available():
        print("Jira credentials unavailable — cannot post.", file=sys.stderr)
        return 2

    verdict = read_json(state_path(args.bug, "verdict.json"))

    print("\nPosting comment to Jira...")
    client.add_comment(args.bug, text)
    print("Comment posted.")

    transition = args.transition
    if transition == "none":
        return 0
    if transition == "auto":
        transition = "reopen" if verdict["overall"] == "FAIL" else "none"
    if transition == "none":
        return 0

    wanted_names = {
        "reopen":   ["Reopen", "Reopened", "Reopen Issue", "In Progress", "Open"],
        "verified": ["Verified", "Verify", "Close", "Closed", "Resolve", "Resolved",
                     "Done"],
    }.get(transition, [])
    if not wanted_names:
        print(f"Unknown transition '{transition}' — skipping.")
        return 0

    tr_resp = client.get_transitions(args.bug)
    available = tr_resp.get("transitions", [])
    print("\nAvailable transitions:")
    for t in available:
        print(f"  - id={t['id']}  name='{t['name']}'  to='{t.get('to', {}).get('name')}'")
    picked = None
    for name in wanted_names:
        for t in available:
            if t["name"].lower() == name.lower() or t.get("to", {}).get("name", "").lower() == name.lower():
                picked = t
                break
        if picked:
            break
    if not picked:
        print(f"No transition matching {wanted_names} available — leaving ticket as-is.")
        return 0

    print(f"Transitioning via id={picked['id']} name='{picked['name']}'...")
    client.transition(args.bug, picked["id"])
    print("Done.")
    return 0


# --------------------------------------------------------------------------- #
# Argparse
# --------------------------------------------------------------------------- #

def main():
    p = argparse.ArgumentParser(prog="bug_verifier",
                                description="QA-engineer-in-a-box for Jira bugs.")
    sub = p.add_subparsers(dest="cmd", required=True)

    pf = sub.add_parser("fetch", help="Fetch bug from Jira (or load JSON from disk).")
    pf.add_argument("bug")
    pf.add_argument("--from-file", help="Load issue JSON from disk instead of Jira REST.")
    pf.set_defaults(func=cmd_fetch)

    pp = sub.add_parser("plan", help="Parse bug into a verification plan.json.")
    pp.add_argument("bug")
    pp.add_argument("--edit", action="store_true", help="Open plan.json in $EDITOR after writing.")
    pp.set_defaults(func=cmd_plan)

    pd = sub.add_parser("deploy", help="Load a Jenkins fix build onto the device.")
    pd.add_argument("bug")
    pd.add_argument("--device", required=True, help="Device hostname or IP.")
    pd.add_argument("--build", required=True, help="Jenkins build URL.")
    pd.add_argument("--mode", required=True,
                    choices=["upgrade", "deploy", "deploy-with-config", "skip"])
    pd.set_defaults(func=cmd_deploy)

    pv = sub.add_parser("verify", help="Run plan assertions on the device.")
    pv.add_argument("bug")
    pv.add_argument("--device", required=True)
    pv.set_defaults(func=cmd_verify)

    pr = sub.add_parser("report", help="Print the proposed Jira comment.")
    pr.add_argument("bug")
    pr.add_argument("--build", default="", help="Jenkins build URL to include.")
    pr.set_defaults(func=cmd_report)

    pn = sub.add_parser("run", help="Fetch→plan→deploy→verify→report end-to-end.")
    pn.add_argument("bug")
    pn.add_argument("--device", required=True)
    pn.add_argument("--build", required=True)
    pn.add_argument("--mode", required=True,
                    choices=["upgrade", "deploy", "deploy-with-config", "skip"])
    pn.add_argument("--from-file")
    pn.set_defaults(func=cmd_run)

    pc = sub.add_parser("comment", help="Post the verdict comment (and optional transition).")
    pc.add_argument("bug")
    pc.add_argument("--build", default="")
    pc.add_argument("--transition", default="auto",
                    choices=["auto", "reopen", "verified", "none"],
                    help="'auto' = reopen on FAIL, none otherwise.")
    pc.add_argument("--confirm", action="store_true",
                    help="Actually post (without this, prints the comment only).")
    pc.set_defaults(func=cmd_comment)

    args = p.parse_args()
    rc = args.func(args)
    sys.exit(rc if isinstance(rc, int) else 0)


if __name__ == "__main__":
    main()
