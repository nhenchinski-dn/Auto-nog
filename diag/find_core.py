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

run("show system coredump", 8)
run("show version", 5)
run("run ls /var/core/", 8)
run("run ls /var/crash/", 8)
run("run ls /var/log/core/", 8)
run("run find / -name '*.core' -o -name 'core.*' 2>/dev/null", 15)

ssh.close()
