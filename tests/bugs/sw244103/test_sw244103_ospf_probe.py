import paramiko, time, re

HOST = '100.64.8.59'
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username='dnroot', password='dnroot', timeout=30,
            look_for_keys=False, allow_agent=False)
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
    return text.strip()

print("=== Probing OSPF CLI syntax ===")
run("configure", 5)

# Try without instance
out = run("protocols ospf ?", 8)
print(f"protocols ospf ?\n{out}\n")

out = run("protocols ospf router-id ?", 8)
print(f"protocols ospf router-id ?\n{out}\n")

out = run("protocols ospf area ?", 8)
print(f"protocols ospf area ?\n{out}\n")

out = run("protocols ospf area 0.0.0.0 ?", 8)
print(f"protocols ospf area 0.0.0.0 ?\n{out}\n")

out = run("protocols ospf area 0.0.0.0 interface ?", 8)
print(f"protocols ospf area 0.0.0.0 interface ?\n{out}\n")

run("end", 3)
chan.send('exit\n')
time.sleep(2)
ssh.close()
print("Done.")
