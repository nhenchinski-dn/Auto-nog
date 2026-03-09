import paramiko, time
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('100.64.6.171', username='dnroot', password='dnroot', timeout=10, look_for_keys=False, allow_agent=False)
chan = ssh.invoke_shell(width=200)
time.sleep(5)
chan.recv(65535)
for cmd in ['show interfaces breakout | no-more', 'show interfaces | no-more']:
    chan.send(cmd + '\n')
    time.sleep(10)
    out = b''
    while chan.recv_ready():
        out += chan.recv(65535)
    print(f'=== {cmd} ===')
    print(out.decode(errors='replace'))
chan.send('exit\n')
time.sleep(1)
chan.close()
ssh.close()
