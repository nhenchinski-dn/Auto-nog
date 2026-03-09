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

run("show pim tree", 8)
run("show pim neighbors", 8)
run("show pim rps", 6)
run("show multicast route", 8)
run("show pim statistics", 6)
run("show interfaces ge800-0/0/10 | include ip", 5)
run("show interfaces ge800-0/0/31 | include ip", 5)

ssh.close()
