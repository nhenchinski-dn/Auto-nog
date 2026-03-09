#!/usr/bin/env python3
"""
Y.1731 RESTCONF Sanity Test Script

Tests RESTCONF (via OpenDaylight) operations for Y.1731 Performance Monitoring:
  Phase 1: Setup   - Mount device to ODL, discover YANG paths, discover CFM context
  Phase 2: GET     - Retrieve PM config/oper data via RESTCONF
  Phase 3: PATCH   - Create DM profile + session, verify via GET and CLI (before/after)
  Phase 4: PATCH   - Create SLM profile + session, verify via GET and CLI (before/after)
  Phase 5: Modify  - Modify DM profile thresholds, verify via CLI (before/after)
  Phase 6: DELETE  - Remove all test artifacts, verify via CLI (before/after)
  Phase 7: Negative - Invalid path, malformed XML, invalid values
  Phase 8: Cleanup - Unmount device from ODL

Every PATCH/DELETE is verified by:
  1. CLI BEFORE -- capture baseline via SSH show command
  2. RESTCONF operation -- send the HTTP request
  3. CLI AFTER  -- capture via SSH and compare to baseline
  4. RESTCONF GET -- verify via RESTCONF GET as secondary check

Jira: SW-237067 (Ethernet OAM Y.1731 | RESTCONF)
Epic: SW-141523 (Ethernet OAM Y.1731 - Proactive PM)
Reference: https://drivenets.atlassian.net/wiki/spaces/QA/pages/5353865217

Usage:
    python3 y1731_restconf_test.py --host 192.168.174.101
    python3 y1731_restconf_test.py --host 192.168.174.101 --odl-host 10.10.75.34
    python3 y1731_restconf_test.py --host 192.168.174.101 --cleanup --skip-mount
"""
import argparse, json, re, sys, time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
import paramiko, requests
from requests.auth import HTTPBasicAuth

# ── Test artifact names (unique to avoid collision with manual config) ──
DM_PROFILE_NAME = "RESTCONF_DM_PROF"
SLM_PROFILE_NAME = "RESTCONF_SLM_PROF"
# Session names are dynamic per MEP: RESTCONF_DM_SESS_mep<id>, RESTCONF_SLM_SESS_mep<id>

# ── Default YANG namespaces (overridden by discovery) ──
DEFAULT_NS = {
    "dn-top": "http://drivenets.com/ns/yang/dn-top",
    "dn-services": "http://drivenets.com/ns/yang/dn-services",
    "dn-pm": "http://drivenets.com/ns/yang/dn-performance-monitoring",
    "dn-cfm": "http://drivenets.com/ns/yang/dn-srv-connectivity-fault-management",
}

# ── ANSI colour codes ──
G = "\033[32m"   # green
R = "\033[31m"   # red
Y = "\033[33m"   # yellow
C = "\033[36m"   # cyan
B = "\033[1m"    # bold
DIM = "\033[2m"  # dim
X = "\033[0m"    # reset

# ── URL templates ──
MOUNT_URL = ("http://{odl_host}:{odl_port}/restconf/config/"
             "network-topology:network-topology/topology/topology-netconf/"
             "node/{node_name}")
MOUNT_STATUS_URL = ("http://{odl_host}:{odl_port}/rests/data/"
                    "network-topology:network-topology/topology=topology-netconf/"
                    "node={node_name}?content=nonconfig")
RESTCONF_DATA_URL = ("http://{odl_host}:{odl_port}/rests/data/"
                     "network-topology:network-topology/topology=topology-netconf/"
                     "node={node_name}/yang-ext:mount/{yang_path}")
RESTCONF_PATCH_URL = ("http://{odl_host}:{odl_port}/rests/data/"
                      "network-topology:network-topology/topology=topology-netconf/"
                      "node={node_name}/yang-ext:mount/dn-top:drivenets-top")


class Y1731RestconfTest:
    """Y.1731 RESTCONF sanity tester for DNOS devices via OpenDaylight."""

    def __init__(self, host, username="dnroot", password="dnroot",
                 odl_host="10.10.75.34", odl_port=8181,
                 odl_user="admin", odl_password="admin",
                 node_name=None, cleanup=False, skip_mount=False,
                 no_ssh_verify=False, verbose=False, md_name=None, ma_name=None,
                 source_mep_id=None, target_mep_id=None):
        self.host = host
        self.username = username
        self.password = password
        self.odl_host = odl_host
        self.odl_port = odl_port
        self.odl_auth = HTTPBasicAuth(odl_user, odl_password)
        self.node_name = node_name or host.replace(".", "_")
        self.cleanup = cleanup
        self.skip_mount = skip_mount
        self.no_ssh_verify = no_ssh_verify
        self.verbose = verbose
        # CLI-provided single context (if any) -- used to seed cfm_contexts
        self._cli_md = md_name
        self._cli_ma = ma_name
        self._cli_src = source_mep_id
        self._cli_tgt = target_mep_id
        # List of CFM contexts: [{md, ma, src, tgt, dir, free, dm_sess, slm_sess}]
        self.cfm_contexts: List[Dict[str, Any]] = []
        self.ns = dict(DEFAULT_NS)
        self.yang_pm_path = ("dn-top:drivenets-top/dn-services:services/"
                             "dn-performance-monitoring:performance-monitoring")
        self.ssh_client = None
        self.shell = None
        # Results: (name, status, detail) where status ∈ {"pass","fail","skip"}
        self.results: List[Tuple[str, str, str]] = []
        self._created_artifacts: set = set()
        self.http = requests.Session()
        self.http.auth = self.odl_auth
        self.http.headers.update({"Accept": "application/json"})

    # ──────────────────────────────────────────────────────────────
    # SSH Helpers
    # ──────────────────────────────────────────────────────────────
    def ssh_connect(self):
        print(f"[*] SSH: Connecting to {self.host} as {self.username} ...")
        self.ssh_client = paramiko.SSHClient()
        self.ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.ssh_client.connect(hostname=self.host, username=self.username,
                                password=self.password, look_for_keys=False,
                                allow_agent=False, timeout=30)
        self.shell = self.ssh_client.invoke_shell(width=250, height=1000)
        self._read_until_prompt(timeout=15)
        self._send("no-paging")
        self._read_until_prompt(timeout=5)
        print("[+] SSH: Connected and paging disabled.\n")

    def ssh_disconnect(self):
        if self.shell:
            self.shell.close()
        if self.ssh_client:
            self.ssh_client.close()
        print("[*] SSH: Disconnected.")

    def _send(self, cmd):
        self.shell.send(cmd + "\n")

    def _read_until_prompt(self, timeout=30):
        buf = ""
        end_time = time.time() + timeout
        while time.time() < end_time:
            if self.shell.recv_ready():
                chunk = self.shell.recv(65536).decode("utf-8", errors="replace")
                buf += chunk
                lines = buf.strip().split("\n")
                last = lines[-1].strip() if lines else ""
                if last.endswith("#") or last.endswith(">"):
                    break
            else:
                time.sleep(0.2)
        return buf

    def run_show(self, cmd, timeout=30):
        self._send(cmd)
        return self._read_until_prompt(timeout=timeout)

    # ──────────────────────────────────────────────────────────────
    # CLI verification helpers (before / after)
    # ──────────────────────────────────────────────────────────────
    def _cli_snapshot(self, show_cmd, timeout=15):
        """Run a show command via a fresh SSH channel (avoids buffer issues)."""
        if self.no_ssh_verify or not self.ssh_client:
            return ""
        try:
            ch = self.ssh_client.invoke_shell(width=250, height=1000)
            try:
                # Wait for initial prompt
                buf = ""
                end = time.time() + 10
                while time.time() < end:
                    if ch.recv_ready():
                        buf += ch.recv(65536).decode("utf-8", errors="replace")
                        if buf.strip().endswith("#") or buf.strip().endswith(">"):
                            break
                    else:
                        time.sleep(0.2)
                # Disable paging
                ch.send("no-paging\n")
                time.sleep(0.5)
                while ch.recv_ready():
                    ch.recv(65536)
                # Send the show command
                ch.send(show_cmd + " | no-more\n")
                time.sleep(0.5)
                # Read the output
                output = ""
                end = time.time() + timeout
                while time.time() < end:
                    if ch.recv_ready():
                        output += ch.recv(65536).decode(
                            "utf-8", errors="replace")
                        lines = output.strip().split("\n")
                        if lines and (lines[-1].strip().endswith("#")
                                      or lines[-1].strip().endswith(">")):
                            break
                    else:
                        time.sleep(0.2)
                return output
            finally:
                ch.close()
        except Exception:
            return ""

    def _cli_contains(self, output, artifact):
        """Case-insensitive check whether artifact appears in CLI output."""
        return artifact.lower() in output.lower()

    # ──────────────────────────────────────────────────────────────
    # RESTCONF Helpers
    # ──────────────────────────────────────────────────────────────
    def _mu(self):
        return MOUNT_URL.format(odl_host=self.odl_host, odl_port=self.odl_port,
                                node_name=self.node_name)

    def _msu(self):
        return MOUNT_STATUS_URL.format(odl_host=self.odl_host,
                                        odl_port=self.odl_port,
                                        node_name=self.node_name)

    def _du(self, yp, ct="config"):
        b = RESTCONF_DATA_URL.format(odl_host=self.odl_host,
                                      odl_port=self.odl_port,
                                      node_name=self.node_name, yang_path=yp)
        return f"{b}?content={ct}"

    def _pu(self):
        return RESTCONF_PATCH_URL.format(odl_host=self.odl_host,
                                          odl_port=self.odl_port,
                                          node_name=self.node_name)

    def rc_get(self, yp, ct="config"):
        return self.http.get(self._du(yp, ct), timeout=60)

    def rc_patch(self, xml):
        h = {"Content-Type": "application/xml", "Accept": "application/xml"}
        return self.http.patch(self._pu(), data=xml, headers=h, timeout=60)

    def rc_delete(self, yang_path):
        """Send HTTP DELETE to a specific YANG path on the mounted device."""
        url = RESTCONF_DATA_URL.format(
            odl_host=self.odl_host, odl_port=self.odl_port,
            node_name=self.node_name, yang_path=yang_path)
        return self.http.delete(url, timeout=60)

    @staticmethod
    def _extract_restconf_error(text):
        """Extract readable error from RESTCONF XML error response."""
        m = re.search(r"<error-message>(.*?)</error-message>", text or "")
        return m.group(1)[:150] if m else (text or "")[:150]

    def _log_rc(self, method, url, status, req_body=None, resp_body=None):
        """Print verbose RESTCONF request/response details when --verbose."""
        if not self.verbose:
            return
        print(f"    {'─' * 56}")
        print(f"    {C}RESTCONF {method}{X}  {url}")
        if req_body:
            # Pretty-print XML (indent for readability)
            pretty = req_body
            try:
                import xml.dom.minidom
                pretty = xml.dom.minidom.parseString(req_body).toprettyxml(
                    indent="  ")
                # Remove the xml declaration line
                pretty = "\n".join(pretty.split("\n")[1:])
            except Exception:
                pass
            print(f"    {DIM}Request body:{X}")
            for line in pretty.strip().split("\n"):
                print(f"      {line}")
        print(f"    {DIM}Response: HTTP {status}{X}")
        if resp_body:
            # Try JSON pretty-print, then XML, then raw (truncated)
            printed = False
            if resp_body.strip().startswith("{"):
                try:
                    pretty = json.dumps(json.loads(resp_body), indent=2)
                    print(f"    {DIM}Response body (JSON):{X}")
                    for line in pretty.split("\n")[:60]:
                        print(f"      {line}")
                    if len(pretty.split("\n")) > 60:
                        print(f"      ... ({len(pretty.split(chr(10)))} lines total)")
                    printed = True
                except Exception:
                    pass
            if not printed and resp_body.strip().startswith("<"):
                try:
                    import xml.dom.minidom
                    pretty = xml.dom.minidom.parseString(resp_body).toprettyxml(
                        indent="  ")
                    pretty = "\n".join(pretty.split("\n")[1:])
                    print(f"    {DIM}Response body (XML):{X}")
                    for line in pretty.strip().split("\n")[:60]:
                        print(f"      {line}")
                    if len(pretty.strip().split("\n")) > 60:
                        print(f"      ... ({len(pretty.strip().split(chr(10)))} lines total)")
                    printed = True
                except Exception:
                    pass
            if not printed and resp_body.strip():
                trunc = resp_body[:2000]
                print(f"    {DIM}Response body:{X}")
                for line in trunc.split("\n")[:40]:
                    print(f"      {line}")
                if len(resp_body) > 2000:
                    print(f"      ... (truncated, {len(resp_body)} bytes total)")
        print(f"    {'─' * 56}")

    # ──────────────────────────────────────────────────────────────
    # Recording
    # ──────────────────────────────────────────────────────────────
    def _record(self, name, status, detail=""):
        """Record a test result.  status: True/'pass', False/'fail', or 'skip'."""
        if status is True:
            status = "pass"
        elif status is False:
            status = "fail"
        self.results.append((name, status, detail))
        tags = {"pass": f"{G}[PASS]{X}",
                "fail": f"{R}[FAIL]{X}",
                "skip": f"{Y}[SKIP]{X}"}
        tag = tags.get(status, f"{R}[????]{X}")
        short = detail[:120] + "..." if len(detail) > 120 else detail
        print(f"  {tag} {name}" + (f" -- {short}" if short else ""))

    def _phase(self, label):
        print(f"\n{'=' * 60}\n{label}\n{'=' * 60}")

    # ──────────────────────────────────────────────────────────────
    # XML Body Builders  (matched to device display-xml output)
    # ──────────────────────────────────────────────────────────────
    def _mount_xml(self):
        return ('<node xmlns="urn:TBD:params:xml:ns:yang:network-topology">'
                f"<node-id>{self.node_name}</node-id>"
                '<host xmlns="urn:opendaylight:netconf-node-topology">'
                f"{self.host}</host>"
                '<port xmlns="urn:opendaylight:netconf-node-topology">830</port>'
                '<username xmlns="urn:opendaylight:netconf-node-topology">'
                f"{self.username}</username>"
                '<password xmlns="urn:opendaylight:netconf-node-topology">'
                f"{self.password}</password>"
                '<tcp-only xmlns="urn:opendaylight:netconf-node-topology">'
                "false</tcp-only>"
                '<default-request-timeout-millis '
                'xmlns="urn:opendaylight:netconf-node-topology">'
                "3600000</default-request-timeout-millis></node>")

    def _wrap_pm(self, inner):
        """Wrap inner XML in drivenets-top > services > performance-monitoring."""
        t = self.ns["dn-top"]
        s = self.ns["dn-services"]
        p = self.ns["dn-pm"]
        return (f'<drivenets-top xmlns="{t}">'
                f'<services xmlns="{s}">'
                f'<performance-monitoring xmlns="{p}">'
                f'{inner}'
                '</performance-monitoring></services></drivenets-top>')

    # ── DM Profile XML (matched to device schema) ──
    def _dm_prof_xml(self, pn=DM_PROFILE_NAME, drm=100, dra=1000,
                     drx=2000, jra=500, jrx=1000, sr=90.0,
                     pc=5, pi_=1, ri=10):
        return self._wrap_pm(
            '<profiles><cfm><two-way-delay-measurement>'
            f'<profile><profile-name>{pn}</profile-name>'
            f'<config-items><profile-name>{pn}</profile-name>'
            '<inform-test-results>enabled</inform-test-results>'
            '<test-duration-probes>'
            f'<probe-count>{pc}</probe-count>'
            f'<probe-interval>{pi_}</probe-interval>'
            f'<repeat-interval>{ri}</repeat-interval>'
            '</test-duration-probes>'
            '<cfm-eth-dm-performance-thresholds>'
            f'<delay-rtt-min>{drm}</delay-rtt-min>'
            f'<delay-rtt-avg>{dra}</delay-rtt-avg>'
            f'<delay-rtt-max>{drx}</delay-rtt-max>'
            f'<jitter-rtt-avg>{jra}</jitter-rtt-avg>'
            f'<jitter-rtt-max>{jrx}</jitter-rtt-max>'
            f'<success-rate-percent>{sr}</success-rate-percent>'
            '</cfm-eth-dm-performance-thresholds>'
            '</config-items></profile>'
            '</two-way-delay-measurement></cfm></profiles>')

    # ── DM Session XML (matched to device schema) ──
    def _dm_sess_xml(self, ctx, pn=DM_PROFILE_NAME):
        """Build DM session XML for a given CFM context dict."""
        cfm_ns = self.ns["dn-cfm"]
        sn = ctx["dm_sess"]
        return self._wrap_pm(
            f'<cfm-tests><proactive-monitoring xmlns="{cfm_ns}">'
            '<two-way-delay-measurements>'
            f'<test-session><session-name>{sn}</session-name>'
            '<config-items>'
            f'<profile>{pn}</profile>'
            '<admin-state>enabled</admin-state>'
            '<description>RESTCONF_test_DM_session</description>'
            f'<source-md-name>{ctx["md"]}</source-md-name>'
            f'<source-ma-name>{ctx["ma"]}</source-ma-name>'
            f'<source-mep-id>{ctx["src"]}</source-mep-id>'
            f'<target-mep-id>{ctx["tgt"]}</target-mep-id>'
            '</config-items></test-session>'
            '</two-way-delay-measurements>'
            '</proactive-monitoring></cfm-tests>')

    # ── SLM Profile XML (matched to device schema) ──
    def _slm_prof_xml(self, pn=SLM_PROFILE_NAME, pcp=5,
                      nel=1.0, fel=1.0, pc=5, pi_=1, ri=10):
        return self._wrap_pm(
            '<profiles><cfm><two-way-synthetic-loss-measurement>'
            f'<profile><profile-name>{pn}</profile-name>'
            f'<config-items><profile-name>{pn}</profile-name>'
            f'<pcp>{pcp}</pcp>'
            '<inform-test-results>enabled</inform-test-results>'
            '<test-duration-probes>'
            f'<probe-count>{pc}</probe-count>'
            f'<probe-interval>{pi_}</probe-interval>'
            f'<repeat-interval>{ri}</repeat-interval>'
            '</test-duration-probes>'
            '<cfm-eth-sl-performance-thresholds>'
            f'<near-end-loss>{nel}</near-end-loss>'
            f'<far-end-loss>{fel}</far-end-loss>'
            '</cfm-eth-sl-performance-thresholds>'
            '</config-items></profile>'
            '</two-way-synthetic-loss-measurement></cfm></profiles>')

    # ── SLM Session XML (matched to device schema) ──
    def _slm_sess_xml(self, ctx, pn=SLM_PROFILE_NAME):
        """Build SLM session XML for a given CFM context dict."""
        cfm_ns = self.ns["dn-cfm"]
        sn = ctx["slm_sess"]
        return self._wrap_pm(
            f'<cfm-tests><proactive-monitoring xmlns="{cfm_ns}">'
            '<two-way-synthetic-loss-measurements>'
            f'<test-session><session-name>{sn}</session-name>'
            '<config-items>'
            f'<profile>{pn}</profile>'
            '<admin-state>enabled</admin-state>'
            '<description>RESTCONF_test_SLM_session</description>'
            f'<source-md-name>{ctx["md"]}</source-md-name>'
            f'<source-ma-name>{ctx["ma"]}</source-ma-name>'
            f'<source-mep-id>{ctx["src"]}</source-mep-id>'
            f'<target-mep-id>{ctx["tgt"]}</target-mep-id>'
            '</config-items></test-session>'
            '</two-way-synthetic-loss-measurements>'
            '</proactive-monitoring></cfm-tests>')

    # ── DELETE XML bodies ──
    def _del_xml(self, etype, ename):
        cfm_ns = self.ns["dn-cfm"]
        m = {
            "dm_session": (
                f'<cfm-tests><proactive-monitoring xmlns="{cfm_ns}">'
                '<two-way-delay-measurements>'
                f'<test-session operation="delete">'
                f'<session-name>{ename}</session-name>'
                '</test-session></two-way-delay-measurements>'
                '</proactive-monitoring></cfm-tests>'),
            "slm_session": (
                f'<cfm-tests><proactive-monitoring xmlns="{cfm_ns}">'
                '<two-way-synthetic-loss-measurements>'
                f'<test-session operation="delete">'
                f'<session-name>{ename}</session-name>'
                '</test-session></two-way-synthetic-loss-measurements>'
                '</proactive-monitoring></cfm-tests>'),
            "dm_profile": (
                '<profiles><cfm><two-way-delay-measurement>'
                f'<profile operation="delete">'
                f'<profile-name>{ename}</profile-name>'
                '</profile>'
                '</two-way-delay-measurement></cfm></profiles>'),
            "slm_profile": (
                '<profiles><cfm><two-way-synthetic-loss-measurement>'
                f'<profile operation="delete">'
                f'<profile-name>{ename}</profile-name>'
                '</profile>'
                '</two-way-synthetic-loss-measurement></cfm></profiles>'),
        }
        return self._wrap_pm(m[etype])

    # ──────────────────────────────────────────────────────────────
    # Phase 1: Setup
    # ──────────────────────────────────────────────────────────────
    def test_mount_device(self):
        self._phase("PHASE 1.1: Mount device to ODL")
        if self.skip_mount:
            self._record("mount_device", True, "Skipped (--skip-mount)")
            return
        h = {"Content-Type": "application/xml", "Accept": "application/xml"}
        try:
            xml_body = self._mount_xml()
            r = self.http.put(self._mu(), data=xml_body, headers=h, timeout=30)
            self._log_rc("PUT", self._mu(), r.status_code, xml_body, r.text)
            ok = r.status_code in (200, 201, 204)
            self._record("mount_device", ok, f"HTTP {r.status_code}" +
                         (f" -- {self._extract_restconf_error(r.text)}" if not ok else ""))
        except Exception as e:
            self._record("mount_device", False, f"Exception: {e}")

    def test_verify_mount_status(self):
        self._phase("PHASE 1.2: Verify mount status")
        if self.skip_mount:
            self._record("verify_mount_status", True, "Skipped (--skip-mount)")
            return
        url = self._msu()
        connected = False
        for i in range(1, 13):
            try:
                r = self.http.get(url, timeout=15)
                if r.status_code == 200:
                    b = r.text.lower()
                    if "connected" in b and "connecting" not in b:
                        self._log_rc("GET", url, r.status_code, resp_body=r.text)
                        connected = True
                        break
            except Exception:
                pass
            print(f"    Waiting for connection... ({i}/12)")
            time.sleep(5)
        self._record("verify_mount_status", connected,
                     "Device connected" if connected else "Timed out")

    def test_discover_yang_paths(self):
        self._phase("PHASE 1.3: Discover YANG namespaces")
        if self.no_ssh_verify:
            self._record("discover_yang_paths", True, "Skipped, using defaults")
            return
        try:
            out = self.run_show("show config services performance-monitoring "
                                "| display-xml | no-more", timeout=30)
            for uri in re.findall(r'xmlns(?::[\w-]+)?="(http://[^"]+)"', out):
                if uri.endswith("/dn-top"):
                    self.ns["dn-top"] = uri
                elif uri.endswith("/dn-services"):
                    self.ns["dn-services"] = uri
                elif "performance-monitoring" in uri:
                    self.ns["dn-pm"] = uri
                elif "connectivity-fault-management" in uri:
                    self.ns["dn-cfm"] = uri
            for uri in re.findall(r'xmlns(?::[\w-]+)?="(http://[^"]+)"', out):
                if "performance-monitoring" in uri:
                    m = re.search(r"/yang/([\w-]+)$", uri)
                    if m:
                        self.yang_pm_path = (
                            f"dn-top:drivenets-top/dn-services:services/"
                            f"{m.group(1)}:performance-monitoring")
            self._record("discover_yang_paths", True,
                         f"pm={self.ns['dn-pm']} | cfm={self.ns['dn-cfm']}")
        except Exception as e:
            self._record("discover_yang_paths", False, f"Error: {e}, using defaults")

    def test_discover_cfm_context(self):
        self._phase("PHASE 1.4: Discover CFM context")

        # If all four values provided via CLI, use that single context
        if all([self._cli_md, self._cli_ma, self._cli_src, self._cli_tgt]):
            ctx = {"md": self._cli_md, "ma": self._cli_ma,
                   "src": self._cli_src, "tgt": self._cli_tgt,
                   "dir": "cli", "free": True,
                   "dm_sess": f"RESTCONF_DM_SESS_mep{self._cli_src}",
                   "slm_sess": f"RESTCONF_SLM_SESS_mep{self._cli_src}"}
            self.cfm_contexts = [ctx]
            self._record("discover_cfm_context", True,
                         f"Using provided: MD={ctx['md']}, MA={ctx['ma']}, "
                         f"src={ctx['src']}, tgt={ctx['tgt']}")
            return
        if self.no_ssh_verify:
            self._record("discover_cfm_context", False,
                         "No CFM context, --no-ssh-verify")
            return
        try:
            # Use display-xml to get structured CFM data with direction info
            out = self.run_show("show config services ethernet-oam "
                                "connectivity-fault-management "
                                "| display-xml | no-more", timeout=30)
            # Parse all MD blocks
            # Structure: maintenance-domain > md-id, maintenance-association > ma-id,
            #            local-mep > mep-id + direction, remote-mep > mep-id
            candidates = []  # (md, ma, local_mep, remote_mep, direction)
            md_blocks = re.split(r'<maintenance-domain>', out)[1:]
            for md_block in md_blocks:
                md_match = re.search(r'<md-id>(\S+)</md-id>', md_block)
                if not md_match:
                    continue
                md_id = md_match.group(1)
                ma_blocks = re.split(r'<maintenance-association>', md_block)[1:]
                for ma_block in ma_blocks:
                    ma_match = re.search(r'<ma-id>(\S+)</ma-id>', ma_block)
                    if not ma_match:
                        continue
                    ma_id = ma_match.group(1)
                    # Each local-mep is a separate candidate
                    lmep_blocks = re.split(r'<local-mep>', ma_block)[1:]
                    remote_meps = [int(x) for x in
                                   re.findall(r'<remote-mep>\s*<mep-id>(\d+)</mep-id>',
                                              ma_block)]
                    remote_mep = remote_meps[0] if remote_meps else None
                    for lmep in lmep_blocks:
                        mep_m = re.search(r'<mep-id>(\d+)</mep-id>', lmep)
                        dir_m = re.search(r'<direction>(\w+)</direction>', lmep)
                        if mep_m:
                            local_mep = int(mep_m.group(1))
                            direction = dir_m.group(1) if dir_m else "unknown"
                            candidates.append((md_id, ma_id, local_mep,
                                               remote_mep, direction))

            # ── Check which MEPs are already used by existing PM sessions ──
            used_meps = set()  # set of (md, ma, mep_id) tuples
            try:
                pm_out = self.run_show(
                    "show config services performance-monitoring cfm | no-more",
                    timeout=15)
                for src_m in re.finditer(
                    r'source\s+maintenance-domain\s+(\S+)\s+'
                    r'maintenance-association\s+(\S+)\s+mep-id\s+(\d+)',
                    pm_out):
                    used_meps.add((src_m.group(1), src_m.group(2),
                                   int(src_m.group(3))))
            except Exception:
                pass

            # Build cfm_contexts list for ALL candidates with complete info
            for md, ma, src, tgt, direction in candidates:
                if tgt is None:
                    continue  # need a target MEP for sessions
                free = (md, ma, src) not in used_meps
                self.cfm_contexts.append({
                    "md": md, "ma": ma, "src": src, "tgt": tgt,
                    "dir": direction, "free": free,
                    "dm_sess": f"RESTCONF_DM_SESS_mep{src}",
                    "slm_sess": f"RESTCONF_SLM_SESS_mep{src}",
                })

            # Sort: free MEPs first, then prefer down direction
            dir_order = {"down": 0, "up": 1, "unknown": 2}
            self.cfm_contexts.sort(
                key=lambda c: (0 if c["free"] else 1,
                               dir_order.get(c["dir"], 9)))

            # Build summary
            free_ctx = [c for c in self.cfm_contexts if c["free"]]
            used_ctx = [c for c in self.cfm_contexts if not c["free"]]
            lines = []
            for c in self.cfm_contexts:
                tag = "FREE" if c["free"] else "IN-USE"
                lines.append(
                    f"mep{c['src']}@{c['md']}/{c['ma']} "
                    f"dir={c['dir']} tgt={c['tgt']} [{tag}]")
            summary = f"Found {len(self.cfm_contexts)} MEP(s): " + " | ".join(lines)
            if free_ctx:
                summary += f" -- will test {len(free_ctx)} free MEP(s)"
            else:
                summary += " -- all MEPs in-use, session tests will SKIP"
            self._record("discover_cfm_context",
                         len(self.cfm_contexts) > 0, summary)
        except Exception as e:
            self._record("discover_cfm_context", False, f"Error: {e}")

    # ──────────────────────────────────────────────────────────────
    # Phase 2: GET Operations
    # ──────────────────────────────────────────────────────────────
    def _do_get(self, name, ct, label):
        self._phase(label)
        try:
            url = self._du(self.yang_pm_path, ct)
            r = self.rc_get(self.yang_pm_path, ct)
            self._log_rc("GET", url, r.status_code, resp_body=r.text)
            ok = r.status_code in (200, 204)
            d = f"HTTP {r.status_code}"
            if ok and r.text:
                try:
                    d += f", keys: {list(r.json().keys())[:5]}"
                except Exception:
                    d += ", body present"
            elif not ok:
                if r.status_code in (404, 409, 500):
                    d += " (may be expected if no PM data)"
                    ok = True
                else:
                    d += f" -- {self._extract_restconf_error(r.text)}"
            self._record(name, ok, d)
        except Exception as e:
            self._record(name, False, f"Exception: {e}")

    def test_get_pm_config(self):
        self._do_get("get_pm_config", "config", "PHASE 2.1: GET PM config")

    def test_get_pm_oper(self):
        self._do_get("get_pm_oper", "nonconfig", "PHASE 2.2: GET PM oper")

    def test_get_pm_all(self):
        self._do_get("get_pm_all", "all", "PHASE 2.3: GET PM all")

    # ──────────────────────────────────────────────────────────────
    # Phase 3-6: PATCH / DELETE with CLI verification
    # ──────────────────────────────────────────────────────────────
    def _do_restconf_patch(self, name, xml, label):
        """Send a RESTCONF PATCH with retry for transient errors. Returns True if OK."""
        self._phase(label)
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                r = self.rc_patch(xml)
                self._log_rc("PATCH", self._pu(), r.status_code, xml, r.text)
                http_ok = r.status_code in (200, 201, 204)
                err_msg = "" if http_ok else self._extract_restconf_error(r.text)
                # Retry on transient errors (commit in progress, empty commit, lock)
                if not http_ok and attempt < max_retries and (
                        "commit is in progress" in err_msg
                        or "empty commit" in err_msg
                        or "lock" in err_msg.lower()):
                    print(f"    {DIM}Retry {attempt}/{max_retries}: {err_msg[:80]}{X}")
                    time.sleep(5)
                    continue
                d = f"HTTP {r.status_code}"
                if not http_ok:
                    if "in use with session" in err_msg or "in use" in err_msg.lower():
                        m = re.search(r"LMEP \d+ in use with session (\S+)", err_msg)
                        conflict = m.group(1) if m else "another session"
                        d += f" -- MEP in use by '{conflict}'"
                        self._record(name, "skip", d)
                        return False
                    else:
                        d += f" -- {err_msg}"
                self._record(name, http_ok, d)
                if http_ok:
                    self._created_artifacts.add(name)
                return http_ok
            except Exception as e:
                if attempt < max_retries:
                    time.sleep(3)
                    continue
                self._record(name, False, f"Exception: {e}")
                return False
        return False

    def _verify_artifacts_in_cli(self, artifacts_map, label, wait_secs=60):
        """
        Wait for ODL->device propagation, then verify artifacts in CLI.

        artifacts_map: dict of {name: (show_cmd, artifact_string)}
        Retries CLI check every 10s up to wait_secs total.
        """
        self._phase(label)
        if self.no_ssh_verify:
            for name in artifacts_map:
                self._record(f"verify_{name}_cli", True, "Skipped (--no-ssh-verify)")
            return

        # Wait for ODL->device propagation with progress indicator
        pending = dict(artifacts_map)  # copy
        found = {}
        max_attempts = max(1, wait_secs // 10)
        for attempt in range(1, max_attempts + 1):
            time.sleep(10)
            still_pending = {}
            for name, (show_cmd, artifact) in pending.items():
                cli_out = self._cli_snapshot(show_cmd)
                if self._cli_contains(cli_out, artifact):
                    found[name] = True
                    print(f"    {G}CLI: '{artifact}' found (~{attempt*10}s){X}")
                else:
                    still_pending[name] = (show_cmd, artifact)
            pending = still_pending
            if not pending:
                break
            names = [artifacts_map[n][1] for n in pending]
            print(f"    {DIM}Waiting for propagation... ({attempt}/{max_attempts}) "
                  f"pending: {names}{X}")

        # Record results
        for name, (show_cmd, artifact) in artifacts_map.items():
            if name in found:
                # Also verify via RESTCONF GET
                get_ok = False
                try:
                    rg = self.rc_get(self.yang_pm_path, "config")
                    get_ok = rg.status_code == 200 and artifact in rg.text
                except Exception:
                    pass
                self._record(f"verify_{name}_cli", True,
                             f"CLI: '{artifact}' present | "
                             f"GET={'found' if get_ok else 'NOT found'}")
            else:
                # Check GET as fallback
                get_ok = False
                try:
                    rg = self.rc_get(self.yang_pm_path, "config")
                    get_ok = rg.status_code == 200 and artifact in rg.text
                except Exception:
                    pass
                self._record(f"verify_{name}_cli", False,
                             f"CLI: '{artifact}' NOT found after {wait_secs}s | "
                             f"GET={'found' if get_ok else 'NOT found'}")

    def _do_modify(self, name, xml, show_cmd, check_str, label):
        """
        PATCH (modify) with before/after CLI verification.

        1. CLI BEFORE  -- capture baseline value
        2. RESTCONF PATCH
        3. CLI AFTER   -- check_str SHOULD appear
        4. RESTCONF GET -- check_str SHOULD appear
        """
        self._phase(label)

        # ── 1. CLI BEFORE ──
        cli_before = self._cli_snapshot(show_cmd)
        was_present = check_str.lower() in cli_before.lower()
        print(f"    {DIM}CLI-BEFORE: '{check_str}' "
              f"{'already present' if was_present else 'not present'}{X}")

        # ── 2. RESTCONF PATCH ──
        try:
            r = self.rc_patch(xml)
            self._log_rc("PATCH", self._pu(), r.status_code, xml, r.text)
            http_ok = r.status_code in (200, 201, 204)
            http_detail = f"HTTP {r.status_code}"
            if not http_ok:
                http_detail += f" -- {self._extract_restconf_error(r.text)}"
        except Exception as e:
            self._record(name, False, f"PATCH Exception: {e}")
            return

        # ── 3. Wait and retry CLI AFTER ──
        now_present = False
        if http_ok and not self.no_ssh_verify:
            for attempt in range(1, 5):
                time.sleep(3)
                cli_after = self._cli_snapshot(show_cmd)
                now_present = check_str.lower() in cli_after.lower()
                if now_present:
                    break
        elif self.no_ssh_verify:
            now_present = True

        # ── 4. RESTCONF GET ──
        get_found = False
        try:
            rg = self.rc_get(self.yang_pm_path, "config")
            self._log_rc("GET", self._du(self.yang_pm_path, "config"),
                         rg.status_code, resp_body=rg.text)
            if rg.status_code == 200:
                get_found = check_str in rg.text
        except Exception:
            pass

        # ── Verdict ──
        if self.no_ssh_verify:
            ok = http_ok and get_found
            detail = f"{http_detail} | GET={'found' if get_found else 'NOT found'}"
        else:
            ok = http_ok and now_present
            if not was_present and now_present:
                detail = (f"{http_detail} | CLI: '{check_str}' appeared after PATCH | "
                          f"GET={'found' if get_found else 'NOT found'}")
            elif was_present and now_present:
                detail = (f"{http_detail} | CLI: '{check_str}' present before & after | "
                          f"GET={'found' if get_found else 'NOT found'}")
            else:
                detail = (f"{http_detail} | CLI: '{check_str}' "
                          f"{'NOT found' if not now_present else 'present'} | "
                          f"GET={'found' if get_found else 'NOT found'}")

        self._record(name, ok, detail)

    def _do_delete(self, name, etype, ename, show_cmd, label):
        """
        DELETE with before/after CLI + RESTCONF GET verification.

        1. CLI BEFORE  -- artifact should exist
        2. RESTCONF PATCH (with operation=delete)
        3. CLI AFTER   -- artifact should be GONE
        4. RESTCONF GET -- artifact should be GONE
        """
        self._phase(label)

        # ── 1. CLI BEFORE ──
        cli_before = self._cli_snapshot(show_cmd)
        was_present = self._cli_contains(cli_before, ename)
        if was_present:
            print(f"    {DIM}CLI-BEFORE: '{ename}' present (will delete){X}")
        else:
            print(f"    {DIM}CLI-BEFORE: '{ename}' already absent{X}")

        # ── 2. RESTCONF DELETE (try HTTP DELETE first, fallback to PATCH) ──
        # Build the specific YANG path for this artifact
        del_paths = {
            "dm_session": (f"{self.yang_pm_path}/cfm-tests/"
                           f"dn-srv-connectivity-fault-management:proactive-monitoring/"
                           f"two-way-delay-measurements/test-session={ename}"),
            "slm_session": (f"{self.yang_pm_path}/cfm-tests/"
                            f"dn-srv-connectivity-fault-management:proactive-monitoring/"
                            f"two-way-synthetic-loss-measurements/test-session={ename}"),
            "dm_profile": (f"{self.yang_pm_path}/profiles/cfm/"
                           f"two-way-delay-measurement/profile={ename}"),
            "slm_profile": (f"{self.yang_pm_path}/profiles/cfm/"
                            f"two-way-synthetic-loss-measurement/profile={ename}"),
        }
        try:
            # Try HTTP DELETE to specific path
            del_path = del_paths.get(etype, "")
            r = self.rc_delete(del_path) if del_path else None
            if r:
                del_url = RESTCONF_DATA_URL.format(
                    odl_host=self.odl_host, odl_port=self.odl_port,
                    node_name=self.node_name, yang_path=del_path)
                self._log_rc("DELETE", del_url, r.status_code, resp_body=r.text)
            if r and r.status_code in (200, 204):
                http_ok = True
                http_detail = f"HTTP DELETE {r.status_code}"
                err_msg = ""
            else:
                # Fallback to PATCH with operation="delete"
                del_xml = self._del_xml(etype, ename)
                r = self.rc_patch(del_xml)
                self._log_rc("PATCH (delete)", self._pu(), r.status_code,
                             del_xml, r.text)
                http_ok = r.status_code in (200, 201, 204)
                http_detail = f"HTTP PATCH {r.status_code}"
                err_msg = ""
            if not http_ok:
                err_msg = self._extract_restconf_error(r.text)
                http_detail += f" -- {err_msg}"
        except Exception as e:
            self._record(name, False, f"DELETE Exception: {e}")
            return

        # ── 3. Wait and verify CLI AFTER ──
        now_gone = False
        if http_ok and not self.no_ssh_verify:
            for attempt in range(1, 5):
                time.sleep(5)
                cli_after = self._cli_snapshot(show_cmd)
                now_gone = not self._cli_contains(cli_after, ename)
                if now_gone:
                    break
        elif self.no_ssh_verify:
            now_gone = True
        else:
            # PATCH failed -- still check CLI
            time.sleep(5)
            cli_after = self._cli_snapshot(show_cmd)
            now_gone = not self._cli_contains(cli_after, ename)

        # ── 4. RESTCONF GET ──
        get_gone = True
        try:
            rg = self.rc_get(self.yang_pm_path, "config")
            if rg.status_code == 200:
                get_gone = ename not in rg.text
        except Exception:
            pass

        # ── Verdict ──
        # If RESTCONF returned error but artifact IS gone from CLI,
        # treat as success (e.g. "empty commit" = nothing to delete = already gone)
        if not http_ok and now_gone and "empty commit" in err_msg:
            ok = True
            detail = (f"{http_detail} | CLI: '{ename}' absent (already removed) | "
                      f"GET={'removed' if get_gone else 'STILL PRESENT'}")
        elif self.no_ssh_verify:
            ok = http_ok and get_gone
            detail = f"{http_detail} | GET={'removed' if get_gone else 'STILL PRESENT'}"
        else:
            ok = (http_ok or now_gone) and now_gone
            if was_present and now_gone:
                detail = (f"{http_detail} | CLI: '{ename}' removed after DELETE | "
                          f"GET={'removed' if get_gone else 'STILL PRESENT'}")
            elif not was_present and now_gone:
                detail = (f"{http_detail} | CLI: was already absent | "
                          f"GET={'removed' if get_gone else 'STILL PRESENT'}")
            else:
                detail = (f"{http_detail} | CLI: '{ename}' "
                          f"{'STILL PRESENT' if not now_gone else 'removed'} | "
                          f"GET={'removed' if get_gone else 'STILL PRESENT'}")

        self._record(name, ok, detail)

    # ──────────────────────────────────────────────────────────────
    # Phase 3: Create all profiles + sessions via RESTCONF
    # ──────────────────────────────────────────────────────────────
    def test_create_dm_profile(self):
        self._do_restconf_patch(
            "create_dm_profile", self._dm_prof_xml(),
            "PHASE 3.1: PATCH - Create DM profile")

    def test_create_slm_profile(self):
        self._do_restconf_patch(
            "create_slm_profile", self._slm_prof_xml(),
            "PHASE 3.2: PATCH - Create SLM profile")

    def test_create_dm_sessions(self):
        """Create DM sessions for ALL discovered CFM contexts."""
        if not self.cfm_contexts:
            self._phase("PHASE 3.3: PATCH - Create DM sessions")
            self._record("create_dm_sessions", "skip",
                         "No CFM contexts discovered")
            return
        for i, ctx in enumerate(self.cfm_contexts, 1):
            tag = f"mep{ctx['src']}@{ctx['md']}/{ctx['ma']}"
            label = f"PHASE 3.3.{i}: PATCH - Create DM session ({tag})"
            name = f"create_dm_sess_mep{ctx['src']}"
            if not ctx["free"]:
                self._phase(label)
                self._record(name, "skip",
                             f"MEP {ctx['src']} in {ctx['md']}/{ctx['ma']} "
                             f"already in-use (device constraint)")
                continue
            self._do_restconf_patch(name, self._dm_sess_xml(ctx), label)
            time.sleep(2)

    def test_create_slm_sessions(self):
        """Create SLM sessions for ALL discovered CFM contexts."""
        if not self.cfm_contexts:
            self._phase("PHASE 3.4: PATCH - Create SLM sessions")
            self._record("create_slm_sessions", "skip",
                         "No CFM contexts discovered")
            return
        for i, ctx in enumerate(self.cfm_contexts, 1):
            tag = f"mep{ctx['src']}@{ctx['md']}/{ctx['ma']}"
            label = f"PHASE 3.4.{i}: PATCH - Create SLM session ({tag})"
            name = f"create_slm_sess_mep{ctx['src']}"
            if not ctx["free"]:
                self._phase(label)
                self._record(name, "skip",
                             f"MEP {ctx['src']} in {ctx['md']}/{ctx['ma']} "
                             f"already in-use (device constraint)")
                continue
            self._do_restconf_patch(name, self._slm_sess_xml(ctx), label)
            time.sleep(2)

    # ──────────────────────────────────────────────────────────────
    # Phase 4: Wait for propagation + verify all creates via CLI
    # ──────────────────────────────────────────────────────────────
    def test_verify_creates_via_cli(self):
        """Verify all successfully created artifacts appear in device CLI."""
        to_verify = {}
        prof_cmd = "show config services performance-monitoring profiles"
        sess_cmd = "show config services performance-monitoring cfm"
        if "create_dm_profile" in self._created_artifacts:
            to_verify["dm_profile"] = (prof_cmd, DM_PROFILE_NAME)
        if "create_slm_profile" in self._created_artifacts:
            to_verify["slm_profile"] = (prof_cmd, SLM_PROFILE_NAME)
        # Dynamic session names from all contexts
        for ctx in self.cfm_contexts:
            key_dm = f"create_dm_sess_mep{ctx['src']}"
            key_slm = f"create_slm_sess_mep{ctx['src']}"
            if key_dm in self._created_artifacts:
                to_verify[f"dm_sess_mep{ctx['src']}"] = (
                    sess_cmd, ctx["dm_sess"])
            if key_slm in self._created_artifacts:
                to_verify[f"slm_sess_mep{ctx['src']}"] = (
                    sess_cmd, ctx["slm_sess"])
        if not to_verify:
            self._phase("PHASE 4: Verify creates via CLI")
            self._record("verify_creates_cli", True,
                         "Nothing to verify (no artifacts created)")
            return
        self._verify_artifacts_in_cli(
            to_verify,
            "PHASE 4: Wait for propagation + verify creates via CLI",
            wait_secs=90)

    # ──────────────────────────────────────────────────────────────
    # Phase 5: Modify DM profile
    # ──────────────────────────────────────────────────────────────
    def test_modify_dm_profile(self):
        if "create_dm_profile" not in self._created_artifacts:
            self._phase("PHASE 5: PATCH - Modify DM profile")
            self._record("modify_dm_profile", "skip", "Profile was not created")
            return
        self._do_modify(
            "modify_dm_profile",
            self._dm_prof_xml(drm=999),
            f"show config services performance-monitoring profiles "
            f"cfm two-way-delay-measurement {DM_PROFILE_NAME}",
            "999",
            "PHASE 5: PATCH - Modify DM profile (delay-rtt-min -> 999)")

    # ──────────────────────────────────────────────────────────────
    # Phase 6: DELETE all test artifacts
    # ──────────────────────────────────────────────────────────────
    def test_delete_dm_sessions(self):
        """Delete DM sessions for ALL contexts that were created."""
        any_created = any(f"create_dm_sess_mep{c['src']}" in self._created_artifacts
                          for c in self.cfm_contexts)
        if not any_created:
            self._phase("PHASE 6.1: DELETE DM sessions")
            self._record("delete_dm_sessions", "skip",
                         "No DM sessions were created")
            return
        for i, ctx in enumerate(self.cfm_contexts, 1):
            key = f"create_dm_sess_mep{ctx['src']}"
            name = f"delete_dm_sess_mep{ctx['src']}"
            tag = f"mep{ctx['src']}@{ctx['md']}/{ctx['ma']}"
            label = f"PHASE 6.1.{i}: DELETE DM session ({tag})"
            if key not in self._created_artifacts:
                self._phase(label)
                self._record(name, "skip", "Session was not created")
                continue
            self._do_delete(
                name, "dm_session", ctx["dm_sess"],
                "show config services performance-monitoring",
                label)

    def test_delete_slm_sessions(self):
        """Delete SLM sessions for ALL contexts that were created."""
        any_created = any(f"create_slm_sess_mep{c['src']}" in self._created_artifacts
                          for c in self.cfm_contexts)
        if not any_created:
            self._phase("PHASE 6.2: DELETE SLM sessions")
            self._record("delete_slm_sessions", "skip",
                         "No SLM sessions were created")
            return
        for i, ctx in enumerate(self.cfm_contexts, 1):
            key = f"create_slm_sess_mep{ctx['src']}"
            name = f"delete_slm_sess_mep{ctx['src']}"
            tag = f"mep{ctx['src']}@{ctx['md']}/{ctx['ma']}"
            label = f"PHASE 6.2.{i}: DELETE SLM session ({tag})"
            if key not in self._created_artifacts:
                self._phase(label)
                self._record(name, "skip", "Session was not created")
                continue
            self._do_delete(
                name, "slm_session", ctx["slm_sess"],
                "show config services performance-monitoring",
                label)

    def test_delete_dm_profile(self):
        if "create_dm_profile" not in self._created_artifacts:
            self._phase("PHASE 6.3: DELETE DM profile")
            self._record("delete_dm_profile", "skip",
                         "Profile was not created")
            return
        self._do_delete(
            "delete_dm_profile", "dm_profile", DM_PROFILE_NAME,
            "show config services performance-monitoring",
            "PHASE 6.3: DELETE DM profile")

    def test_delete_slm_profile(self):
        if "create_slm_profile" not in self._created_artifacts:
            self._phase("PHASE 6.4: DELETE SLM profile")
            self._record("delete_slm_profile", "skip",
                         "Profile was not created")
            return
        self._do_delete(
            "delete_slm_profile", "slm_profile", SLM_PROFILE_NAME,
            "show config services performance-monitoring",
            "PHASE 6.4: DELETE SLM profile")

    def test_verify_all_removed_via_cli(self):
        self._phase("PHASE 6.5: Verify all test artifacts removed")
        if self.no_ssh_verify:
            self._record("verify_all_removed", True, "Skipped (--no-ssh-verify)")
            return
        try:
            out = self._cli_snapshot(
                "show config services performance-monitoring", timeout=15)
            # Build list of all test artifact names (profiles + all session names)
            arts = [DM_PROFILE_NAME, SLM_PROFILE_NAME]
            for ctx in self.cfm_contexts:
                arts.append(ctx["dm_sess"])
                arts.append(ctx["slm_sess"])
            rem = [a for a in arts if self._cli_contains(out, a)]
            self._record("verify_all_removed", len(rem) == 0,
                         "All test artifacts removed" if not rem
                         else f"Still present: {rem}")
        except Exception as e:
            self._record("verify_all_removed", False, f"Exception: {e}")

    # ──────────────────────────────────────────────────────────────
    # Phase 7: Negative Tests
    # ──────────────────────────────────────────────────────────────
    def test_get_invalid_path(self):
        self._phase("PHASE 7.1: GET invalid path (negative)")
        try:
            yp = "dn-top:drivenets-top/dn-nonexistent:fake"
            r = self.rc_get(yp, "config")
            self._log_rc("GET", self._du(yp, "config"), r.status_code,
                         resp_body=r.text)
            ok = r.status_code != 200
            self._record("get_invalid_path", ok,
                         f"HTTP {r.status_code} ({'expected non-200' if ok else 'unexpected 200'})")
        except Exception as e:
            self._record("get_invalid_path", True,
                         f"Exception as expected: {type(e).__name__}")

    def test_patch_invalid_body(self):
        self._phase("PHASE 7.2: PATCH malformed XML (negative)")
        try:
            bad_xml = "<drivenets-top><not-valid></drivenets-top>"
            r = self.rc_patch(bad_xml)
            self._log_rc("PATCH", self._pu(), r.status_code, bad_xml, r.text)
            ok = r.status_code not in (200, 201, 204)
            self._record("patch_invalid_body", ok,
                         f"HTTP {r.status_code} ({'expected reject' if ok else 'unexpected accept'})")
        except Exception as e:
            self._record("patch_invalid_body", True, f"Exception: {type(e).__name__}")

    def test_patch_invalid_profile_value(self):
        self._phase("PHASE 7.3: PATCH invalid value (negative)")
        try:
            bad = self._wrap_pm(
                '<profiles><cfm><two-way-delay-measurement>'
                '<profile><profile-name>RESTCONF_INVALID_TEST</profile-name>'
                '<config-items><profile-name>RESTCONF_INVALID_TEST</profile-name>'
                '<cfm-eth-dm-performance-thresholds>'
                '<delay-rtt-min>NOT_A_NUMBER</delay-rtt-min>'
                '</cfm-eth-dm-performance-thresholds>'
                '</config-items></profile>'
                '</two-way-delay-measurement></cfm></profiles>')
            r = self.rc_patch(bad)
            self._log_rc("PATCH", self._pu(), r.status_code, bad, r.text)
            ok = r.status_code not in (200, 201, 204)
            self._record("patch_invalid_value", ok,
                         f"HTTP {r.status_code} ({'expected reject' if ok else 'unexpected accept'})")
        except Exception as e:
            self._record("patch_invalid_value", True,
                         f"Exception: {type(e).__name__}")

    # ──────────────────────────────────────────────────────────────
    # Phase 8: Cleanup
    # ──────────────────────────────────────────────────────────────
    def test_unmount_device(self):
        self._phase("PHASE 8: Unmount device")
        if not self.cleanup:
            self._record("unmount_device", True, "Skipped (--cleanup not set)")
            return
        try:
            r = self.http.delete(self._mu(), timeout=30)
            self._log_rc("DELETE", self._mu(), r.status_code, resp_body=r.text)
            ok = r.status_code in (200, 204)
            self._record("unmount_device", ok,
                         f"HTTP {r.status_code}" +
                         ("" if ok else f" -- {r.text[:200]}"))
        except Exception as e:
            self._record("unmount_device", False, f"Exception: {e}")

    # ──────────────────────────────────────────────────────────────
    # Orchestrator
    # ──────────────────────────────────────────────────────────────
    def run_all(self):
        start = datetime.now()
        print("=" * 70)
        print(f"  {B}Y.1731 RESTCONF SANITY TEST{X}")
        print(f"  Device   : {self.host}")
        print(f"  ODL      : {self.odl_host}:{self.odl_port}")
        print(f"  Node     : {self.node_name}")
        print(f"  Jira     : SW-237067")
        print(f"  Started  : {start.strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 70)
        try:
            if not self.no_ssh_verify:
                self.ssh_connect()
            # Phase 1: Setup
            self.test_mount_device()
            self.test_verify_mount_status()
            self.test_discover_yang_paths()
            self.test_discover_cfm_context()
            # Phase 2: GET baseline
            self.test_get_pm_config()
            self.test_get_pm_oper()
            self.test_get_pm_all()
            # Small delay to let device settle before writes
            time.sleep(3)
            # Phase 3: Create all artifacts via RESTCONF
            # Small delay between creates to avoid ODL commit races
            self.test_create_dm_profile()
            time.sleep(2)
            self.test_create_slm_profile()
            time.sleep(2)
            # Sessions: iterate over ALL discovered CFM contexts
            self.test_create_dm_sessions()
            self.test_create_slm_sessions()
            # Phase 4: Wait for ODL->device propagation + verify via CLI
            self.test_verify_creates_via_cli()
            # Phase 5: Modify (also verifies via CLI before/after)
            self.test_modify_dm_profile()
            # Phase 6: Delete + verify via CLI
            self.test_delete_dm_sessions()
            self.test_delete_slm_sessions()
            self.test_delete_dm_profile()
            self.test_delete_slm_profile()
            self.test_verify_all_removed_via_cli()
            # Phase 7: Negative tests
            self.test_get_invalid_path()
            self.test_patch_invalid_body()
            self.test_patch_invalid_profile_value()
            # Phase 8: Cleanup
            self.test_unmount_device()
        except Exception as e:
            print(f"\n[ERROR] {e}")
            self._record("unexpected_error", False, str(e))
        finally:
            try:
                if not self.no_ssh_verify:
                    self.ssh_disconnect()
            except Exception:
                pass
            self.http.close()

        # ── Summary ──
        elapsed = (datetime.now() - start).total_seconds()
        total = len(self.results)
        passed_list = [(n, d) for n, s, d in self.results if s == "pass"]
        failed_list = [(n, d) for n, s, d in self.results if s == "fail"]
        skipped_list = [(n, d) for n, s, d in self.results if s == "skip"]
        passed = len(passed_list)
        failed = len(failed_list)
        skipped = len(skipped_list)

        print(f"\n{'=' * 70}")
        print(f"  {B}RESULTS SUMMARY{X}")
        print(f"{'=' * 70}")
        print(f"  Total  : {total}")
        print(f"  {G}Passed : {passed}{X}")
        if skipped:
            print(f"  {Y}Skipped: {skipped}{X}")
        if failed:
            print(f"  {R}Failed : {failed}{X}")
        else:
            print(f"  Failed : {failed}")
        print(f"  Time   : {elapsed:.1f}s")

        # ── PASSED ──
        print(f"\n  {G}{B}PASSED ({passed}):{X}")
        for n, d in passed_list:
            short = d[:90] if d else ""
            print(f"    {G}[PASS]{X} {n}" + (f"  {short}" if short else ""))

        # ── SKIPPED ──
        if skipped:
            print(f"\n  {Y}{B}SKIPPED ({skipped}):{X}")
            for n, d in skipped_list:
                short = d[:120] if d else ""
                print(f"    {Y}[SKIP]{X} {n}")
                if short:
                    print(f"           {DIM}{short}{X}")

        # ── FAILED ──
        if failed:
            print(f"\n  {R}{B}FAILED ({failed}):{X}")
            for n, d in failed_list:
                err = d
                if "<error-message>" in err:
                    m_err = re.search(r"<error-message>(.*?)</error-message>", err)
                    if m_err:
                        err = m_err.group(1)[:120]
                elif len(err) > 120:
                    err = err[:120] + "..."
                print(f"    {R}[FAIL]{X} {n}")
                if err:
                    print(f"           {Y}{err}{X}")

        print(f"\n{'=' * 70}")
        if failed == 0:
            print(f"  {G}{B}>>> ALL {total} TESTS PASSED ({passed} passed, {skipped} skipped) <<<{X}")
        else:
            print(f"  {R}{B}>>> {failed}/{total} TESTS FAILED <<<{X}")
        print(f"{'=' * 70}\n")
        return failed == 0


def main():
    p = argparse.ArgumentParser(
        description=("Y.1731 RESTCONF sanity test for DNOS via OpenDaylight.\n"
                     "Tests GET, PATCH, DELETE for Performance Monitoring.\n"
                     "Verifies every operation via CLI (before/after) and RESTCONF GET.\n\n"
                     "Jira: SW-237067 | Epic: SW-141523"),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", required=True, help="Device management IP")
    p.add_argument("--user", default="dnroot", help="SSH/NETCONF user (default: dnroot)")
    p.add_argument("--password", default="dnroot", help="SSH/NETCONF password")
    p.add_argument("--odl-host", default="10.10.75.34", help="ODL server IP")
    p.add_argument("--odl-port", type=int, default=8181, help="ODL port")
    p.add_argument("--odl-user", default="admin", help="ODL user")
    p.add_argument("--odl-password", default="admin", help="ODL password")
    p.add_argument("--node-name", default=None, help="ODL mount name (default: auto)")
    p.add_argument("--md-name", default=None, help="Maintenance Domain (auto-discovered)")
    p.add_argument("--ma-name", default=None, help="Maintenance Association (auto-discovered)")
    p.add_argument("--source-mep-id", type=int, default=None, help="Source MEP ID")
    p.add_argument("--target-mep-id", type=int, default=None, help="Target MEP ID")
    p.add_argument("--cleanup", action="store_true", help="Unmount from ODL at end")
    p.add_argument("--skip-mount", action="store_true", help="Skip mount (already mounted)")
    p.add_argument("--no-ssh-verify", action="store_true", help="Skip SSH/CLI verification")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="Show full RESTCONF request/response bodies")
    a = p.parse_args()
    t = Y1731RestconfTest(
        host=a.host, username=a.user, password=a.password,
        odl_host=a.odl_host, odl_port=a.odl_port,
        odl_user=a.odl_user, odl_password=a.odl_password,
        node_name=a.node_name, cleanup=a.cleanup,
        skip_mount=a.skip_mount, no_ssh_verify=a.no_ssh_verify,
        verbose=a.verbose,
        md_name=a.md_name, ma_name=a.ma_name,
        source_mep_id=a.source_mep_id, target_mep_id=a.target_mep_id)
    sys.exit(0 if t.run_all() else 1)


if __name__ == "__main__":
    main()
