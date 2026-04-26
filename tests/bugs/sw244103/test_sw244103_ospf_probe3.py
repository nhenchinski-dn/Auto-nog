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

run("configure", 5)

# Check area interface options
out = run("protocols ospf instance test1 area 0.0.0.0 interface ge400-0/0/3.100 ?", 8)
print(f"interface options:\n{out}\n")
run("top", 3)

# Try just creating the interface without admin-state
out = run("protocols ospf instance test1 area 0.0.0.0 interface ge400-0/0/3.100", 5)
print(f"create interface:\n{out}\n")
run("top", 3)

# Check network-type options
out = run("protocols ospf instance test1 area 0.0.0.0 interface ge400-0/0/3.100 network-type ?", 8)
print(f"network-type options:\n{out}\n")
run("top", 3)

# Now commit and see what we got
out = run("commit", 15)
print(f"commit:\n{out}\n")

out = run("show config protocols ospf | no-more", 10)
print(f"OSPF config:\n{out}\n")

# Now clean up
run("no protocols ospf instance test1", 5)
run("top", 3)
out = run("commit", 15)
print(f"cleanup commit:\n{out}\n")

run("end", 3)
chan.send('exit\n')
time.sleep(2)
ssh.close()
print("Done.")
