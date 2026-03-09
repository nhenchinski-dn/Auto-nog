import paramiko, time, re, sys
sys.stdout.reconfigure(line_buffering=True)
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('100.64.6.171', username='dnroot', password='dnroot',
            timeout=15, look_for_keys=False, allow_agent=False)
chan = ssh.invoke_shell(width=300)
time.sleep(8)
chan.recv(65535)
a = re.compile(r'\x1b\[[0-9;]*m')

def r(c, w=6):
    chan.send(c + '\n')
    time.sleep(w)
    o = b''
    while chan.recv_ready():
        o += chan.recv(65535)
    return a.sub('', o.decode(errors='replace'))

print('=== PIM neighbor on IIF ===')
t = r('show pim neighbor | include ge800-0/0/8 | no-more')
for l in t.split('\n'):
    if 'ge800-0/0/8' in l and '|' in l:
        print(l.strip())

print('\n=== MC route summary ===')
t = r('show multicast route summary | no-more')
for l in t.split('\n'):
    if 'Number' in l:
        print(l.strip())

print('\n=== Counters ge800-0/0/8 ===')
t = r('show interfaces counters | include "ge800-0/0/8 " | no-more')
for l in t.split('\n'):
    if 'ge800-0/0/8 ' in l and '|' in l:
        print(l.strip())

print('\n=== Counters ge800-0/0/9 ===')
t = r('show interfaces counters | include "ge800-0/0/9 " | no-more')
for l in t.split('\n'):
    if 'ge800-0/0/9 ' in l and '|' in l:
        print(l.strip())

chan.send('exit\n')
time.sleep(1)
chan.close()
ssh.close()
print('\nDONE')
