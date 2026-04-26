"""Inspect Spirent port-group at slot 1 port 25 for breakout configuration."""
import time
from stcrestclient import stchttp

stc = stchttp.StcHttp('il-auto-containers', port=80)
for s in stc.sessions():
    if 'sw258863_brk' in s:
        try: stc.join_session(s); stc.end_session(s)
        except: pass
sid = stc.new_session('dn', 'sw258863_brk')
stc.join_session(sid)

stc.perform('ChassisConnect', params={'Hostname': '100.64.15.236'})
time.sleep(3)
pm = stc.get('system1', 'children-physicalchassismanager')
chassis = stc.get(pm, 'children-physicalchassis').split()[0]
modules = stc.get(chassis, 'children-physicaltestmodule')

for mod in modules.split():
    slot = stc.get(mod, 'Index')
    if slot != '1':
        continue
    print(f'=== Slot {slot} module ===')
    mc = stc.get(mod)
    for k, v in sorted(mc.items()):
        kl = k.lower()
        if kl.startswith('children') or 'group' in kl or 'fan' in kl or 'mode' in kl:
            print(f'  {k} = {v}')
    pgs = stc.get(mod, 'children-physicalportgroup')
    for pg in pgs.split():
        ports = stc.get(pg, 'children-physicalport').split()
        port_idxs = [stc.get(p, 'Index') for p in ports]
        if '25' in port_idxs:
            print(f'\n--- PortGroup containing port 25 ---')
            print(f'  Port indices: {port_idxs}')
            pgc = stc.get(pg)
            for k, v in sorted(pgc.items()):
                print(f'    {k} = {v}')
            print('\n  Each port detail:')
            for p, idx in zip(ports, port_idxs):
                pc = stc.get(p)
                print(f'    Port {idx}:')
                for k in sorted(pc):
                    if 'fan' in k.lower() or 'group' in k.lower() or 'mode' in k.lower() or 'break' in k.lower() or 'phy' in k.lower() or 'state' in k.lower():
                        print(f'      {k} = {pc[k]}')

stc.end_session(sid)
