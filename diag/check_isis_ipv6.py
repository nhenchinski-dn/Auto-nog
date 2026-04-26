import paramiko, time, re

HOST = 'WKY1C7VD00008P2'

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(HOST, username='dnroot', password='dnroot',
               look_for_keys=False, allow_agent=False, timeout=15)
shell = client.invoke_shell(width=250, height=5000)
time.sleep(6)
shell.recv(65535)

def run(cmd, wait=8):
    shell.send(cmd + '\n')
    time.sleep(wait)
    out = b''
    while shell.recv_ready():
        out += shell.recv(65535)
        time.sleep(0.3)
    txt = out.decode('utf-8', errors='replace')
    txt = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', txt)
    txt = re.sub(r'\r', '', txt)
    return txt

cmds = [
    'show isis database detail | no-more',
    'show isis database self-originate detail | no-more',
]

for c in cmds:
    print(f'\n{"="*60}')
    print(f'CMD: {c}')
    print('='*60)
    print(run(c, wait=10))

client.close()
