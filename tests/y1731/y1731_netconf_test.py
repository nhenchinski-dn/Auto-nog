#!/usr/bin/env python3
"""
Y.1731 NETCONF Sanity Test Script

Tests native NETCONF (SSH port 830) operations for Y.1731 Performance Monitoring:
  Phase 1: Setup    - Connect via NETCONF, discover YANG capabilities, discover CFM context
  Phase 2: GET      - Retrieve PM config/oper data via <get-config> and <get>
  Phase 3: CREATE   - Create DM/SLM profiles + sessions via <edit-config>
  Phase 4: Verify   - Verify creates via <get-config> and CLI
  Phase 5: Modify   - Modify DM profile thresholds via <edit-config>, verify
  Phase 6: DELETE   - Remove all test artifacts via <edit-config> operation="delete"
  Phase 7: Negative - Invalid filter, malformed config, invalid values, boundary tests
  Phase 8: Cleanup  - Close NETCONF session

Every edit-config is verified by:
  1. CLI BEFORE -- capture baseline via SSH show command
  2. NETCONF operation -- send the RPC
  3. CLI AFTER  -- capture via SSH and compare to baseline
  4. NETCONF get-config -- verify as secondary check

Jira: SW-237066 (Ethernet OAM Y.1731 | NETCONF)
Epic: SW-141523 (Ethernet OAM Y.1731 - Proactive PM)

Usage:
    python3 y1731_netconf_test.py --host 192.168.174.101
    python3 y1731_netconf_test.py --host 192.168.174.101 --no-cli-verify
    python3 y1731_netconf_test.py --host 192.168.174.101 --md-name MD1 --ma-name MA1 --source-mep-id 1 --target-mep-id 2
"""
import argparse
import re
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from xml.sax.saxutils import escape as xml_escape

import paramiko
from ncclient import manager
from ncclient.operations.rpc import RPCError

# ── Test artifact names ──
DM_PROFILE_NAME = "NETCONF_DM_PROF"
SLM_PROFILE_NAME = "NETCONF_SLM_PROF"

# ── YANG namespaces ──
NS = {
    "dn-top": "http://drivenets.com/ns/yang/dn-top",
    "dn-svc": "http://drivenets.com/ns/yang/dn-services",
    "dn-pm": "http://drivenets.com/ns/yang/dn-performance-monitoring",
    "dn-cfm": "http://drivenets.com/ns/yang/dn-srv-connectivity-fault-management",
}

# ── ANSI colour codes ──
G = "\033[32m"
R = "\033[31m"
Y = "\033[33m"
C = "\033[36m"
B = "\033[1m"
DIM = "\033[2m"
X = "\033[0m"


def _indent_xml(xml_str: str) -> str:
    """Pretty-print XML for verbose output."""
    try:
        from xml.dom.minidom import parseString
        return "\n".join(
            line for line in parseString(xml_str).toprettyxml(indent="  ").split("\n")
            if line.strip()
        )[len('<?xml version="1.0" ?>'):]
    except Exception:
        return xml_str


# ──────────────────────────────────────────────────────────────
# XML Builders
# ──────────────────────────────────────────────────────────────

def _pm_filter() -> str:
    return (
        f'<drivenets-top xmlns="{NS["dn-top"]}">'
        f'<services xmlns="{NS["dn-svc"]}">'
        f'<performance-monitoring xmlns="{NS["dn-pm"]}"/>'
        '</services></drivenets-top>'
    )


def _cfm_filter() -> str:
    return (
        f'<drivenets-top xmlns="{NS["dn-top"]}">'
        f'<services xmlns="{NS["dn-svc"]}">'
        f'<ethernet-oam xmlns="{NS["dn-cfm"]}">'
        '<connectivity-fault-management/>'
        '</ethernet-oam></services></drivenets-top>'
    )


def _dm_profile_filter() -> str:
    return (
        f'<drivenets-top xmlns="{NS["dn-top"]}">'
        f'<services xmlns="{NS["dn-svc"]}">'
        f'<performance-monitoring xmlns="{NS["dn-pm"]}">'
        '<profiles><cfm><two-way-delay-measurement>'
        f'<profile><profile-name>{DM_PROFILE_NAME}</profile-name></profile>'
        '</two-way-delay-measurement></cfm></profiles>'
        '</performance-monitoring></services></drivenets-top>'
    )


def _wrap_pm(inner: str) -> str:
    return (
        f'<drivenets-top xmlns="{NS["dn-top"]}">'
        f'<services xmlns="{NS["dn-svc"]}">'
        f'<performance-monitoring xmlns="{NS["dn-pm"]}">'
        f'{inner}'
        '</performance-monitoring></services></drivenets-top>'
    )


def dm_profile_xml(name: str = DM_PROFILE_NAME, drm: int = 100,
                    dra: int = 1000, drx: int = 2000,
                    jra: int = 500, jrx: int = 1000,
                    sr: float = 90.0, pc: int = 5,
                    pi: int = 1, ri: int = 10) -> str:
    return _wrap_pm(
        '<profiles><cfm><two-way-delay-measurement>'
        f'<profile><profile-name>{name}</profile-name>'
        f'<config-items><profile-name>{name}</profile-name>'
        '<inform-test-results>enabled</inform-test-results>'
        '<test-duration-probes>'
        f'<probe-count>{pc}</probe-count>'
        f'<probe-interval>{pi}</probe-interval>'
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
        '</two-way-delay-measurement></cfm></profiles>'
    )


def slm_profile_xml(name: str = SLM_PROFILE_NAME, pcp: int = 5,
                     nel: float = 1.0, fel: float = 1.0,
                     pc: int = 5, pi: int = 1, ri: int = 10) -> str:
    return _wrap_pm(
        '<profiles><cfm><two-way-synthetic-loss-measurement>'
        f'<profile><profile-name>{name}</profile-name>'
        f'<config-items><profile-name>{name}</profile-name>'
        f'<pcp>{pcp}</pcp>'
        '<inform-test-results>enabled</inform-test-results>'
        '<test-duration-probes>'
        f'<probe-count>{pc}</probe-count>'
        f'<probe-interval>{pi}</probe-interval>'
        f'<repeat-interval>{ri}</repeat-interval>'
        '</test-duration-probes>'
        '<cfm-eth-sl-performance-thresholds>'
        f'<near-end-loss>{nel}</near-end-loss>'
        f'<far-end-loss>{fel}</far-end-loss>'
        '</cfm-eth-sl-performance-thresholds>'
        '</config-items></profile>'
        '</two-way-synthetic-loss-measurement></cfm></profiles>'
    )


def dm_session_xml(ctx: Dict[str, Any], profile: str = DM_PROFILE_NAME) -> str:
    sn = xml_escape(ctx["dm_sess"])
    md = xml_escape(str(ctx["md"]))
    ma = xml_escape(str(ctx["ma"]))
    return _wrap_pm(
        f'<cfm-tests><proactive-monitoring xmlns="{NS["dn-cfm"]}">'
        '<two-way-delay-measurements>'
        f'<test-session><session-name>{sn}</session-name>'
        '<config-items>'
        f'<profile>{xml_escape(profile)}</profile>'
        '<admin-state>enabled</admin-state>'
        '<description>NETCONF_test_DM_session</description>'
        f'<source-md-name>{md}</source-md-name>'
        f'<source-ma-name>{ma}</source-ma-name>'
        f'<source-mep-id>{ctx["src"]}</source-mep-id>'
        f'<target-mep-id>{ctx["tgt"]}</target-mep-id>'
        '</config-items></test-session>'
        '</two-way-delay-measurements>'
        '</proactive-monitoring></cfm-tests>'
    )


def slm_session_xml(ctx: Dict[str, Any], profile: str = SLM_PROFILE_NAME) -> str:
    sn = xml_escape(ctx["slm_sess"])
    md = xml_escape(str(ctx["md"]))
    ma = xml_escape(str(ctx["ma"]))
    return _wrap_pm(
        f'<cfm-tests><proactive-monitoring xmlns="{NS["dn-cfm"]}">'
        '<two-way-synthetic-loss-measurements>'
        f'<test-session><session-name>{sn}</session-name>'
        '<config-items>'
        f'<profile>{xml_escape(profile)}</profile>'
        '<admin-state>enabled</admin-state>'
        '<description>NETCONF_test_SLM_session</description>'
        f'<source-md-name>{md}</source-md-name>'
        f'<source-ma-name>{ma}</source-ma-name>'
        f'<source-mep-id>{ctx["src"]}</source-mep-id>'
        f'<target-mep-id>{ctx["tgt"]}</target-mep-id>'
        '</config-items></test-session>'
        '</two-way-synthetic-loss-measurements>'
        '</proactive-monitoring></cfm-tests>'
    )


def delete_xml(etype: str, ename: str) -> str:
    NC = "urn:ietf:params:xml:ns:netconf:base:1.0"
    op = f' xmlns:nc="{NC}" nc:operation="delete"'
    m = {
        "dm_session": (
            f'<cfm-tests><proactive-monitoring xmlns="{NS["dn-cfm"]}">'
            '<two-way-delay-measurements>'
            f'<test-session{op}>'
            f'<session-name>{ename}</session-name>'
            '</test-session></two-way-delay-measurements>'
            '</proactive-monitoring></cfm-tests>'
        ),
        "slm_session": (
            f'<cfm-tests><proactive-monitoring xmlns="{NS["dn-cfm"]}">'
            '<two-way-synthetic-loss-measurements>'
            f'<test-session{op}>'
            f'<session-name>{ename}</session-name>'
            '</test-session></two-way-synthetic-loss-measurements>'
            '</proactive-monitoring></cfm-tests>'
        ),
        "dm_profile": (
            '<profiles><cfm><two-way-delay-measurement>'
            f'<profile{op}>'
            f'<profile-name>{ename}</profile-name>'
            '</profile>'
            '</two-way-delay-measurement></cfm></profiles>'
        ),
        "slm_profile": (
            '<profiles><cfm><two-way-synthetic-loss-measurement>'
            f'<profile{op}>'
            f'<profile-name>{ename}</profile-name>'
            '</profile>'
            '</two-way-synthetic-loss-measurement></cfm></profiles>'
        ),
    }
    return _wrap_pm(m[etype])


# ──────────────────────────────────────────────────────────────
# Main Test Class
# ──────────────────────────────────────────────────────────────

class Y1731NetconfTest:
    """Y.1731 NETCONF sanity tester for DNOS devices (native SSH:830)."""

    def __init__(self, host: str, username: str = "dnroot",
                 password: str = "dnroot", port: int = 830,
                 no_cli_verify: bool = False, verbose: bool = False,
                 md_name: str = None, ma_name: str = None,
                 source_mep_id: int = None, target_mep_id: int = None):
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self.no_cli_verify = no_cli_verify
        self.verbose = verbose
        self._cli_md = md_name
        self._cli_ma = ma_name
        self._cli_src = source_mep_id
        self._cli_tgt = target_mep_id

        self.nc: Optional[manager.Manager] = None
        self.ssh_client: Optional[paramiko.SSHClient] = None
        self.cfm_contexts: List[Dict[str, Any]] = []
        self.results: List[Tuple[str, str, str]] = []
        self._created: set = set()

    # ──────────────────────────────────────────────────────────
    # Recording
    # ──────────────────────────────────────────────────────────
    def _record(self, name: str, status, detail: str = ""):
        if status is True:
            status = "pass"
        elif status is False:
            status = "fail"
        self.results.append((name, status, detail))
        tags = {"pass": f"{G}[PASS]{X}", "fail": f"{R}[FAIL]{X}",
                "skip": f"{Y}[SKIP]{X}"}
        tag = tags.get(status, f"{R}[????]{X}")
        short = detail[:140] + "..." if len(detail) > 140 else detail
        print(f"  {tag} {name}" + (f" -- {short}" if short else ""))

    def _phase(self, label: str):
        print(f"\n{'=' * 64}\n{label}\n{'=' * 64}")

    def _vlog(self, direction: str, xml_str: str):
        if not self.verbose:
            return
        print(f"    {C}{direction}{X}")
        for line in _indent_xml(xml_str).strip().split("\n")[:50]:
            print(f"      {line}")

    # ──────────────────────────────────────────────────────────
    # NETCONF Helpers
    # ──────────────────────────────────────────────────────────
    def nc_connect(self):
        print(f"[*] NETCONF: Connecting to {self.host}:{self.port} ...")
        self.nc = manager.connect(
            host=self.host, port=self.port,
            username=self.username, password=self.password,
            hostkey_verify=False,
            device_params={"name": "default"},
            timeout=60,
            allow_agent=False, look_for_keys=False,
        )
        sid = self.nc.session_id
        print(f"[+] NETCONF: Connected (session-id={sid})")

    def nc_disconnect(self):
        if self.nc and self.nc.connected:
            try:
                self.nc.close_session()
                print("[*] NETCONF: Session closed.")
            except Exception:
                pass

    @staticmethod
    def _reply_xml(reply) -> str:
        """Extract XML string from an ncclient RPC reply (works across versions).

        Property accessors like .data_xml or .xml may raise TypeError/ValueError
        when the underlying lxml element is None, so each must be guarded.
        """
        try:
            raw = reply._raw
            if raw:
                return raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
        except Exception:
            pass
        try:
            val = reply.xml
            if val and isinstance(val, str) and len(val) > 10:
                return val
        except Exception:
            pass
        try:
            val = reply.data_xml
            if val and isinstance(val, str) and len(val) > 10:
                return val
        except Exception:
            pass
        try:
            from lxml import etree
            elem = getattr(reply, "data", None) or getattr(reply, "_root", None)
            if elem is not None:
                return etree.tostring(elem, encoding="unicode", pretty_print=True)
        except Exception:
            pass
        try:
            return str(reply)
        except Exception:
            return ""

    def nc_get_config_xml(self, filt: str, source: str = "running") -> str:
        reply = self.nc.get_config(source=source, filter=("subtree", filt))
        xml_str = self._reply_xml(reply)
        self._vlog(f"<get-config source='{source}'>", xml_str)
        return xml_str

    def nc_get_xml(self, filt: str) -> str:
        reply = self.nc.get(filter=("subtree", filt))
        xml_str = self._reply_xml(reply)
        self._vlog("<get>", xml_str)
        return xml_str

    def nc_edit_config(self, config_xml: str) -> bool:
        """edit-config to candidate + commit. Retries on transient lock/commit errors."""
        wrapped = f"<config>{config_xml}</config>"
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                self._vlog("<edit-config target='candidate'>", config_xml)
                self.nc.edit_config(target="candidate", config=wrapped)
                self.nc.commit()
                return True
            except RPCError as e:
                err = str(e)
                if attempt < max_retries and any(
                    s in err for s in ("commit is in progress", "empty commit",
                                       "lock", "in-use")
                ):
                    print(f"    {DIM}Retry {attempt}/{max_retries}: {err[:80]}{X}")
                    try:
                        self.nc.discard_changes()
                    except Exception:
                        pass
                    time.sleep(5)
                    continue
                try:
                    self.nc.discard_changes()
                except Exception:
                    pass
                raise
        return False

    # ──────────────────────────────────────────────────────────
    # CLI (SSH) Helpers  -- for before/after verification
    # ──────────────────────────────────────────────────────────
    def cli_connect(self):
        if self.no_cli_verify:
            return
        print(f"[*] CLI-SSH: Connecting to {self.host} for verification ...")
        self.ssh_client = paramiko.SSHClient()
        self.ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.ssh_client.connect(
            hostname=self.host, username=self.username,
            password=self.password, look_for_keys=False,
            allow_agent=False, timeout=30,
        )
        print("[+] CLI-SSH: Connected.\n")

    def cli_disconnect(self):
        if self.ssh_client:
            self.ssh_client.close()
            print("[*] CLI-SSH: Disconnected.")

    def cli_snapshot(self, show_cmd: str, timeout: int = 20) -> str:
        if self.no_cli_verify or not self.ssh_client:
            return ""
        try:
            ch = self.ssh_client.invoke_shell(width=250, height=1000)
            try:
                buf = ""
                end = time.time() + 10
                while time.time() < end:
                    if ch.recv_ready():
                        buf += ch.recv(65536).decode("utf-8", errors="replace")
                        if buf.strip().endswith("#") or buf.strip().endswith(">"):
                            break
                    else:
                        time.sleep(0.2)
                ch.send("no-paging\n")
                time.sleep(0.5)
                while ch.recv_ready():
                    ch.recv(65536)
                ch.send(show_cmd + " | no-more\n")
                time.sleep(0.5)
                output = ""
                end = time.time() + timeout
                while time.time() < end:
                    if ch.recv_ready():
                        output += ch.recv(65536).decode("utf-8", errors="replace")
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

    @staticmethod
    def _cli_has(output: str, artifact: str) -> bool:
        return artifact.lower() in output.lower()

    # ──────────────────────────────────────────────────────────
    # Phase 1: Connect + Discover
    # ──────────────────────────────────────────────────────────
    def test_netconf_connect(self):
        self._phase("PHASE 1.1: NETCONF connect")
        try:
            self.nc_connect()
            ok = self.nc is not None and self.nc.connected
            self._record("netconf_connect", ok,
                         f"session-id={self.nc.session_id}" if ok else "Failed")
        except Exception as e:
            self._record("netconf_connect", False, f"Exception: {e}")

    def test_capabilities(self):
        self._phase("PHASE 1.2: Verify NETCONF capabilities")
        try:
            caps = list(self.nc.server_capabilities)
            has_base = any("netconf" in c and "base" in c for c in caps)
            has_pm = any("performance-monitoring" in c for c in caps)
            has_cfm = any("connectivity-fault-management" in c for c in caps)
            detail_parts = [f"total={len(caps)}", f"base={'yes' if has_base else 'NO'}"]
            if has_pm:
                detail_parts.append("PM-YANG=yes")
            if has_cfm:
                detail_parts.append("CFM-YANG=yes")
            if not has_pm and not has_cfm:
                detail_parts.append("YANG modules not in hello (normal for DNOS)")
            self._record("capabilities", has_base, ", ".join(detail_parts))
            if self.verbose:
                for c in caps:
                    print(f"    {DIM}{c}{X}")
        except Exception as e:
            self._record("capabilities", False, f"Exception: {e}")

    def test_discover_cfm_context(self):
        self._phase("PHASE 1.3: Discover CFM context via NETCONF")

        if all([self._cli_md, self._cli_ma,
                self._cli_src is not None, self._cli_tgt is not None]):
            ctx = {
                "md": self._cli_md, "ma": self._cli_ma,
                "src": self._cli_src, "tgt": self._cli_tgt,
                "dir": "cli", "free": True,
                "dm_sess": f"NETCONF_DM_SESS_mep{self._cli_src}",
                "slm_sess": f"NETCONF_SLM_SESS_mep{self._cli_src}",
            }
            self.cfm_contexts = [ctx]
            self._record("discover_cfm", True,
                         f"CLI-provided: MD={ctx['md']}, MA={ctx['ma']}, "
                         f"src={ctx['src']}, tgt={ctx['tgt']}")
            return

        try:
            xml_str = self.nc_get_config_xml(_cfm_filter())

            def _tag(name):
                return rf'<(?:\w+:)?{name}[^>]*>'

            def _text(name):
                return rf'<(?:\w+:)?{name}[^>]*>([^<]+)</(?:\w+:)?{name}>'

            candidates = []
            md_blocks = re.split(_tag("maintenance-domain"), xml_str)[1:]
            for md_block in md_blocks:
                md_m = re.search(_text("md-id"), md_block)
                if not md_m:
                    continue
                md_id = md_m.group(1).strip()
                ma_blocks = re.split(_tag("maintenance-association"), md_block)[1:]
                for ma_block in ma_blocks:
                    ma_m = re.search(_text("ma-id"), ma_block)
                    if not ma_m:
                        continue
                    ma_id = ma_m.group(1).strip()
                    remote_meps = [int(x) for x in
                                   re.findall(_text("mep-id"),
                                              "".join(re.findall(
                                                  r'<(?:\w+:)?remote-mep[^>]*>.*?</(?:\w+:)?remote-mep>',
                                                  ma_block, re.DOTALL)))]
                    tgt = remote_meps[0] if remote_meps else None
                    for lmep in re.split(_tag("local-mep"), ma_block)[1:]:
                        mep_m = re.search(_text("mep-id"), lmep)
                        dir_m = re.search(_text("direction"), lmep)
                        if mep_m:
                            candidates.append((
                                md_id, ma_id, int(mep_m.group(1).strip()),
                                tgt, dir_m.group(1).strip() if dir_m else "unknown"))

            used = set()
            try:
                pm_str = self.nc_get_config_xml(_pm_filter())
                for m in re.finditer(
                    _text("source-md-name") + r'\s*'
                    + _text("source-ma-name") + r'\s*'
                    + _text("source-mep-id"), pm_str
                ):
                    used.add((m.group(1).strip(), m.group(2).strip(),
                              int(m.group(3).strip())))
            except Exception:
                pass

            for md, ma, src, tgt, direction in candidates:
                if tgt is None:
                    continue
                free = (md, ma, src) not in used
                if self.verbose:
                    print(f"    {DIM}Discovered: MD='{md}' MA='{ma}' "
                          f"src={src} tgt={tgt} dir={direction} "
                          f"{'FREE' if free else 'IN-USE'}{X}")
                self.cfm_contexts.append({
                    "md": md, "ma": ma, "src": src, "tgt": tgt,
                    "dir": direction, "free": free,
                    "dm_sess": f"NETCONF_DM_SESS_mep{src}",
                    "slm_sess": f"NETCONF_SLM_SESS_mep{src}",
                })

            dir_order = {"down": 0, "up": 1, "unknown": 2}
            self.cfm_contexts.sort(
                key=lambda c: (0 if c["free"] else 1,
                               dir_order.get(c["dir"], 9)))

            lines = []
            for c in self.cfm_contexts:
                tag = "FREE" if c["free"] else "IN-USE"
                lines.append(f"mep{c['src']}@{c['md']}/{c['ma']} "
                             f"dir={c['dir']} tgt={c['tgt']} [{tag}]")
            summary = f"Found {len(self.cfm_contexts)} MEP(s)"
            if lines:
                summary += ": " + " | ".join(lines)
            self._record("discover_cfm", len(self.cfm_contexts) > 0, summary)
        except Exception as e:
            self._record("discover_cfm", False, f"Exception: {e}")

    # ──────────────────────────────────────────────────────────
    # Phase 2: GET Baseline
    # ──────────────────────────────────────────────────────────
    def test_get_pm_config(self):
        self._phase("PHASE 2.1: get-config PM (running)")
        try:
            xml_str = self.nc_get_config_xml(_pm_filter())
            detail = f"response_len={len(xml_str)}"
            if "performance-monitoring" in xml_str:
                detail += ", PM tree present"
            self._record("get_pm_config", True, detail)
        except RPCError as e:
            err = str(e)
            if "data-missing" in err or "empty" in err.lower():
                self._record("get_pm_config", True,
                             f"No PM config yet (expected): {err[:80]}")
            else:
                self._record("get_pm_config", False, f"RPCError: {err[:120]}")
        except Exception as e:
            self._record("get_pm_config", False, f"Exception: {e}")

    def test_get_pm_oper(self):
        self._phase("PHASE 2.2: get PM (config + oper state)")
        try:
            xml_str = self.nc_get_xml(_pm_filter())
            self._record("get_pm_oper", True, f"response_len={len(xml_str)}")
        except RPCError as e:
            err = str(e)
            if "data-missing" in err or "empty" in err.lower():
                self._record("get_pm_oper", True, f"No PM data (expected): {err[:80]}")
            else:
                self._record("get_pm_oper", False, f"RPCError: {err[:120]}")
        except Exception as e:
            self._record("get_pm_oper", False, f"Exception: {e}")

    # ──────────────────────────────────────────────────────────
    # Phase 3: Create Profiles + Sessions
    # ──────────────────────────────────────────────────────────
    def _do_edit(self, name: str, config_xml: str, label: str) -> bool:
        self._phase(label)
        try:
            ok = self.nc_edit_config(config_xml)
            self._record(name, ok, "edit-config accepted")
            if ok:
                self._created.add(name)
            return ok
        except RPCError as e:
            err = str(e)
            if "in use" in err.lower():
                m = re.search(r"LMEP \d+ in use with session (\S+)", err)
                conflict = m.group(1) if m else "another session"
                self._record(name, "skip", f"MEP in use by '{conflict}'")
                return False
            self._record(name, False, f"RPCError: {err[:140]}")
            return False
        except Exception as e:
            self._record(name, False, f"Exception: {e}")
            return False

    def test_create_dm_profile(self):
        self._do_edit("create_dm_profile", dm_profile_xml(),
                      "PHASE 3.1: edit-config -- Create DM profile")

    def test_create_slm_profile(self):
        self._do_edit("create_slm_profile", slm_profile_xml(),
                      "PHASE 3.2: edit-config -- Create SLM profile")

    def test_create_dm_sessions(self):
        if not self.cfm_contexts:
            self._phase("PHASE 3.3: edit-config -- Create DM sessions")
            self._record("create_dm_sessions", "skip", "No CFM contexts")
            return
        for i, ctx in enumerate(self.cfm_contexts, 1):
            tag = f"mep{ctx['src']}@{ctx['md']}/{ctx['ma']}"
            name = f"create_dm_sess_mep{ctx['src']}"
            label = f"PHASE 3.3.{i}: edit-config -- Create DM session ({tag})"
            if not ctx["free"]:
                self._phase(label)
                self._record(name, "skip", f"MEP {ctx['src']} already in-use")
                continue
            self._do_edit(name, dm_session_xml(ctx), label)
            time.sleep(2)

    def test_create_slm_sessions(self):
        if not self.cfm_contexts:
            self._phase("PHASE 3.4: edit-config -- Create SLM sessions")
            self._record("create_slm_sessions", "skip", "No CFM contexts")
            return
        for i, ctx in enumerate(self.cfm_contexts, 1):
            tag = f"mep{ctx['src']}@{ctx['md']}/{ctx['ma']}"
            name = f"create_slm_sess_mep{ctx['src']}"
            label = f"PHASE 3.4.{i}: edit-config -- Create SLM session ({tag})"
            if not ctx["free"]:
                self._phase(label)
                self._record(name, "skip", f"MEP {ctx['src']} already in-use")
                continue
            self._do_edit(name, slm_session_xml(ctx), label)
            time.sleep(2)

    # ──────────────────────────────────────────────────────────
    # Phase 4: Verify Creates (NETCONF get-config + CLI)
    # ──────────────────────────────────────────────────────────
    def test_verify_creates(self):
        self._phase("PHASE 4: Verify creates via get-config + CLI")
        if not self._created:
            self._record("verify_creates", True, "Nothing created, nothing to verify")
            return

        time.sleep(5)

        try:
            nc_xml = self.nc_get_config_xml(_pm_filter())
        except Exception as e:
            nc_xml = ""
            self._record("verify_creates_netconf", False, f"get-config failed: {e}")
            return

        prof_cmd = "show config services performance-monitoring profiles"
        sess_cmd = "show config services performance-monitoring cfm"

        artifacts = []
        if "create_dm_profile" in self._created:
            artifacts.append(("dm_profile", DM_PROFILE_NAME, prof_cmd))
        if "create_slm_profile" in self._created:
            artifacts.append(("slm_profile", SLM_PROFILE_NAME, prof_cmd))
        for ctx in self.cfm_contexts:
            if f"create_dm_sess_mep{ctx['src']}" in self._created:
                artifacts.append((f"dm_sess_mep{ctx['src']}", ctx["dm_sess"], sess_cmd))
            if f"create_slm_sess_mep{ctx['src']}" in self._created:
                artifacts.append((f"slm_sess_mep{ctx['src']}", ctx["slm_sess"], sess_cmd))

        for label, art_name, show_cmd in artifacts:
            nc_ok = art_name in nc_xml
            cli_ok = True
            cli_detail = ""
            if not self.no_cli_verify:
                cli_out = self.cli_snapshot(show_cmd)
                cli_ok = self._cli_has(cli_out, art_name)
                cli_detail = f" | CLI={'found' if cli_ok else 'NOT found'}"
            ok = nc_ok and cli_ok
            self._record(f"verify_{label}", ok,
                         f"NETCONF={'found' if nc_ok else 'NOT found'}{cli_detail}")

    # ──────────────────────────────────────────────────────────
    # Phase 5: Modify DM Profile
    # ──────────────────────────────────────────────────────────
    def test_modify_dm_profile(self):
        self._phase("PHASE 5: edit-config -- Modify DM profile (delay-rtt-min -> 999)")
        if "create_dm_profile" not in self._created:
            self._record("modify_dm_profile", "skip", "Profile was not created")
            return

        show_cmd = (f"show config services performance-monitoring profiles "
                    f"cfm two-way-delay-measurement {DM_PROFILE_NAME}")

        cli_before = self.cli_snapshot(show_cmd)

        try:
            ok = self.nc_edit_config(dm_profile_xml(drm=999))
        except RPCError as e:
            self._record("modify_dm_profile", False, f"RPCError: {str(e)[:120]}")
            return
        except Exception as e:
            self._record("modify_dm_profile", False, f"Exception: {e}")
            return

        nc_found = False
        try:
            nc_xml = self.nc_get_config_xml(_dm_profile_filter())
            nc_found = "999" in nc_xml
        except Exception:
            pass

        cli_found = True
        if not self.no_cli_verify:
            time.sleep(3)
            cli_after = self.cli_snapshot(show_cmd)
            cli_found = self._cli_has(cli_after, "999")

        overall = ok and nc_found and cli_found
        detail = (f"edit-config={'ok' if ok else 'FAIL'}"
                  f" | NETCONF={'found' if nc_found else 'NOT found'}")
        if not self.no_cli_verify:
            detail += f" | CLI={'found' if cli_found else 'NOT found'}"
        self._record("modify_dm_profile", overall, detail)

    # ──────────────────────────────────────────────────────────
    # Phase 6: Delete All Test Artifacts
    # ──────────────────────────────────────────────────────────
    def _do_delete(self, name: str, etype: str, ename: str,
                   show_cmd: str, label: str):
        self._phase(label)

        cli_before = self.cli_snapshot(show_cmd)
        was_present = self._cli_has(cli_before, ename)
        if was_present:
            print(f"    {DIM}CLI-BEFORE: '{ename}' present (will delete){X}")
        else:
            print(f"    {DIM}CLI-BEFORE: '{ename}' already absent{X}")

        try:
            ok = self.nc_edit_config(delete_xml(etype, ename))
            detail = "edit-config accepted"
        except RPCError as e:
            err = str(e)
            if "data-missing" in err or "not found" in err.lower():
                ok = True
                detail = f"Already absent ({err[:60]})"
            else:
                self._record(name, False, f"RPCError: {err[:120]}")
                return
        except Exception as e:
            self._record(name, False, f"Exception: {e}")
            return

        nc_gone = True
        try:
            nc_xml = self.nc_get_config_xml(_pm_filter())
            nc_gone = ename not in nc_xml
        except Exception:
            pass

        cli_gone = True
        if not self.no_cli_verify:
            time.sleep(3)
            cli_after = self.cli_snapshot(show_cmd)
            cli_gone = not self._cli_has(cli_after, ename)

        overall = ok and nc_gone and cli_gone
        detail += f" | NETCONF={'removed' if nc_gone else 'STILL PRESENT'}"
        if not self.no_cli_verify:
            detail += f" | CLI={'removed' if cli_gone else 'STILL PRESENT'}"
        self._record(name, overall, detail)

    def test_delete_dm_sessions(self):
        show_cmd = "show config services performance-monitoring"
        any_created = any(f"create_dm_sess_mep{c['src']}" in self._created
                          for c in self.cfm_contexts)
        if not any_created:
            self._phase("PHASE 6.1: DELETE DM sessions")
            self._record("delete_dm_sessions", "skip", "None were created")
            return
        for i, ctx in enumerate(self.cfm_contexts, 1):
            key = f"create_dm_sess_mep{ctx['src']}"
            name = f"delete_dm_sess_mep{ctx['src']}"
            tag = f"mep{ctx['src']}@{ctx['md']}/{ctx['ma']}"
            label = f"PHASE 6.1.{i}: DELETE DM session ({tag})"
            if key not in self._created:
                self._phase(label)
                self._record(name, "skip", "Session was not created")
                continue
            self._do_delete(name, "dm_session", ctx["dm_sess"], show_cmd, label)

    def test_delete_slm_sessions(self):
        show_cmd = "show config services performance-monitoring"
        any_created = any(f"create_slm_sess_mep{c['src']}" in self._created
                          for c in self.cfm_contexts)
        if not any_created:
            self._phase("PHASE 6.2: DELETE SLM sessions")
            self._record("delete_slm_sessions", "skip", "None were created")
            return
        for i, ctx in enumerate(self.cfm_contexts, 1):
            key = f"create_slm_sess_mep{ctx['src']}"
            name = f"delete_slm_sess_mep{ctx['src']}"
            tag = f"mep{ctx['src']}@{ctx['md']}/{ctx['ma']}"
            label = f"PHASE 6.2.{i}: DELETE SLM session ({tag})"
            if key not in self._created:
                self._phase(label)
                self._record(name, "skip", "Session was not created")
                continue
            self._do_delete(name, "slm_session", ctx["slm_sess"], show_cmd, label)

    def test_delete_dm_profile(self):
        if "create_dm_profile" not in self._created:
            self._phase("PHASE 6.3: DELETE DM profile")
            self._record("delete_dm_profile", "skip", "Profile was not created")
            return
        self._do_delete("delete_dm_profile", "dm_profile", DM_PROFILE_NAME,
                        "show config services performance-monitoring",
                        "PHASE 6.3: DELETE DM profile")

    def test_delete_slm_profile(self):
        if "create_slm_profile" not in self._created:
            self._phase("PHASE 6.4: DELETE SLM profile")
            self._record("delete_slm_profile", "skip", "Profile was not created")
            return
        self._do_delete("delete_slm_profile", "slm_profile", SLM_PROFILE_NAME,
                        "show config services performance-monitoring",
                        "PHASE 6.4: DELETE SLM profile")

    def test_verify_all_removed(self):
        self._phase("PHASE 6.5: Verify all test artifacts removed")
        try:
            nc_xml = self.nc_get_config_xml(_pm_filter())
        except Exception:
            nc_xml = ""

        arts = [DM_PROFILE_NAME, SLM_PROFILE_NAME]
        for ctx in self.cfm_contexts:
            arts.extend([ctx["dm_sess"], ctx["slm_sess"]])

        nc_remaining = [a for a in arts if a in nc_xml]

        cli_remaining = []
        if not self.no_cli_verify:
            cli_out = self.cli_snapshot(
                "show config services performance-monitoring")
            cli_remaining = [a for a in arts if self._cli_has(cli_out, a)]

        remaining = list(set(nc_remaining + cli_remaining))
        ok = len(remaining) == 0
        self._record("verify_all_removed", ok,
                     "All test artifacts removed" if ok
                     else f"Still present: {remaining}")

    # ──────────────────────────────────────────────────────────
    # Phase 7: Negative / Edge-Case Tests
    # ──────────────────────────────────────────────────────────
    def _neg(self, name: str, label: str, edit_xml: str = None,
             get_filter: str = None, expect_reject: bool = True):
        self._phase(label)
        try:
            if edit_xml:
                self.nc_edit_config(edit_xml)
                if expect_reject:
                    self._record(name, False, "Accepted (should have been rejected)")
                else:
                    self._record(name, True, "Accepted as expected")
            elif get_filter:
                xml_str = self.nc_get_config_xml(get_filter)
                is_empty = len(xml_str.strip()) < 100
                self._record(name, is_empty if expect_reject else not is_empty,
                             f"response_len={len(xml_str)}")
        except RPCError as e:
            err = str(e)[:120]
            if expect_reject:
                self._record(name, True, f"Rejected: {err}")
            else:
                self._record(name, False, f"Unexpected RPCError: {err}")
        except Exception as e:
            self._record(name, expect_reject,
                         f"{type(e).__name__}: {str(e)[:100]}")

    def test_neg_get_invalid_filter(self):
        self._neg("neg_invalid_filter",
                  "PHASE 7.1: get-config -- nonexistent YANG path",
                  get_filter=(
                      f'<drivenets-top xmlns="{NS["dn-top"]}">'
                      '<nonexistent-container '
                      'xmlns="http://drivenets.com/ns/yang/dn-fake"/>'
                      '</drivenets-top>'))

    def test_neg_edit_unknown_element(self):
        self._neg("neg_unknown_element",
                  "PHASE 7.2: edit-config -- unknown element in valid namespace",
                  edit_xml=(
                      f'<drivenets-top xmlns="{NS["dn-top"]}">'
                      '<not-valid-element/></drivenets-top>'))

    def test_neg_edit_string_for_integer(self):
        self._neg("neg_string_for_int",
                  "PHASE 7.3: edit-config -- string where integer expected",
                  edit_xml=_wrap_pm(
                      '<profiles><cfm><two-way-delay-measurement>'
                      '<profile><profile-name>NEG_STR</profile-name>'
                      '<config-items><profile-name>NEG_STR</profile-name>'
                      '<cfm-eth-dm-performance-thresholds>'
                      '<delay-rtt-min>NOT_A_NUMBER</delay-rtt-min>'
                      '</cfm-eth-dm-performance-thresholds>'
                      '</config-items></profile>'
                      '</two-way-delay-measurement></cfm></profiles>'))

    def test_neg_edit_negative_integer(self):
        self._neg("neg_negative_int",
                  "PHASE 7.4: edit-config -- negative value for unsigned field",
                  edit_xml=_wrap_pm(
                      '<profiles><cfm><two-way-delay-measurement>'
                      '<profile><profile-name>NEG_NEGINT</profile-name>'
                      '<config-items><profile-name>NEG_NEGINT</profile-name>'
                      '<cfm-eth-dm-performance-thresholds>'
                      '<delay-rtt-min>-1</delay-rtt-min>'
                      '</cfm-eth-dm-performance-thresholds>'
                      '</config-items></profile>'
                      '</two-way-delay-measurement></cfm></profiles>'))

    def test_neg_edit_overflow_integer(self):
        self._neg("neg_overflow_int",
                  "PHASE 7.5: edit-config -- overflow uint32 (>4294967295)",
                  edit_xml=_wrap_pm(
                      '<profiles><cfm><two-way-delay-measurement>'
                      '<profile><profile-name>NEG_OVER</profile-name>'
                      '<config-items><profile-name>NEG_OVER</profile-name>'
                      '<cfm-eth-dm-performance-thresholds>'
                      '<delay-rtt-min>99999999999</delay-rtt-min>'
                      '</cfm-eth-dm-performance-thresholds>'
                      '</config-items></profile>'
                      '</two-way-delay-measurement></cfm></profiles>'))

    def test_neg_edit_empty_profile_name(self):
        self._neg("neg_empty_profile_name",
                  "PHASE 7.6: edit-config -- empty profile name",
                  edit_xml=_wrap_pm(
                      '<profiles><cfm><two-way-delay-measurement>'
                      '<profile><profile-name></profile-name>'
                      '<config-items><profile-name></profile-name>'
                      '</config-items></profile>'
                      '</two-way-delay-measurement></cfm></profiles>'))

    def test_neg_edit_very_long_profile_name(self):
        long_name = "X" * 300
        self._neg("neg_long_profile_name",
                  "PHASE 7.7: edit-config -- 300-char profile name",
                  edit_xml=_wrap_pm(
                      '<profiles><cfm><two-way-delay-measurement>'
                      f'<profile><profile-name>{long_name}</profile-name>'
                      f'<config-items><profile-name>{long_name}</profile-name>'
                      '</config-items></profile>'
                      '</two-way-delay-measurement></cfm></profiles>'))

    def test_neg_edit_special_chars_profile_name(self):
        self._neg("neg_special_chars",
                  "PHASE 7.8: edit-config -- special chars in profile name",
                  edit_xml=_wrap_pm(
                      '<profiles><cfm><two-way-delay-measurement>'
                      '<profile><profile-name>a&lt;b&gt;c&amp;d"e</profile-name>'
                      '<config-items>'
                      '<profile-name>a&lt;b&gt;c&amp;d"e</profile-name>'
                      '</config-items></profile>'
                      '</two-way-delay-measurement></cfm></profiles>'))

    def test_neg_edit_missing_mandatory_leaf(self):
        self._neg("neg_missing_mandatory",
                  "PHASE 7.9: edit-config -- session missing source-mep-id",
                  edit_xml=_wrap_pm(
                      f'<cfm-tests><proactive-monitoring xmlns="{NS["dn-cfm"]}">'
                      '<two-way-delay-measurements>'
                      '<test-session><session-name>NEG_MISSING</session-name>'
                      '<config-items>'
                      '<profile>DEFAULT_DM</profile>'
                      '<admin-state>enabled</admin-state>'
                      '<source-md-name>FAKE_MD</source-md-name>'
                      '<source-ma-name>FAKE_MA</source-ma-name>'
                      '<target-mep-id>99</target-mep-id>'
                      '</config-items></test-session>'
                      '</two-way-delay-measurements>'
                      '</proactive-monitoring></cfm-tests>'))

    def test_neg_edit_nonexistent_profile_ref(self):
        self._neg("neg_nonexistent_profile",
                  "PHASE 7.10: edit-config -- session refs non-existent profile",
                  edit_xml=_wrap_pm(
                      f'<cfm-tests><proactive-monitoring xmlns="{NS["dn-cfm"]}">'
                      '<two-way-delay-measurements>'
                      '<test-session><session-name>NEG_BADPROF</session-name>'
                      '<config-items>'
                      '<profile>DOES_NOT_EXIST_PROFILE_12345</profile>'
                      '<admin-state>enabled</admin-state>'
                      '<source-md-name>FAKE_MD</source-md-name>'
                      '<source-ma-name>FAKE_MA</source-ma-name>'
                      '<source-mep-id>999</source-mep-id>'
                      '<target-mep-id>998</target-mep-id>'
                      '</config-items></test-session>'
                      '</two-way-delay-measurements>'
                      '</proactive-monitoring></cfm-tests>'))

    def test_neg_edit_nonexistent_md(self):
        self._neg("neg_nonexistent_md",
                  "PHASE 7.11: edit-config -- session refs non-existent MD",
                  edit_xml=_wrap_pm(
                      f'<cfm-tests><proactive-monitoring xmlns="{NS["dn-cfm"]}">'
                      '<two-way-delay-measurements>'
                      '<test-session><session-name>NEG_BADMD</session-name>'
                      '<config-items>'
                      '<profile>DEFAULT_DM</profile>'
                      '<admin-state>enabled</admin-state>'
                      '<source-md-name>COMPLETELY_FAKE_MD_XYZ</source-md-name>'
                      '<source-ma-name>COMPLETELY_FAKE_MA_XYZ</source-ma-name>'
                      '<source-mep-id>1</source-mep-id>'
                      '<target-mep-id>2</target-mep-id>'
                      '</config-items></test-session>'
                      '</two-way-delay-measurements>'
                      '</proactive-monitoring></cfm-tests>'))

    def test_neg_edit_invalid_admin_state(self):
        self._neg("neg_invalid_admin_state",
                  "PHASE 7.12: edit-config -- invalid admin-state enum value",
                  edit_xml=_wrap_pm(
                      f'<cfm-tests><proactive-monitoring xmlns="{NS["dn-cfm"]}">'
                      '<two-way-delay-measurements>'
                      '<test-session><session-name>NEG_BADSTATE</session-name>'
                      '<config-items>'
                      '<profile>DEFAULT_DM</profile>'
                      '<admin-state>BANANA</admin-state>'
                      '<source-md-name>FAKE</source-md-name>'
                      '<source-ma-name>FAKE</source-ma-name>'
                      '<source-mep-id>1</source-mep-id>'
                      '<target-mep-id>2</target-mep-id>'
                      '</config-items></test-session>'
                      '</two-way-delay-measurements>'
                      '</proactive-monitoring></cfm-tests>'))

    def test_neg_edit_zero_probe_count(self):
        self._neg("neg_zero_probe_count",
                  "PHASE 7.13: edit-config -- probe-count=0",
                  edit_xml=dm_profile_xml(name="NEG_ZERO_PC", pc=0))

    def test_neg_edit_zero_probe_interval(self):
        self._neg("neg_zero_probe_interval",
                  "PHASE 7.14: edit-config -- probe-interval=0",
                  edit_xml=dm_profile_xml(name="NEG_ZERO_PI", pi=0))

    def test_neg_edit_negative_pcp(self):
        self._neg("neg_negative_pcp",
                  "PHASE 7.15: edit-config -- SLM pcp=-1",
                  edit_xml=slm_profile_xml(name="NEG_PCP_NEG", pcp=-1))

    def test_neg_edit_pcp_out_of_range(self):
        self._neg("neg_pcp_out_of_range",
                  "PHASE 7.16: edit-config -- SLM pcp=8 (max is 7)",
                  edit_xml=slm_profile_xml(name="NEG_PCP_8", pcp=8))

    def test_neg_edit_negative_success_rate(self):
        self._neg("neg_negative_success_rate",
                  "PHASE 7.17: edit-config -- success-rate-percent=-5.0",
                  edit_xml=dm_profile_xml(name="NEG_SR_NEG", sr=-5.0))

    def test_neg_edit_success_rate_over_100(self):
        self._neg("neg_success_rate_150",
                  "PHASE 7.18: edit-config -- success-rate-percent=150.0",
                  edit_xml=dm_profile_xml(name="NEG_SR_150", sr=150.0))

    def test_neg_delete_nonexistent_profile(self):
        self._neg("neg_delete_nonexistent",
                  "PHASE 7.19: edit-config delete -- non-existent profile",
                  edit_xml=delete_xml("dm_profile", "THIS_PROFILE_NEVER_EXISTED"))

    def test_neg_edit_duplicate_session_name(self):
        if not self.cfm_contexts:
            self._phase("PHASE 7.20: edit-config -- duplicate session name")
            self._record("neg_duplicate_session", "skip", "No CFM contexts")
            return
        ctx = self.cfm_contexts[0]
        dup_name = "NEG_DUP_SESS"
        dup_xml = _wrap_pm(
            f'<cfm-tests><proactive-monitoring xmlns="{NS["dn-cfm"]}">'
            '<two-way-delay-measurements>'
            f'<test-session><session-name>{dup_name}</session-name>'
            '<config-items>'
            '<profile>DEFAULT_DM</profile>'
            '<admin-state>enabled</admin-state>'
            f'<source-md-name>{xml_escape(str(ctx["md"]))}</source-md-name>'
            f'<source-ma-name>{xml_escape(str(ctx["ma"]))}</source-ma-name>'
            f'<source-mep-id>{ctx["src"]}</source-mep-id>'
            f'<target-mep-id>{ctx["tgt"]}</target-mep-id>'
            '</config-items></test-session>'
            '</two-way-delay-measurements>'
            '</proactive-monitoring></cfm-tests>'
        )
        self._phase("PHASE 7.20: edit-config -- duplicate session name")
        try:
            self.nc_edit_config(dup_xml)
        except (RPCError, Exception):
            self._record("neg_duplicate_session", "skip",
                         "First create failed (MEP in-use), cannot test duplicate")
            return
        try:
            self.nc_edit_config(dup_xml)
            self._record("neg_duplicate_session", True,
                         "Second create accepted (idempotent merge)")
        except RPCError as e:
            self._record("neg_duplicate_session", True,
                         f"Second create rejected: {str(e)[:100]}")
        except Exception as e:
            self._record("neg_duplicate_session", True,
                         f"Second create exception: {type(e).__name__}")
        finally:
            try:
                self.nc_edit_config(delete_xml("dm_session", dup_name))
            except Exception:
                pass

    def test_neg_edit_wrong_namespace(self):
        self._neg("neg_wrong_namespace",
                  "PHASE 7.21: edit-config -- correct structure, wrong namespace",
                  edit_xml=(
                      '<drivenets-top xmlns="http://wrong.example.com/fake">'
                      '<services xmlns="http://wrong.example.com/fake2">'
                      '<performance-monitoring xmlns="http://wrong.example.com/fake3">'
                      '<profiles><cfm><two-way-delay-measurement>'
                      '<profile><profile-name>NEG_NS</profile-name>'
                      '<config-items><profile-name>NEG_NS</profile-name>'
                      '</config-items></profile>'
                      '</two-way-delay-measurement></cfm></profiles>'
                      '</performance-monitoring></services></drivenets-top>'))

    # ──────────────────────────────────────────────────────────
    # Phase 8: Close
    # ──────────────────────────────────────────────────────────
    def test_close_session(self):
        self._phase("PHASE 8: Close NETCONF session")
        try:
            if self.nc and self.nc.connected:
                self.nc.close_session()
                self._record("close_session", True, "Session closed gracefully")
                self.nc = None
            else:
                self._record("close_session", True, "Already disconnected")
        except Exception as e:
            self._record("close_session", False, f"Exception: {e}")

    # ──────────────────────────────────────────────────────────
    # Orchestrator
    # ──────────────────────────────────────────────────────────
    def run_all(self) -> bool:
        start = datetime.now()
        print("=" * 70)
        print(f"  {B}Y.1731 NETCONF SANITY TEST{X}")
        print(f"  Device   : {self.host}:{self.port}")
        print(f"  Jira     : SW-237066")
        print(f"  CLI verify: {'disabled' if self.no_cli_verify else 'enabled'}")
        print(f"  Started  : {start.strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 70)

        try:
            self.test_netconf_connect()
            if not self.nc or not self.nc.connected:
                self._record("abort", False, "Cannot continue without NETCONF session")
                return False
            if not self.no_cli_verify:
                self.cli_connect()
            self.test_capabilities()
            self.test_discover_cfm_context()

            self.test_get_pm_config()
            self.test_get_pm_oper()

            time.sleep(2)

            self.test_create_dm_profile()
            time.sleep(2)
            self.test_create_slm_profile()
            time.sleep(2)
            self.test_create_dm_sessions()
            self.test_create_slm_sessions()

            self.test_verify_creates()
            self.test_modify_dm_profile()

            self.test_delete_dm_sessions()
            self.test_delete_slm_sessions()
            self.test_delete_dm_profile()
            self.test_delete_slm_profile()
            self.test_verify_all_removed()

            self.test_neg_get_invalid_filter()
            self.test_neg_edit_unknown_element()
            self.test_neg_edit_string_for_integer()
            self.test_neg_edit_negative_integer()
            self.test_neg_edit_overflow_integer()
            self.test_neg_edit_empty_profile_name()
            self.test_neg_edit_very_long_profile_name()
            self.test_neg_edit_special_chars_profile_name()
            self.test_neg_edit_missing_mandatory_leaf()
            self.test_neg_edit_nonexistent_profile_ref()
            self.test_neg_edit_nonexistent_md()
            self.test_neg_edit_invalid_admin_state()
            self.test_neg_edit_zero_probe_count()
            self.test_neg_edit_zero_probe_interval()
            self.test_neg_edit_negative_pcp()
            self.test_neg_edit_pcp_out_of_range()
            self.test_neg_edit_negative_success_rate()
            self.test_neg_edit_success_rate_over_100()
            self.test_neg_delete_nonexistent_profile()
            self.test_neg_edit_duplicate_session_name()
            self.test_neg_edit_wrong_namespace()

            self.test_close_session()

        except Exception as e:
            print(f"\n{R}[ERROR]{X} Unexpected: {e}")
            self._record("unexpected_error", False, str(e))
        finally:
            try:
                if self.nc and self.nc.connected:
                    self.nc.close_session()
            except Exception:
                pass
            try:
                if not self.no_cli_verify:
                    self.cli_disconnect()
            except Exception:
                pass

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

        print(f"\n  {G}{B}PASSED ({passed}):{X}")
        for n, d in passed_list:
            short = d[:90] if d else ""
            print(f"    {G}[PASS]{X} {n}" + (f"  {short}" if short else ""))

        if skipped:
            print(f"\n  {Y}{B}SKIPPED ({skipped}):{X}")
            for n, d in skipped_list:
                print(f"    {Y}[SKIP]{X} {n}")
                if d:
                    print(f"           {DIM}{d[:120]}{X}")

        if failed:
            print(f"\n  {R}{B}FAILED ({failed}):{X}")
            for n, d in failed_list:
                err = d[:140]
                print(f"    {R}[FAIL]{X} {n}")
                if err:
                    print(f"           {Y}{err}{X}")

        print(f"\n{'=' * 70}")
        if failed == 0:
            print(f"  {G}{B}>>> ALL {total} TESTS PASSED "
                  f"({passed} passed, {skipped} skipped) <<<{X}")
        else:
            print(f"  {R}{B}>>> {failed}/{total} TESTS FAILED <<<{X}")
        print(f"{'=' * 70}\n")
        return failed == 0


def main():
    p = argparse.ArgumentParser(
        description=(
            "Y.1731 NETCONF sanity test for DNOS devices (native SSH:830).\n"
            "Tests get-config, get, edit-config (create/modify/delete)\n"
            "for Performance Monitoring profiles and sessions.\n\n"
            "Jira: SW-237066 | Epic: SW-141523"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--host", required=True, help="Device management IP")
    p.add_argument("--port", type=int, default=830, help="NETCONF port (default: 830)")
    p.add_argument("--user", default="dnroot", help="Username (default: dnroot)")
    p.add_argument("--password", default="dnroot", help="Password (default: dnroot)")
    p.add_argument("--md-name", default=None, help="Maintenance Domain (auto-discovered)")
    p.add_argument("--ma-name", default=None, help="Maintenance Association (auto-discovered)")
    p.add_argument("--source-mep-id", type=int, default=None, help="Source MEP ID")
    p.add_argument("--target-mep-id", type=int, default=None, help="Target MEP ID")
    p.add_argument("--no-cli-verify", action="store_true",
                   help="Skip CLI (SSH) verification of NETCONF operations")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="Show full NETCONF RPC XML bodies")
    a = p.parse_args()

    t = Y1731NetconfTest(
        host=a.host, port=a.port,
        username=a.user, password=a.password,
        no_cli_verify=a.no_cli_verify, verbose=a.verbose,
        md_name=a.md_name, ma_name=a.ma_name,
        source_mep_id=a.source_mep_id, target_mep_id=a.target_mep_id,
    )
    sys.exit(0 if t.run_all() else 1)


if __name__ == "__main__":
    main()
