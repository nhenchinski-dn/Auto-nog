import paramiko, time

HOST = "100.64.6.171"
USER = "dnroot"
PASS = "dnroot"

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASS, look_for_keys=False)
chan = ssh.invoke_shell(width=300, height=500)
time.sleep(2)
chan.recv(65535)

def run(cmd, wait=5):
    chan.send(cmd + "\n")
    time.sleep(wait)
    out = ""
    while chan.recv_ready():
        out += chan.recv(65535).decode("utf-8", errors="replace")
        time.sleep(0.3)
    print("=" * 60)
    print("CMD: %s" % cmd)
    print("=" * 60)
    print(out)
    return out

# Drop into Linux shell
run("run start shell", 3)
run("dnroot", 5)

# Find core files
run("find /var -name '*core*' -type f -size +1M 2>/dev/null | head -30", 15)
run("ls -lah /var/core/ 2>/dev/null", 5)
run("ls -lah /var/crash/ 2>/dev/null", 5)
run("find / -maxdepth 4 -name '*core*' -type d 2>/dev/null", 10)
run("find / -maxdepth 5 -name '*core*' -size +1M 2>/dev/null | head -30", 20)

# Find PIM binary
run("ps aux | grep -i pim", 5)
run("find / -maxdepth 5 -name '*pimd*' -type f 2>/dev/null | head -10", 15)

ssh.close()
