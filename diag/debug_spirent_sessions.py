#!/usr/bin/env python3
"""Check what Spirent sessions and streams are active."""

from stcrestclient import stchttp

LABSERVER = 'il-auto-containers'

stc = stchttp.StcHttp(LABSERVER, port=80)

sessions = stc.sessions()
print(f"Active sessions ({len(sessions)}):")
for s in sessions:
    print(f"  {s}")

for s in sessions:
    if 'sw244113' in s.lower() or 'dn' in s.lower():
        print(f"\n--- Inspecting session: {s} ---")
        try:
            stc.join_session(s)
            project = stc.get('system1', 'children-project')
            ports = stc.get(project, 'children-port')
            if ports:
                for p in ports.split():
                    loc = stc.get(p, 'Location')
                    online = stc.get(p, 'Online')
                    print(f"  Port: {loc}  Online: {online}")

                    gen = stc.get(p, 'children-generator')
                    gen_state = stc.get(gen, 'State')
                    print(f"  Generator state: {gen_state}")

                    sbs = stc.get(p, 'children-streamblock')
                    if sbs:
                        for sb in sbs.split():
                            name = stc.get(sb, 'Name')
                            active = stc.get(sb, 'Active')
                            load = stc.get(sb, 'Load')
                            load_unit = stc.get(sb, 'LoadUnit')

                            eth = stc.get(sb, 'children-ethernet:EthernetII').split()[0]
                            src_mac = stc.get(eth, 'srcMac')
                            dst_mac = stc.get(eth, 'dstMac')

                            ipv4_children = stc.get(sb, 'children-ipv4:IPv4')
                            ipv6_children = stc.get(sb, 'children-ipv6:IPv6')
                            
                            if ipv4_children:
                                ipv4 = ipv4_children.split()[0]
                                src_ip = stc.get(ipv4, 'sourceAddr')
                                dst_ip = stc.get(ipv4, 'destAddr')
                                print(f"  Stream: {name}  active={active}  load={load} {load_unit}")
                                print(f"    IPv4: src={src_ip} dst={dst_ip}")
                            elif ipv6_children:
                                ipv6 = ipv6_children.split()[0]
                                src_ip = stc.get(ipv6, 'sourceAddr')
                                dst_ip = stc.get(ipv6, 'destAddr')
                                print(f"  Stream: {name}  active={active}  load={load} {load_unit}")
                                print(f"    IPv6: src={src_ip} dst={dst_ip}")
                            else:
                                print(f"  Stream: {name}  active={active}  (no L3 header)")
                    else:
                        print("  No streamblocks")
            else:
                print("  No ports")
        except Exception as e:
            print(f"  Error: {e}")

print("\nDone.")
