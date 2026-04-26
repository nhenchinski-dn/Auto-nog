"""Set CustomFecMode=KP4_FEC on Spirent ports 25 & 26 and verify link comes up."""
import time
from stcrestclient import stchttp

stc = stchttp.StcHttp('il-auto-containers', port=80)
for s in stc.sessions():
    if 'sw258863_fec' in s:
        try: stc.join_session(s); stc.end_session(s)
        except: pass
sid = stc.new_session('dn', 'sw258863_fec')
stc.join_session(sid)
project = stc.get('system1', 'children-project')

ports = []
for slot, p in [(1, 25), (1, 26)]:
    port = stc.create('port', under=project)
    stc.config(port, {'location': f'//100.64.15.236/{slot}/{p}'})
    ports.append(port)
stc.perform('AttachPorts', params={'RevokeOwner': 'true'})
stc.apply()

# Try a few FEC modes and see what works
for fec in ['KP4_FEC', 'RS_FEC', 'NONE', 'KR_FEC']:
    print(f'\n*** Trying CustomFecMode={fec} ***')
    for port in ports:
        phy = stc.get(port, 'children-Ethernet100GigFiber')
        try:
            stc.config(phy, {'CustomFecMode': fec, 'AutoNegotiation': 'false'})
        except Exception as e:
            print(f'  config err on {port}: {e}')
    stc.apply()
    # Detach + reattach to re-init PHY
    stc.perform('DetachPorts', params={'PortList': ' '.join(ports)})
    time.sleep(3)
    stc.perform('AttachPorts', params={'PortList': ' '.join(ports), 'RevokeOwner': 'true'})
    stc.apply()
    time.sleep(15)
    for port in ports:
        loc = stc.get(port, 'Location')
        phy = stc.get(port, 'children-Ethernet100GigFiber')
        c = stc.get(phy)
        print(f'  {loc}: Link={c.get("LinkStatus")} Fault={c.get("LinkFaultStatus")} '
              f'FecMode={c.get("CustomFecMode")} FecStat={c.get("CustomFecModeStatus")} '
              f'Speed={c.get("LineSpeedStatus")}')
    if all(stc.get(stc.get(p, 'children-Ethernet100GigFiber'), 'LinkStatus') == 'UP' for p in ports):
        print(f'\n>>> SUCCESS with CustomFecMode={fec} <<<')
        break

print('\nDone — leaving session open for traffic test.')
print(f'Session id: {sid}')
