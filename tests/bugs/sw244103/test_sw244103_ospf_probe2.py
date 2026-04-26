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

print("=== Probing OSPF instance CLI ===")
run("configure", 5)

out = run("protocols ospf instance test1 ?", 8)
print(f"protocols ospf instance test1 ?\n{out}\n")

# Go back to top
run("top", 3)

# Try flat-path approach
out = run("protocols ospf instance test1 router-id 10.100.1.1", 5)
print(f"router-id:\n{out}\n")
run("top", 3)

out = run("protocols ospf instance test1 area 0.0.0.0 interface ge400-0/0/3.100 admin-state enabled", 5)
print(f"area interface:\n{out}\n")
run("top", 3)

# Check what we have
out = run("show config protocols ospf | no-more", 10)
print(f"Config so far:\n{out}\n")

# Rollback
run("rollback", 5)
run("end", 3)

chan.send('exit\n')
time.sleep(2)
ssh.close()
print("Done.")
