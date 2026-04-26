#!/usr/bin/env python3
"""Probe Spirent chassis for available ports."""
from stcrestclient import stchttp

stc = stchttp.StcHttp("il-auto-containers", port=80)

# End any existing probe sessions
for s in stc.sessions():
    if "sw244113_probe" in s:
        try:
            stc.join_session(s); stc.end_session(s)
        except Exception as e:
            print(f"warn: {e}")

sid = stc.new_session("dn", "sw244113_probe")
stc.join_session(sid)

# Connect chassis
chassis_mgr = stc.get("system1", "children-physicalchassismanager")
print(f"chassis_mgr: {chassis_mgr}")

stc.perform("GetPortInfo", params={"ChassisIp": "100.64.15.236"})

# List chassis
for ch in stc.get(chassis_mgr, "children-physicalchassis").split():
    print(f"\nChassis: {ch}")
    print(f"  Hostname: {stc.get(ch, 'Hostname')}")
    print(f"  ConnectionState: {stc.get(ch, 'ConnectionState')}")
    modules = stc.get(ch, "children-physicaltestmodule").split()
    for mod in modules:
        slot_idx = stc.get(mod, "Index")
        desc = stc.get(mod, "Description")
        print(f"  Module slot {slot_idx}: {desc}")
        ports = stc.get(mod, "children-physicalport").split()
        for p in ports:
            pi = stc.get(p, "Index")
            online = stc.get(p, "IsOnline") if "IsOnline" in str(stc.get(p, "children-")) else "?"
            owner = stc.get(p, "Owner") if "Owner" in str(stc.get(p, "children-")) else "?"
            print(f"    port {pi}: {p}")
            # Try to grab key attrs
            for attr in ("Owner", "Online"):
                try:
                    v = stc.get(p, attr)
                    print(f"      {attr}: {v}")
                except Exception:
                    pass

try:
    stc.end_session(sid)
except Exception:
    pass
