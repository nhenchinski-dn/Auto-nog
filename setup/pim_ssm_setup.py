import paramiko, time

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('100.64.6.171', username='dnroot', password='dnroot',
            timeout=10, look_for_keys=False, allow_agent=False)
chan = ssh.invoke_shell(width=250)
time.sleep(8)
chan.recv(65535)

chan.send('configure\n')
time.sleep(3)
chan.recv(65535)

# Source-side sub-interface on ge800-0/0/30 (matching SW-223934 pattern)
# Spirent source will be at 3.5.0.2, DUT at 3.5.0.1
cmds = [
    'interfaces ge800-0/0/30.1 admin-state enabled',
    'interfaces ge800-0/0/30.1 ipv4-address 3.5.0.1/24',
    'interfaces ge800-0/0/30.1 vlan-id 1',
    'protocols pim address-family ipv4 interface ge800-0/0/30.1 admin-state enabled',
    'protocols pim address-family ipv4 interface lo1 admin-state enabled',
]

for c in cmds:
    chan.send(c + '\n')
    time.sleep(3)
    out = b''
    while chan.recv_ready():
        out += chan.recv(65535)
    text = out.decode(errors='replace')
    for l in text.split('\n'):
        if 'ERROR' in l:
            print(f'ERR [{c[:40]}]: {l.strip()}')

chan.send('commit check\n')
time.sleep(10)
out = b''
while chan.recv_ready():
    out += chan.recv(65535)
text = out.decode(errors='replace')
print('COMMIT CHECK:')
for l in text.split('\n'):
    l = l.strip()
    if l and ('error' in l.lower() or 'succeed' in l.lower() or 'commit' in l.lower() or 'fail' in l.lower()):
        print(f'  {l}')

chan.send('commit\n')
time.sleep(15)
out = b''
while chan.recv_ready():
    out += chan.recv(65535)
text = out.decode(errors='replace')
print('\nCOMMIT:')
for l in text.split('\n'):
    l = l.strip()
    if l and ('error' in l.lower() or 'succeed' in l.lower() or 'commit' in l.lower()):
        print(f'  {l}')

chan.send('end\n')
time.sleep(3)
chan.recv(65535)

# Verify
for cmd in ['show pim summary | no-more', 'show pim neighbors | no-more',
            'show pim tree | no-more', 'show multicast route summary | no-more',
            'show interfaces ge800-0/0/30.1 | no-more']:
    chan.send(cmd + '\n')
    time.sleep(5)
    out = b''
    while chan.recv_ready():
        out += chan.recv(65535)
    short = cmd.replace(' | no-more', '')
    print(f'\n=== {short} ===')
    text = out.decode(errors='replace')
    for l in text.split('\n'):
        if 'no-more' not in l and l.strip():
            print(l.rstrip())

chan.send('exit\n')
time.sleep(1)
chan.close()
ssh.close()
