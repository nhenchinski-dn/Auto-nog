#!/usr/bin/env python3
"""Attach Spirent port 1/17 and check L1 link status."""
from stcrestclient import stchttp
import time

CHASSIS = "100.64.15.236"
LABSERVER = "il-auto-containers"

stc = stchttp.StcHttp(LABSERVER, port=80)
for s in stc.sessions():
    if "sw244113_v2only" in s or "sw244113_portchk" in s:
        try: stc.join_session(s); stc.end_session(s)
        except: pass
sid = stc.new_session("dn", "sw244113_portchk")
stc.join_session(sid)

project = stc.get("system1", "children-project")

def make_port(port_idx):
    p = stc.create("port", under=project)
    stc.config(p, {"location": f"//{CHASSIS}/1/{port_idx}"})
    return p

# Try all 8 ports
ports = {}
for name, idx in (("P1",1),("P2",9),("P3",17),("P4",25),("P5",33),("P6",41),("P7",49),("P8",57)):
    ports[name] = (idx, make_port(idx))

try:
    stc.perform("AttachPorts", params={"RevokeOwner": "true"})
except Exception as e:
    print(f"attach err: {e}")

stc.apply()
time.sleep(3)

for name, (idx, p) in ports.items():
    attrs = {}
    for a in ("Online","Location","Name"):
        try: attrs[a] = stc.get(p, a)
        except: pass
    # physicalport underneath
    try:
        phys = stc.get(p, "ActivePhy-Sources")
        attrs["ActivePhy"] = phys
    except: pass
    try:
        ls = stc.perform("GetL1LinkStatus", params={"HandleList": p})
        attrs["L1"] = ls
    except Exception as e:
        attrs["L1_err"] = str(e)[:80]
    print(f"{name} (1/{idx}): {attrs}")

try: stc.end_session(sid)
except: pass
