import paramiko, time, re
HOST = '100.64.6.171'
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username='dnroot', password='dnroot', look_for_keys=False)
chan = ssh.invoke_shell(width=300, height=500)
time.sleep(2)
chan.recv(65535)
def run(cmd, wait=5):
    chan.send(cmd + '\n')
    time.sleep(wait)
    out = ''
    while chan.recv_ready():
        out += chan.recv(65535).decode('utf-8', errors='replace')
        time.sleep(0.3)
    return out
def prun(cmd, wait=5):
    out = run(cmd, wait)
    out = re.sub(r'\x1b\[[0-9;]*m', '', out)
    print('\n=== %s ===' % cmd)
    print(out.strip())
    return out
prun('show pim summary')
prun('show multicast route summary')
prun('show pim tree group 232.0.0.1 source 3.5.0.2 | no-more')
prun('show pim tree group 232.0.100.0 source 3.5.0.2 | no-more')
prun('show pim tree group 232.0.200.0 source 3.5.0.2 | no-more')
prun('show multicast route group 232.0.0.1 source 3.5.0.2 | no-more')
prun('show multicast route group 232.0.200.0 source 3.5.0.2 | no-more')
prun('show interfaces ge800-0/0/31 | no-more', wait=8)
prun('show interfaces ge800-0/0/10.1 | no-more', wait=5)
prun('show interfaces ge800-0/0/10.2 | no-more', wait=5)
prun('show interfaces ge800-0/0/10.3 | no-more', wait=5)
prun('show system alarms')
prun('show pim statistics')
ssh.close()
