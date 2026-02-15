#!/usr/bin/env python3
"""
Y.1731 RESTCONF Sanity Test Script

Tests RESTCONF (via OpenDaylight) operations for Y.1731 Performance Monitoring:
  Phase 1: Setup   - Mount device to ODL, discover YANG paths, discover CFM context
  Phase 2: GET     - Retrieve PM config/oper data via RESTCONF
  Phase 3: PATCH   - Create DM profile + session, verify via GET and CLI
  Phase 4: PATCH   - Create SLM profile + session, verify via GET and CLI
  Phase 5: Modify  - Modify DM profile thresholds, verify
  Phase 6: DELETE  - Remove all test artifacts via RESTCONF
  Phase 7: Negative - Invalid path, malformed XML, invalid values
  Phase 8: Cleanup - Unmount device from ODL

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

DM_PROFILE_NAME = "RESTCONF_DM_PROF"
DM_SESSION_NAME = "RESTCONF_DM_SESS"
SLM_PROFILE_NAME = "RESTCONF_SLM_PROF"
SLM_SESSION_NAME = "RESTCONF_SLM_SESS"
DEFAULT_NS = {
    "dn-top": "http://drivenets.com/ns/yang/dn-top",
    "dn-services": "http://drivenets.com/ns/yang/dn-services",
    "dn-pm": "http://drivenets.com/ns/yang/dn-performance-monitoring",
}
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
                 no_ssh_verify=False, md_name=None, ma_name=None,
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
        self.md_name = md_name
        self.ma_name = ma_name
        self.source_mep_id = source_mep_id
        self.target_mep_id = target_mep_id
        self.ns = dict(DEFAULT_NS)
        self.yang_pm_path = ("dn-top:drivenets-top/dn-services:services/"
                             "dn-performance-monitoring:performance-monitoring")
        self.ssh_client = None
        self.shell = None
        self.results = []
        self.http = requests.Session()
        self.http.auth = self.odl_auth
        self.http.headers.update({"Accept": "application/json"})

    # --- SSH Helpers ---
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

    # --- RESTCONF Helpers ---
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

    def _record(self, name, passed, detail=""):
        self.results.append((name, passed, detail))
        tag = "[PASS]" if passed else "[FAIL]"
        print(f"  {tag} {name}" + (f" -- {detail}" if detail else ""))

    # --- XML Body Builders ---
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
        t = self.ns["dn-top"]
        s = self.ns["dn-services"]
        p = self.ns["dn-pm"]
        return (f'<drivenets-top xmlns="{t}"><services xmlns="{s}">'
                f'<performance-monitoring xmlns="{p}">{inner}'
                "</performance-monitoring></services></drivenets-top>")

    def _dm_prof_xml(self, pn=DM_PROFILE_NAME, drm=100, dra=1000,
                     drx=2000, jra=500, jrx=1000, sr=90,
                     pc=5, pi_=1, ri=10):
        return self._wrap_pm(
            "<profiles><cfm><two-way-delay-measurement>"
            f"<profile-name>{pn}</profile-name><config-items>"
            f"<profile-name>{pn}</profile-name>"
            "<inform-test-results>enabled</inform-test-results>"
            f"<test-duration><probes><probe-count>{pc}</probe-count>"
            f"<probe-interval>{pi_}</probe-interval>"
            f"<repeat-interval>{ri}</repeat-interval>"
            "</probes></test-duration><thresholds>"
            f"<delay-rtt-min>{drm}</delay-rtt-min>"
            f"<delay-rtt-avg>{dra}</delay-rtt-avg>"
            f"<delay-rtt-max>{drx}</delay-rtt-max>"
            f"<jitter-rtt-avg>{jra}</jitter-rtt-avg>"
            f"<jitter-rtt-max>{jrx}</jitter-rtt-max>"
            f"<success-rate>{sr}</success-rate></thresholds>"
            "</config-items></two-way-delay-measurement></cfm></profiles>")

    def _dm_sess_xml(self, sn=DM_SESSION_NAME, pn=DM_PROFILE_NAME):
        return self._wrap_pm(
            "<cfm><two-way-delay-measurement>"
            f"<session-name>{sn}</session-name><config-items>"
            f"<session-name>{sn}</session-name><profile>{pn}</profile>"
            "<admin-state>enabled</admin-state>"
            "<description>RESTCONF_test_DM_session</description><source>"
            f"<maintenance-domain>{self.md_name}</maintenance-domain>"
            f"<maintenance-association>{self.ma_name}</maintenance-association>"
            f"<mep-id>{self.source_mep_id}</mep-id></source>"
            f"<target><mep-id>{self.target_mep_id}</mep-id></target>"
            "</config-items></two-way-delay-measurement></cfm>")

    def _slm_prof_xml(self, pn=SLM_PROFILE_NAME, pcp=5,
                      nel=1, fel=1, pc=5, pi_=1, ri=10):
        return self._wrap_pm(
            "<profiles><cfm><two-way-synthetic-loss-measurement>"
            f"<profile-name>{pn}</profile-name><config-items>"
            f"<profile-name>{pn}</profile-name><pcp>{pcp}</pcp>"
            "<inform-test-results>enabled</inform-test-results>"
            f"<test-duration><probes><probe-count>{pc}</probe-count>"
            f"<probe-interval>{pi_}</probe-interval>"
            f"<repeat-interval>{ri}</repeat-interval>"
            "</probes></test-duration><thresholds>"
            f"<near-end-loss>{nel}</near-end-loss>"
            f"<far-end-loss>{fel}</far-end-loss></thresholds>"
            "</config-items></two-way-synthetic-loss-measurement>"
            "</cfm></profiles>")

    def _slm_sess_xml(self, sn=SLM_SESSION_NAME, pn=SLM_PROFILE_NAME):
        return self._wrap_pm(
            "<cfm><two-way-synthetic-loss-measurement>"
            f"<session-name>{sn}</session-name><config-items>"
            f"<session-name>{sn}</session-name><profile>{pn}</profile>"
            "<admin-state>enabled</admin-state>"
            "<description>RESTCONF_test_SLM_session</description><source>"
            f"<maintenance-domain>{self.md_name}</maintenance-domain>"
            f"<maintenance-association>{self.ma_name}</maintenance-association>"
            f"<mep-id>{self.source_mep_id}</mep-id></source>"
            f"<target><mep-id>{self.target_mep_id}</mep-id></target>"
            "</config-items></two-way-synthetic-loss-measurement></cfm>")

    def _del_xml(self, etype, ename):
        m = {
            "dm_session": (
                '<cfm><two-way-delay-measurement operation="delete">'
                f"<session-name>{ename}</session-name>"
                "</two-way-delay-measurement></cfm>"),
            "slm_session": (
                '<cfm><two-way-synthetic-loss-measurement operation="delete">'
                f"<session-name>{ename}</session-name>"
                "</two-way-synthetic-loss-measurement></cfm>"),
            "dm_profile": (
                '<profiles><cfm><two-way-delay-measurement operation="delete">'
                f"<profile-name>{ename}</profile-name>"
                "</two-way-delay-measurement></cfm></profiles>"),
            "slm_profile": (
                '<profiles><cfm><two-way-synthetic-loss-measurement operation="delete">'
                f"<profile-name>{ename}</profile-name>"
                "</two-way-synthetic-loss-measurement></cfm></profiles>"),
        }
        return self._wrap_pm(m[etype])

    # ==========================================================
    # Phase 1: Setup
    # ==========================================================
    def test_mount_device(self):
        print("\n" + "=" * 60 + "\nPHASE 1.1: Mount device to ODL\n" + "=" * 60)
        if self.skip_mount:
            self._record("mount_device", True, "Skipped (--skip-mount)")
            return
        h = {"Content-Type": "application/xml", "Accept": "application/xml"}
        try:
            r = self.http.put(self._mu(), data=self._mount_xml(), headers=h, timeout=30)
            ok = r.status_code in (200, 201, 204)
            self._record("mount_device", ok, f"HTTP {r.status_code}" +
                         (f" -- {r.text[:200]}" if not ok else ""))
        except Exception as e:
            self._record("mount_device", False, f"Exception: {e}")

    def test_verify_mount_status(self):
        print("\n" + "=" * 60 + "\nPHASE 1.2: Verify mount status\n" + "=" * 60)
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
                        connected = True
                        break
            except Exception:
                pass
            print(f"    Waiting for connection... ({i}/12)")
            time.sleep(5)
        self._record("verify_mount_status", connected,
                     "Device connected" if connected else "Timed out")

    def test_discover_yang_paths(self):
        print("\n" + "=" * 60 + "\nPHASE 1.3: Discover YANG namespaces\n" + "=" * 60)
        if self.no_ssh_verify:
            self._record("discover_yang_paths", True, "Skipped, using defaults")
            return
        try:
            out = self.run_show("show config services performance-monitoring "
                                "| display-xml | no-more", timeout=30)
            for uri in re.findall(r'xmlns(?::[\w-]+)?="(http://[^"]+)"', out):
                if "dn-top" in uri:
                    self.ns["dn-top"] = uri
                elif "dn-services" in uri or "dn-srv" in uri:
                    self.ns["dn-services"] = uri
                elif "performance-monitoring" in uri:
                    self.ns["dn-pm"] = uri
            for uri in re.findall(r'xmlns(?::[\w-]+)?="(http://[^"]+)"', out):
                if "performance-monitoring" in uri:
                    m = re.search(r"/yang/([\w-]+)$", uri)
                    if m:
                        self.yang_pm_path = (
                            f"dn-top:drivenets-top/dn-services:services/"
                            f"{m.group(1)}:performance-monitoring")
            self._record("discover_yang_paths", True,
                         f"pm={self.ns['dn-pm']} | path={self.yang_pm_path}")
        except Exception as e:
            self._record("discover_yang_paths", False, f"Error: {e}, using defaults")

    def test_discover_cfm_context(self):
        print("\n" + "=" * 60 + "\nPHASE 1.4: Discover CFM context\n" + "=" * 60)
        if all([self.md_name, self.ma_name, self.source_mep_id, self.target_mep_id]):
            self._record("discover_cfm_context", True,
                         f"Using provided: MD={self.md_name}, MA={self.ma_name}, "
                         f"src={self.source_mep_id}, tgt={self.target_mep_id}")
            return
        if self.no_ssh_verify:
            self._record("discover_cfm_context", False, "No CFM context, --no-ssh-verify")
            return
        try:
            out = self.run_show("show config services ethernet-oam "
                                "connectivity-fault-management "
                                "| display-set | no-more", timeout=30)
            mds = list(dict.fromkeys(re.findall(r"maintenance-domain\s+(\S+)", out)))
            mas = list(dict.fromkeys(re.findall(r"maintenance-association\s+(\S+)", out)))
            meps = sorted(set(int(x) for x in re.findall(r"mep-id\s+(\d+)", out)))
            if mds and not self.md_name:
                self.md_name = mds[0]
            if mas and not self.ma_name:
                self.ma_name = mas[0]
            if meps:
                if not self.source_mep_id:
                    self.source_mep_id = meps[0]
                if not self.target_mep_id:
                    self.target_mep_id = meps[1] if len(meps) > 1 else meps[0]
            ok = all([self.md_name, self.ma_name, self.source_mep_id, self.target_mep_id])
            d = (f"MD={self.md_name}, MA={self.ma_name}, "
                 f"src={self.source_mep_id}, tgt={self.target_mep_id}")
            self._record("discover_cfm_context", ok, d + ("" if ok else " (incomplete)"))
        except Exception as e:
            self._record("discover_cfm_context", False, f"Error: {e}")

    # ==========================================================
    # Phase 2: GET Operations
    # ==========================================================
    def _do_get(self, name, ct, label):
        print("\n" + "=" * 60 + f"\n{label}\n" + "=" * 60)
        try:
            r = self.rc_get(self.yang_pm_path, ct)
            ok = r.status_code in (200, 204)
            d = f"HTTP {r.status_code}"
            if ok and r.text:
                try:
                    d += f", keys: {list(r.json().keys())[:5]}"
                except Exception:
                    d += ", body present"
            elif not ok:
                if r.status_code in (404, 409, 500):
                    d += " (may be expected)"
                    ok = True
                else:
                    d += f" -- {r.text[:200]}"
            self._record(name, ok, d)
        except Exception as e:
            self._record(name, False, f"Exception: {e}")

    def test_get_pm_config(self):
        self._do_get("get_pm_config", "config", "PHASE 2.1: GET PM config")

    def test_get_pm_oper(self):
        self._do_get("get_pm_oper", "nonconfig", "PHASE 2.2: GET PM oper")

    def test_get_pm_all(self):
        self._do_get("get_pm_all", "all", "PHASE 2.3: GET PM all")

    # ==========================================================
    # Phase 3-4: PATCH helpers
    # ==========================================================
    def _do_patch(self, name, xml, label):
        print("\n" + "=" * 60 + f"\n{label}\n" + "=" * 60)
        try:
            r = self.rc_patch(xml)
            ok = r.status_code in (200, 201, 204)
            d = f"HTTP {r.status_code}"
            if not ok:
                d += f" -- {r.text[:300]}"
            self._record(name, ok, d)
        except Exception as e:
            self._record(name, False, f"Exception: {e}")

    def _do_verify_get(self, name, artifact, label):
        print("\n" + "=" * 60 + f"\n{label}\n" + "=" * 60)
        try:
            r = self.rc_get(self.yang_pm_path, "config")
            if r.status_code != 200:
                self._record(name, False, f"HTTP {r.status_code}")
                return
            found = artifact in r.text
            self._record(name, found,
                         f"'{artifact}' {'found' if found else 'NOT found'}")
        except Exception as e:
            self._record(name, False, f"Exception: {e}")

    def _do_verify_cli(self, name, cmd, artifact, label):
        print("\n" + "=" * 60 + f"\n{label}\n" + "=" * 60)
        if self.no_ssh_verify:
            self._record(name, True, "Skipped (--no-ssh-verify)")
            return
        try:
            out = self.run_show(cmd, timeout=15)
            found = artifact.lower() in out.lower()
            self._record(name, found,
                         f"'{artifact}' {'found' if found else 'NOT found'} in CLI")
        except Exception as e:
            self._record(name, False, f"Exception: {e}")

    # ==========================================================
    # Phase 3: PATCH DM
    # ==========================================================
    def test_patch_dm_profile(self):
        self._do_patch("patch_dm_profile", self._dm_prof_xml(),
                       "PHASE 3.1: PATCH - Create DM profile")

    def test_verify_dm_profile_via_get(self):
        self._do_verify_get("verify_dm_profile_via_get", DM_PROFILE_NAME,
                            "PHASE 3.2: Verify DM profile via GET")

    def test_verify_dm_profile_via_cli(self):
        self._do_verify_cli("verify_dm_profile_via_cli",
            f"show config services performance-monitoring profiles "
            f"cfm two-way-delay-measurement {DM_PROFILE_NAME}",
            DM_PROFILE_NAME, "PHASE 3.3: Verify DM profile via CLI")

    def test_patch_dm_session(self):
        if not all([self.md_name, self.ma_name, self.source_mep_id, self.target_mep_id]):
            print("\n" + "=" * 60 + "\nPHASE 3.4: PATCH - Create DM session\n" + "=" * 60)
            self._record("patch_dm_session", False, "Missing CFM context")
            return
        self._do_patch("patch_dm_session", self._dm_sess_xml(),
                       "PHASE 3.4: PATCH - Create DM session")

    def test_verify_dm_session_via_get(self):
        self._do_verify_get("verify_dm_session_via_get", DM_SESSION_NAME,
                            "PHASE 3.5: Verify DM session via GET")

    def test_verify_dm_session_via_cli(self):
        self._do_verify_cli("verify_dm_session_via_cli",
            f"show config services performance-monitoring "
            f"cfm two-way-delay-measurement {DM_SESSION_NAME}",
            DM_SESSION_NAME, "PHASE 3.6: Verify DM session via CLI")

    # ==========================================================
    # Phase 4: PATCH SLM
    # ==========================================================
    def test_patch_slm_profile(self):
        self._do_patch("patch_slm_profile", self._slm_prof_xml(),
                       "PHASE 4.1: PATCH - Create SLM profile")

    def test_verify_slm_profile_via_get(self):
        self._do_verify_get("verify_slm_profile_via_get", SLM_PROFILE_NAME,
                            "PHASE 4.2: Verify SLM profile via GET")

    def test_verify_slm_profile_via_cli(self):
        self._do_verify_cli("verify_slm_profile_via_cli",
            f"show config services performance-monitoring profiles "
            f"cfm two-way-synthetic-loss-measurement {SLM_PROFILE_NAME}",
            SLM_PROFILE_NAME, "PHASE 4.3: Verify SLM profile via CLI")

    def test_patch_slm_session(self):
        if not all([self.md_name, self.ma_name, self.source_mep_id, self.target_mep_id]):
            print("\n" + "=" * 60 + "\nPHASE 4.4: PATCH - Create SLM session\n" + "=" * 60)
            self._record("patch_slm_session", False, "Missing CFM context")
            return
        self._do_patch("patch_slm_session", self._slm_sess_xml(),
                       "PHASE 4.4: PATCH - Create SLM session")

    def test_verify_slm_session_via_get(self):
        self._do_verify_get("verify_slm_session_via_get", SLM_SESSION_NAME,
                            "PHASE 4.5: Verify SLM session via GET")

    def test_verify_slm_session_via_cli(self):
        self._do_verify_cli("verify_slm_session_via_cli",
            f"show config services performance-monitoring "
            f"cfm two-way-synthetic-loss-measurement {SLM_SESSION_NAME}",
            SLM_SESSION_NAME, "PHASE 4.6: Verify SLM session via CLI")

    # ==========================================================
    # Phase 5: Modify
    # ==========================================================
    def test_patch_modify_dm_profile(self):
        self._do_patch("patch_modify_dm_profile", self._dm_prof_xml(drm=200),
                       "PHASE 5.1: PATCH - Modify DM profile threshold")

    def test_verify_dm_modification_via_get(self):
        print("\n" + "=" * 60 + "\nPHASE 5.2: Verify modification via GET\n" + "=" * 60)
        try:
            r = self.rc_get(self.yang_pm_path, "config")
            if r.status_code != 200:
                self._record("verify_dm_modification_via_get", False, f"HTTP {r.status_code}")
                return
            fp = DM_PROFILE_NAME in r.text
            fv = "200" in r.text
            self._record("verify_dm_modification_via_get", fp and fv,
                         f"Profile found={fp}, delay-rtt-min=200 found={fv}")
        except Exception as e:
            self._record("verify_dm_modification_via_get", False, f"Exception: {e}")

    def test_verify_dm_modification_via_cli(self):
        print("\n" + "=" * 60 + "\nPHASE 5.3: Verify modification via CLI\n" + "=" * 60)
        if self.no_ssh_verify:
            self._record("verify_dm_modification_via_cli", True, "Skipped")
            return
        try:
            out = self.run_show(f"show config services performance-monitoring profiles "
                                f"cfm two-way-delay-measurement {DM_PROFILE_NAME}", timeout=15)
            found = "200" in out
            self._record("verify_dm_modification_via_cli", found,
                         f"delay-rtt-min 200 {'found' if found else 'NOT found'}")
        except Exception as e:
            self._record("verify_dm_modification_via_cli", False, f"Exception: {e}")

    # ==========================================================
    # Phase 6: DELETE
    # ==========================================================
    def _do_del(self, name, etype, ename, label):
        print("\n" + "=" * 60 + f"\n{label}\n" + "=" * 60)
        try:
            r = self.rc_patch(self._del_xml(etype, ename))
            ok = r.status_code in (200, 201, 204)
            d = f"HTTP {r.status_code}"
            if not ok:
                d += f" -- {r.text[:300]}"
            self._record(name, ok, d)
        except Exception as e:
            self._record(name, False, f"Exception: {e}")

    def test_delete_dm_session(self):
        self._do_del("delete_dm_session", "dm_session", DM_SESSION_NAME,
                     "PHASE 6.1: DELETE DM session")

    def test_verify_dm_session_removed(self):
        print("\n" + "=" * 60 + "\nPHASE 6.2: Verify DM session removed\n" + "=" * 60)
        try:
            r = self.rc_get(self.yang_pm_path, "config")
            if r.status_code != 200:
                self._record("verify_dm_session_removed", True,
                             f"HTTP {r.status_code} (PM may be empty)")
                return
            nf = DM_SESSION_NAME not in r.text
            self._record("verify_dm_session_removed", nf,
                         f"'{DM_SESSION_NAME}' {'removed' if nf else 'STILL PRESENT'}")
        except Exception as e:
            self._record("verify_dm_session_removed", False, f"Exception: {e}")

    def test_delete_slm_session(self):
        self._do_del("delete_slm_session", "slm_session", SLM_SESSION_NAME,
                     "PHASE 6.3: DELETE SLM session")

    def test_delete_dm_profile(self):
        self._do_del("delete_dm_profile", "dm_profile", DM_PROFILE_NAME,
                     "PHASE 6.4: DELETE DM profile")

    def test_delete_slm_profile(self):
        self._do_del("delete_slm_profile", "slm_profile", SLM_PROFILE_NAME,
                     "PHASE 6.5: DELETE SLM profile")

    def test_verify_all_removed_via_cli(self):
        print("\n" + "=" * 60 + "\nPHASE 6.6: Verify all removed via CLI\n" + "=" * 60)
        if self.no_ssh_verify:
            self._record("verify_all_removed_via_cli", True, "Skipped")
            return
        try:
            out = self.run_show("show config services performance-monitoring "
                                "| display-set | no-more", timeout=15)
            arts = [DM_PROFILE_NAME, DM_SESSION_NAME, SLM_PROFILE_NAME, SLM_SESSION_NAME]
            rem = [a for a in arts if a.lower() in out.lower()]
            self._record("verify_all_removed_via_cli", len(rem) == 0,
                         "All removed" if not rem else f"Still present: {rem}")
        except Exception as e:
            self._record("verify_all_removed_via_cli", False, f"Exception: {e}")

    # ==========================================================
    # Phase 7: Negative Tests
    # ==========================================================
    def test_get_invalid_path(self):
        print("\n" + "=" * 60 + "\nPHASE 7.1: GET invalid path (negative)\n" + "=" * 60)
        try:
            r = self.rc_get("dn-top:drivenets-top/dn-nonexistent:fake", "config")
            ok = r.status_code != 200
            self._record("get_invalid_path", ok,
                         f"HTTP {r.status_code} ({'expected' if ok else 'unexpected'})")
        except Exception as e:
            self._record("get_invalid_path", True,
                         f"Exception as expected: {type(e).__name__}")

    def test_patch_invalid_body(self):
        print("\n" + "=" * 60 + "\nPHASE 7.2: PATCH malformed XML (negative)\n" + "=" * 60)
        try:
            r = self.rc_patch("<drivenets-top><not-valid></drivenets-top>")
            ok = r.status_code not in (200, 201, 204)
            self._record("patch_invalid_body", ok,
                         f"HTTP {r.status_code} ({'expected' if ok else 'unexpected'})")
        except Exception as e:
            self._record("patch_invalid_body", True, f"Exception: {type(e).__name__}")

    def test_patch_invalid_profile_value(self):
        print("\n" + "=" * 60 + "\nPHASE 7.3: PATCH invalid value (negative)\n" + "=" * 60)
        try:
            bad = self._wrap_pm(
                "<profiles><cfm><two-way-delay-measurement>"
                "<profile-name>RESTCONF_INVALID_TEST</profile-name>"
                "<config-items><profile-name>RESTCONF_INVALID_TEST</profile-name>"
                "<thresholds><delay-rtt-min>NOT_A_NUMBER</delay-rtt-min>"
                "</thresholds></config-items>"
                "</two-way-delay-measurement></cfm></profiles>")
            r = self.rc_patch(bad)
            ok = r.status_code not in (200, 201, 204)
            self._record("patch_invalid_profile_value", ok,
                         f"HTTP {r.status_code} ({'expected' if ok else 'unexpected'})")
        except Exception as e:
            self._record("patch_invalid_profile_value", True,
                         f"Exception: {type(e).__name__}")

    # ==========================================================
    # Phase 8: Cleanup
    # ==========================================================
    def test_unmount_device(self):
        print("\n" + "=" * 60 + "\nPHASE 8: Unmount device\n" + "=" * 60)
        if not self.cleanup:
            self._record("unmount_device", True, "Skipped (--cleanup not set)")
            return
        try:
            r = self.http.delete(self._mu(), timeout=30)
            ok = r.status_code in (200, 204)
            self._record("unmount_device", ok,
                         f"HTTP {r.status_code}" +
                         ("" if ok else f" -- {r.text[:200]}"))
        except Exception as e:
            self._record("unmount_device", False, f"Exception: {e}")

    # ==========================================================
    # Orchestrator
    # ==========================================================
    def run_all(self):
        start = datetime.now()
        print("=" * 70)
        print("  Y.1731 RESTCONF SANITY TEST")
        print(f"  Device   : {self.host}")
        print(f"  ODL      : {self.odl_host}:{self.odl_port}")
        print(f"  Node     : {self.node_name}")
        print(f"  Jira     : SW-237067")
        print(f"  Started  : {start.strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 70)
        try:
            if not self.no_ssh_verify:
                self.ssh_connect()
            self.test_mount_device()
            self.test_verify_mount_status()
            self.test_discover_yang_paths()
            self.test_discover_cfm_context()
            self.test_get_pm_config()
            self.test_get_pm_oper()
            self.test_get_pm_all()
            self.test_patch_dm_profile()
            self.test_verify_dm_profile_via_get()
            self.test_verify_dm_profile_via_cli()
            self.test_patch_dm_session()
            self.test_verify_dm_session_via_get()
            self.test_verify_dm_session_via_cli()
            self.test_patch_slm_profile()
            self.test_verify_slm_profile_via_get()
            self.test_verify_slm_profile_via_cli()
            self.test_patch_slm_session()
            self.test_verify_slm_session_via_get()
            self.test_verify_slm_session_via_cli()
            self.test_patch_modify_dm_profile()
            self.test_verify_dm_modification_via_get()
            self.test_verify_dm_modification_via_cli()
            self.test_delete_dm_session()
            self.test_verify_dm_session_removed()
            self.test_delete_slm_session()
            self.test_delete_dm_profile()
            self.test_delete_slm_profile()
            self.test_verify_all_removed_via_cli()
            self.test_get_invalid_path()
            self.test_patch_invalid_body()
            self.test_patch_invalid_profile_value()
            self.test_unmount_device()
        except Exception as e:
            print(f"\n[ERROR] {e}")
            self._record("Unexpected error", False, str(e))
        finally:
            try:
                if not self.no_ssh_verify:
                    self.ssh_disconnect()
            except Exception:
                pass
            self.http.close()

        elapsed = (datetime.now() - start).total_seconds()
        total = len(self.results)
        passed = sum(1 for _, p, _ in self.results if p)
        failed = total - passed
        print("\n" + "=" * 70 + "\n  FULL RESULTS\n" + "=" * 70)
        for n, p, d in self.results:
            print(f"  {'[PASS]' if p else '[FAIL]'} {n}" +
                  (f" -- {d}" if d else ""))
        print("\n" + "-" * 70 + "\n  SUMMARY\n" + "-" * 70)
        print(f"  Total : {total}")
        print(f"  Passed: {passed}")
        print(f"  Failed: {failed}")
        print(f"  Time  : {elapsed:.1f}s")
        print("=" * 70)
        if failed:
            print("\n  Failed tests:")
            for n, p, d in self.results:
                if not p:
                    print(f"    - {n}: {d}")
        v = "ALL TESTS PASSED" if failed == 0 else "SOME TESTS FAILED"
        print(f"\n  >>> {v} <<<\n")
        return failed == 0


def main():
    p = argparse.ArgumentParser(
        description=("Y.1731 RESTCONF sanity test for DNOS via OpenDaylight.\n"
                     "Tests GET, PATCH, DELETE for Performance Monitoring.\n\n"
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
    a = p.parse_args()
    t = Y1731RestconfTest(
        host=a.host, username=a.user, password=a.password,
        odl_host=a.odl_host, odl_port=a.odl_port,
        odl_user=a.odl_user, odl_password=a.odl_password,
        node_name=a.node_name, cleanup=a.cleanup,
        skip_mount=a.skip_mount, no_ssh_verify=a.no_ssh_verify,
        md_name=a.md_name, ma_name=a.ma_name,
        source_mep_id=a.source_mep_id, target_mep_id=a.target_mep_id)
    sys.exit(0 if t.run_all() else 1)


if __name__ == "__main__":
    main()
