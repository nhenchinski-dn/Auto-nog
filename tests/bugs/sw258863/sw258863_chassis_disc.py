"""Discover which Spirent ports on chassis 100.64.15.236 are linked to NCP3-nog."""
import time
from stcrestclient import stchttp

stc = stchttp.StcHttp('il-auto-containers', port=80)
for s in stc.sessions():
    if 'sw258863_disc' in s:
        try: stc.join_session(s); stc.end_session(s)
        except: pass
sid = stc.new_session('dn', 'sw258863_disc')
stc.join_session(sid)

stc.perform('ChassisConnect', params={'Hostname': '100.64.15.236'})
time.sleep(3)
pm = stc.get('system1', 'children-physicalchassismanager')
chassis = stc.get(pm, 'children-physicalchassis').split()[0]
modules = stc.get(chassis, 'children-physicaltestmodule')

print(f'{"Slot":>4} {"Port":>4} {"Link":>6} {"Peer":<25} {"PeerPort":<25}')
for mod in modules.split():
    slot = stc.get(mod, 'Index')
    model = stc.get(mod, 'Model')
    pgs = stc.get(mod, 'children-physicalportgroup')
    for pg in pgs.split():
        for p in stc.get(pg, 'children-physicalport').split():
            idx = stc.get(p, 'Index')
            link = stc.get(p, 'LinkStatus')
            peer = stc.get(p, 'PeerSystemName')
            peer_port = stc.get(p, 'PeerPortId')
            if link == 'Up' or 'NCP3' in (peer or '') or 'nog' in (peer or '').lower() or 'WKY' in (peer or ''):
                print(f'{slot:>4} {idx:>4} {link:>6} {peer or "—":<25} {peer_port or "—":<25} [{model}]')
print('---')
print('All slot 1 ports (where ports 25-28 should be):')
for mod in modules.split():
    slot = stc.get(mod, 'Index')
    if slot != '1':
        continue
    model = stc.get(mod, 'Model')
    print(f'  Slot {slot}: model={model}')
    pgs = stc.get(mod, 'children-physicalportgroup')
    for pg in pgs.split():
        for p in stc.get(pg, 'children-physicalport').split():
            idx = stc.get(p, 'Index')
            link = stc.get(p, 'LinkStatus')
            peer = stc.get(p, 'PeerSystemName')
            peer_port = stc.get(p, 'PeerPortId')
            print(f'    Port {idx:>3}: link={link:>6}  peer={peer or "—":<22} peer_port={peer_port or "—"}')

stc.end_session(sid)
