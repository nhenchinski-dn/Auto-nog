import paramiko, time, sys
sys.stdout.reconfigure(line_buffering=True)

HOST = 'YBW1F7VB00010P1'

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username='dnroot', password='dnroot',
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

print('=== Enabling PIM on 100 sub-interfaces (ge800-0/0/10.1-.100) ===')
for i in range(1, 101):
    send(f'protocols pim address-family ipv4 interface ge800-0/0/10.{i} admin-state enabled', 2)
    if i % 10 == 0:
        print(f'  PIM enabled on ge800-0/0/10.{i}')

print('\n=== Committing ===')
chan.send('commit\n')
time.sleep(30)
out = b''
for _ in range(5):
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

print('\n=== Verifying PIM ===')
for cmd in [
    'show pim summary | no-more',
    'show pim neighbors | no-more',
]:
    chan.send(cmd + '\n')
    time.sleep(8)
    out = b''
    while chan.recv_ready():
        out += chan.recv(65535)
    print(out.decode(errors='replace'))

chan.send('exit\n')
time.sleep(1)
chan.close()
ssh.close()
print('\nDone.')
