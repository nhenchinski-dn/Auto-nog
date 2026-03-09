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
    text = out.decode(errors='replace')
    return text

commands = [
    ('show pim summary | no-more', 8),
    ('show pim neighbors | no-more', 8),
    ('show pim tree | no-more', 15),
    ('show pim statistics | no-more', 8),
    ('show multicast route summary | no-more', 8),
    ('show multicast route | no-more', 15),
    ('show interfaces ge800-0/0/30 counters | no-more', 5),
    ('show interfaces ge800-0/0/31 counters | no-more', 5),
    ('show interfaces ge800-0/0/30.1 | no-more', 5),
    ('show interfaces ge800-0/0/31.1 | no-more', 5),
    ('show system information | no-more', 5),
]

for cmd, wait in commands:
    short = cmd.replace(' | no-more', '')
    print(f'\n{"="*80}')
    print(f'=== {short} ===')
    print(f'{"="*80}')
    text = run(cmd, wait)
    for l in text.split('\n'):
        if 'no-more' not in l:
            print(l.rstrip())

chan.send('exit\n')
time.sleep(1)
chan.close()
ssh.close()
print('\n\nDONE')
