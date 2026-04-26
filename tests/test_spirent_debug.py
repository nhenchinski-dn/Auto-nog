#!/usr/bin/env python3
"""Debug: Figure out correct STC PDU/VLAN approach."""

from stcrestclient import stchttp
import time

LABSERVER = 'il-auto-containers'
CHASSIS_IP = '100.64.15.236'

stc = stchttp.StcHttp(LABSERVER, port=80)

# Clean up old session
for s in stc.sessions():
    if 'sw244113' in s:
        try:
            stc.join_session(s)
            stc.end_session(s)
        except: pass

sid = stc.new_session('dn', 'sw244113_debug')
stc.join_session(sid)

project = stc.get('system1', 'children-project')
port1 = stc.create('port', under=project)
stc.config(port1, {'location': f'//{CHASSIS_IP}/1/25'})
stc.perform('AttachPorts', params={'RevokeOwner': 'true'})
stc.apply()
print(f"Port online: {stc.get(port1, 'Online')}")

# Create a bare streamblock
sb = stc.create('streamBlock', under=port1)
stc.config(sb, {'Name': 'test_stream', 'FixedFrameLength': '128'})
stc.apply()

# Inspect what children the streamblock has
children = stc.get(sb, 'children')
print(f"\nStreamblock children: {children}")

# Try to get the frame config
fc = stc.get(sb, 'FrameConfig')
print(f"\nDefault FrameConfig:\n{fc}")

# List the headers/children types
for child in children.split():
    obj_type = stc.get(child, 'Name')
    print(f"  Child: {child} Name={obj_type}")
    try:
        sub_children = stc.get(child, 'children')
        if sub_children:
            print(f"    Sub-children: {sub_children}")
    except: pass

# Now try to add a VLAN via the Vlans container on the EthernetII
print("\n--- Trying VLAN approaches ---")

# Approach: find EthernetII and add Vlans
eth_children = [c for c in children.split() if 'ethernet' in c.lower()]
print(f"Ethernet children: {eth_children}")

if eth_children:
    eth = eth_children[0]
    eth_sub = stc.get(eth, 'children')
    print(f"EthernetII sub-children: {eth_sub}")

    # Try creating Vlans container under EthernetII
    try:
        vlans = stc.create('Vlans', under=eth)
        print(f"Created Vlans: {vlans}")
        vlan = stc.create('Vlan', under=vlans)
        print(f"Created Vlan under Vlans: {vlan}")
        stc.config(vlan, {'VlanId': '100'})
        stc.apply()
        print("SUCCESS with Vlans > Vlan approach!")
        fc2 = stc.get(sb, 'FrameConfig')
        print(f"FrameConfig after VLAN:\n{fc2}")
    except Exception as e:
        print(f"Vlans approach failed: {e}")

        # Try approach 2: create Vlan directly under ethernet
        try:
            vlan = stc.create('Vlan', under=eth)
            print(f"Created Vlan directly under eth: {vlan}")
            stc.config(vlan, {'VlanId': '100'})
            stc.apply()
            print("SUCCESS with direct Vlan approach!")
        except Exception as e2:
            print(f"Direct Vlan failed: {e2}")

print("\nDone. Cleaning up...")
stc.end_session(sid)
