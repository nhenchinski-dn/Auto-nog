import paramiko, time, re, json

HOST = '100.64.8.59'

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username='dnroot', password='dnroot',
            look_for_keys=False, allow_agent=False, timeout=30)
chan = ssh.invoke_shell(width=300, height=5000)
time.sleep(6)
chan.recv(65535)

def run(cmd, wait=8):
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
    "show system version | no-more",
    "show interfaces brief | no-more",
    "show config interfaces p333 | no-more",
    "show config interfaces p204 | no-more",
    "show route vrf default table ipv4-unicast | no-more",
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

with open('/home/dn/output/sw244103_recon.json', 'w') as f:
    json.dump(results, f, indent=2)

print("\nDone. Results saved to /home/dn/output/sw244103_recon.json")
