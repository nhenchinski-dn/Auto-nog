import paramiko, time, re

HOST = 'WKY1C7VD00008P2'
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(HOST, username='dnroot', password='dnroot',
               look_for_keys=False, allow_agent=False, timeout=15)
shell = client.invoke_shell(width=250, height=5000)
time.sleep(6)
shell.recv(65535)

def run(cmd, wait=5):
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

print("=== Route for 1.1.1.1 ===")
print(run('show route 1.1.1.1/32 | no-more', 8))

print("=== Route for 1.0.0.0/8 (any covering route?) ===")
print(run('show route 1.0.0.0/8 | no-more', 8))

print("=== uRPF config on interface ===")
out = run('show interfaces ge400-0/0/3.100 | no-more', 8)
for line in out.split('\n'):
    if 'urpf' in line.lower() or 'rpf' in line.lower():
        print(line.strip())

print("\n=== Default route? ===")
print(run('show route 0.0.0.0/0 | no-more', 8))

print("\n=== IS-IS routes matching 1.x.x.x ===")
out = run('show route protocol isis | no-more', 10)
for line in out.split('\n'):
    if line.strip().startswith('I') and '1.0.' in line:
        print(line.strip())

client.close()
