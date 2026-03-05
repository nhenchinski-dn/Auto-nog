import paramiko, time, sys

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('100.64.6.171', username='dnroot', password='dnroot',
            timeout=10, look_for_keys=False, allow_agent=False)
chan = ssh.invoke_shell(width=250)
time.sleep(8)
chan.recv(65535)

chan.send('configure\n')
time.sleep(2)
chan.recv(65535)

cmds = [
    'interfaces ge800-0/0/30.1 admin-state enabled',
    'interfaces ge800-0/0/30.1 ipv4-address 3.5.0.1/24',
    'interfaces ge800-0/0/30.1 vlan-id 1',
    'protocols pim address-family ipv4 interface ge800-0/0/30.1 admin-state enabled',
    'protocols pim address-family ipv4 rp static address 8.8.8.8 group-ranges 239.0.0.0/8',
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
            print('ERR:', l.strip())

chan.send('commit\n')
time.sleep(15)
out = b''
while chan.recv_ready():
    out += chan.recv(65535)
for l in out.decode(errors='replace').split('\n'):
    l = l.strip()
    if l and ('ommit' in l.lower() or 'error' in l.lower()):
        print(l)

chan.send('end\n')
time.sleep(2)
chan.recv(65535)

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
