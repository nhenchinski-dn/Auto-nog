import paramiko, time, sys

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('100.64.6.171', username='dnroot', password='dnroot',
            timeout=10, look_for_keys=False, allow_agent=False)
chan = ssh.invoke_shell(width=250)
time.sleep(8)
chan.recv(65535)

def send(cmd, wait=2):
    chan.send(cmd + '\n')
    time.sleep(wait)
    out = b''
    while chan.recv_ready():
        out += chan.recv(65535)
    text = out.decode(errors='replace')
    for l in text.split('\n'):
        if 'ERROR' in l:
            print(f'  ERROR [{cmd[:60]}]: {l.strip()}')
            return False
    return True

chan.send('configure\n')
time.sleep(3)
chan.recv(65535)

print('=== Creating source sub-interface (ge800-0/0/30.1) ===')
send('interfaces ge800-0/0/30.1 admin-state enabled')
send('interfaces ge800-0/0/30.1 ipv4-address 3.5.0.1/24')
send('interfaces ge800-0/0/30.1 vlan-id 1')

print('=== Creating 102 receiver sub-interfaces (ge800-0/0/31.1-.102) ===')
for i in range(1, 103):
    ok = send(f'interfaces ge800-0/0/31.{i} admin-state enabled')
    send(f'interfaces ge800-0/0/31.{i} ipv4-address 3.5.{i}.1/24')
    send(f'interfaces ge800-0/0/31.{i} vlan-id {i}')
    if i % 10 == 0:
        print(f'  Created ge800-0/0/31.{i} (3.5.{i}.1/24, VLAN {i})')

print('=== Configuring PIM on source interface ===')
send('protocols pim address-family ipv4 interface ge800-0/0/30.1 admin-state enabled', 3)

print('=== Configuring PIM on 102 receiver interfaces ===')
for i in range(1, 103):
    send(f'protocols pim address-family ipv4 interface ge800-0/0/31.{i} admin-state enabled', 2)
    if i % 10 == 0:
        print(f'  PIM enabled on ge800-0/0/31.{i}')

print('=== Configuring static RP ===')
send('protocols pim static-rp 3.5.0.1 group PIM', 3)

print('=== Configuring prefix-list for group range ===')
send('routing-policy prefix-list ipv4 PIM rule 1 allow 239.1.1.1/32', 3)

print('=== Configuring multicast rpf-intact ===')
send('multicast rpf-intact admin-state enabled', 3)

print('\n=== Running commit check ===')
chan.send('commit check\n')
time.sleep(15)
out = b''
while chan.recv_ready():
    out += chan.recv(65535)
text = out.decode(errors='replace')
for l in text.split('\n'):
    l = l.strip()
    if l and any(k in l.lower() for k in ['error', 'succeed', 'commit', 'fail', 'notice']):
        print(f'  {l}')

print('\n=== Committing ===')
chan.send('commit\n')
time.sleep(30)
out = b''
for _ in range(3):
    time.sleep(5)
    while chan.recv_ready():
        out += chan.recv(65535)
text = out.decode(errors='replace')
for l in text.split('\n'):
    l = l.strip()
    if l and any(k in l.lower() for k in ['error', 'succeed', 'commit', 'fail', 'notice']):
        print(f'  {l}')

chan.send('end\n')
time.sleep(3)
chan.recv(65535)

print('\n=== Verifying ===')
for cmd in ['show pim summary | no-more', 'show pim neighbors | no-more']:
    chan.send(cmd + '\n')
    time.sleep(5)
    out = b''
    while chan.recv_ready():
        out += chan.recv(65535)
    print(out.decode(errors='replace'))

chan.send('exit\n')
time.sleep(1)
chan.close()
ssh.close()
print('\nDone.')
