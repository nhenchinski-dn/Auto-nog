"""
Configure OSPFv3 emulated device on Spirent — no VLAN.
Spirent: port 1/25, untagged, IPv6 2001:db8:100::2/64
Advertise 5 external IPv6 routes.
"""
from stcrestclient import stchttp
import time

LABSERVER = 'il-auto-containers'
CHASSIS_IP = '100.64.15.236'
SLOT, PORT = 1, 25
SESSION_NAME = 'ospfv3_test'

stc = stchttp.StcHttp(LABSERVER, port=80)

# Clean stale sessions
for s in stc.sessions():
    if SESSION_NAME in s:
        try: stc.join_session(s); stc.end_session(s)
        except: pass

sid = stc.new_session('dn', SESSION_NAME)
stc.join_session(sid)

project = stc.get('system1', 'children-project')
port1 = stc.create('port', under=project)
stc.config(port1, {'location': f'//{CHASSIS_IP}/{SLOT}/{PORT}'})
stc.perform('AttachPorts', params={'RevokeOwner': 'true'})
stc.apply()
print(f'Port online: {stc.get(port1, "Online")}')

# Create emulated device — IPv6-only, NO VLAN
device = stc.create('EmulatedDevice', under=project,
                    **{'Name': 'OSPFv3_Router',
                       'EnablePingResponse': 'TRUE',
                       'RouterId': '10.100.1.2'})

eth_if = stc.create('EthIIIf', under=device,
                    **{'SourceMac': '00:10:94:02:A7:EC'})

ipv6_if = stc.create('Ipv6If', under=device,
                     **{'Address': '2001:db8:100::2',
                        'Gateway': '2001:db8:100::1',
                        'PrefixLength': '64'})

ipv6_ll = stc.create('Ipv6If', under=device,
                     **{'Address': 'fe80::10:94ff:fe02:a7ec',
                        'Gateway': '::',
                        'PrefixLength': '64'})

# Stack directly on Ethernet (no VLAN)
stc.config(ipv6_if, **{'StackedOnEndpoint-targets': eth_if})
stc.config(ipv6_ll, **{'StackedOnEndpoint-targets': eth_if})

stc.config(device, **{'TopLevelIf-targets': f'{ipv6_if} {ipv6_ll}',
                      'PrimaryIf-targets': ipv6_if})

stc.config(port1, **{'AffiliationPort-sources': device})
stc.apply()
print('Emulated device created (untagged IPv6 + link-local).')

# Create OSPFv3 router config
ospfv3 = stc.create('Ospfv3RouterConfig', under=device,
                    **{'AreaId': '0.0.0.0',
                       'NetworkType': 'P2P',
                       'Name': 'OSPFv3_Router'})

stc.config(ospfv3, **{'UsesIf-targets': ipv6_if})

# Advertise 5 external IPv6 routes
ospfv3_route = stc.create('Ospfv3AsExternalLsaBlock', under=ospfv3)
block = stc.get(ospfv3_route, 'children-Ipv6NetworkBlock').split()[0]
stc.config(block, {
    'StartIpList': '2001:db8:aa::',
    'PrefixLength': '48',
    'NetworkCount': '5',
})

stc.apply()
print('OSPFv3 router + 5 external routes configured.')

# Start
print('Starting ARP/ND...')
stc.perform('ArpNdStartCommand', params={'HandleList': port1})
time.sleep(5)

print('Starting device...')
stc.perform('DeviceStartCommand', params={'DeviceList': device})
time.sleep(10)

print('Waiting 20s for OSPFv3 adjacency...')
time.sleep(20)

print(f'\nDone. Session: {sid}')
print('Check DUT with:')
print('  show ospfv3 neighbors | no-more')
print('  show route table ipv6-unicast protocol ospfv3 | no-more')
