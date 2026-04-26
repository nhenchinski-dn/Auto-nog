import paramiko, time, re, json

HOST = '100.64.8.59'

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username='dnroot', password='dnroot',
            look_for_keys=False, allow_agent=False, timeout=30)
chan = ssh.invoke_shell(width=300, height=5000)
time.sleep(6)
chan.recv(65535)

def run(cmd, wait=10):
    chan.send(cmd + '\n')
    time.sleep(wait)
    out = b''
    while chan.recv_ready():
        out += chan.recv(65535)
        time.sleep(0.5)
    text = out.decode(errors='replace')
    text = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)
    text = re.sub(r'\r', '', text)
    text = re.sub(r'-- More -- \(Press q to quit\)\s*', '', text)
    return text

commands = [
    "show interfaces | no-more",
    "show config interfaces ge400-0/0/3 | no-more",
    "show config interfaces ge400-0/0/3.100 | no-more",
    "show interfaces detail ge400-0/0/3 | no-more",
    "show interfaces detail ge400-0/0/3.100 | no-more",
    "show config interfaces bundle-10 | no-more",
    "show config interfaces bundle-10.100 | no-more",
    "show lldp neighbors | no-more",
    "show config protocols static | no-more",
    "show config protocols bgp | no-more",
    "show config protocols ospf | no-more",
    "show config protocols isis | no-more",
]

results = {}
for cmd in commands:
    print(f"\n{'='*60}")
    print(f"CMD: {cmd}")
    print('='*60)
    output = run(cmd)
    print(output)
    results[cmd] = output

chan.send('exit\n')
time.sleep(2)
ssh.close()

with open('/home/dn/output/sw244103_recon2.json', 'w') as f:
    json.dump(results, f, indent=2)

print("\nDone.")
