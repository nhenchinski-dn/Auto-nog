"""Inspect Spirent ports 25 & 26 PHY/FEC settings."""
import time
from stcrestclient import stchttp

stc = stchttp.StcHttp('il-auto-containers', port=80)
for s in stc.sessions():
    if 'sw258863_phy' in s:
        try: stc.join_session(s); stc.end_session(s)
        except: pass
sid = stc.new_session('dn', 'sw258863_phy')
stc.join_session(sid)
project = stc.get('system1', 'children-project')

ports = []
for slot, p in [(1, 25), (1, 26)]:
    port = stc.create('port', under=project)
    stc.config(port, {'location': f'//100.64.15.236/{slot}/{p}'})
    ports.append(port)
stc.perform('AttachPorts', params={'RevokeOwner': 'true'})
stc.apply()

for port in ports:
    print(f'\n===== Port {stc.get(port, "Location")} =====')
    print(f'Online: {stc.get(port, "Online")}')
    cfg = stc.get(port)
    for k, v in sorted(cfg.items()):
        kl = k.lower()
        if kl.startswith('children') or 'phy' in kl or 'fec' in kl or 'speed' in kl or 'link' in kl:
            print(f'  {k} = {v}')
    # Find current PHY
    for child_attr in ['children-Ethernet100GigFiber', 'children-Ethernet100GigCopper',
                       'children-Ethernet400GigFiber', 'children-EthernetCopper',
                       'children-EthernetFiber']:
        child = stc.get(port, child_attr)
        if child:
            print(f'  >> {child_attr}: {child}')
            try:
                phy_cfg = stc.get(child)
                for k, v in sorted(phy_cfg.items()):
                    if 'fec' in k.lower() or 'speed' in k.lower() or 'link' in k.lower() or 'auto' in k.lower():
                        print(f'    {k} = {v}')
            except Exception as e:
                print(f'    err: {e}')
    # Active phy
    ap = stc.get(port, 'ActivePhy-Targets') or stc.get(port, 'ActivePhy-targets')
    print(f'  ActivePhy = {ap}')
    if ap:
        try:
            apc = stc.get(ap)
            for k in sorted(apc):
                if 'fec' in k.lower() or 'link' in k.lower() or 'speed' in k.lower() or 'auto' in k.lower():
                    print(f'    {k} = {apc[k]}')
        except Exception as e:
            print(f'    err: {e}')

stc.end_session(sid)
