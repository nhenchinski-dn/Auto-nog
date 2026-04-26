#!/usr/bin/env python3
"""Probe Spirent chassis for available ports."""
from stcrestclient import stchttp

stc = stchttp.StcHttp("il-auto-containers", port=80)
for s in stc.sessions():
    if "sw244113_probe" in s:
        try: stc.join_session(s); stc.end_session(s)
        except: pass
sid = stc.new_session("dn", "sw244113_probe")
stc.join_session(sid)

stc.perform("ConnectToChassis", params={"AddrList": "100.64.15.236"})

chassis_mgr = stc.get("system1", "children-physicalchassismanager")
for ch in stc.get(chassis_mgr, "children-physicalchassis").split():
    print(f"\n=== Chassis {ch} ===")
    try:
        print(f"  Hostname: {stc.get(ch, 'Hostname')}")
        print(f"  ConnectionState: {stc.get(ch, 'ConnectionState')}")
    except Exception as e:
        print(f"  err: {e}")
    modules = stc.get(ch, "children-physicaltestmodule").split()
    for mod in modules:
        try:
            idx = stc.get(mod, "Index")
            desc = stc.get(mod, "Description")
            print(f"  Slot {idx}: {desc}")
            groups = stc.get(mod, "children-physicalportgroup").split()
            ports = []
            for g in groups:
                try:
                    ps = stc.get(g, "children-physicalport").split()
                    ports.extend(ps)
                except Exception as e:
                    print(f"    group err: {e}")
            for p in ports:
                try:
                    pi = stc.get(p, "Index")
                    nm = stc.get(p, "Name") if p else ""
                    attrs = {}
                    for a in ("LinkStatus","Owner","PortSpeed","Online","ActivePhy","PortState"):
                        try: attrs[a] = stc.get(p, a)
                        except Exception: pass
                    print(f"    port {pi}: Name={nm}  {attrs}")
                except Exception as e:
                    print(f"    err on port: {e}")
        except Exception as e:
            print(f"  module err: {e}")

try: stc.end_session(sid)
except: pass
