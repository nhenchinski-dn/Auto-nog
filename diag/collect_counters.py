import paramiko, time, sys
sys.stdout.reconfigure(line_buffering=True)

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('100.64.6.171', username='dnroot', password='dnroot',
            timeout=10, look_for_keys=False, allow_agent=False)
chan = ssh.invoke_shell(width=300)
time.sleep(8)
chan.recv(65535)

def run(cmd, wait=8):
    chan.send(cmd + '\n')
    time.sleep(wait)
    out = b''
    while chan.recv_ready():
        out += chan.recv(65535)
    return out.decode(errors='replace')

commands = [
    ('show interfaces counters detail | include ge800-0/0/30 | no-more', 10),
    ('show interfaces counters detail | include ge800-0/0/31 | no-more', 10),
    ('show interfaces counters | include ge800-0/0/30 | no-more', 10),
    ('show interfaces counters | include ge800-0/0/31 | no-more', 10),
    ('show system | no-more', 8),
    ('show version | no-more', 8),
    ('show multicast route 239.1.1.1 | no-more', 10),
    ('show multicast route 239.1.1.50 | no-more', 10),
]

for cmd, wait in commands:
    short = cmd.replace(' | no-more', '')
    print(f'\n{"="*60}')
    print(f'CMD: {short}')
    print(f'{"="*60}')
    text = run(cmd, wait)
    for l in text.split('\n'):
        if 'no-more' not in l:
            print(l.rstrip())

chan.send('exit\n')
time.sleep(1)
chan.close()
ssh.close()
print('\nDONE')
