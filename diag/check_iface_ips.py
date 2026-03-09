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

def run(cmd, wait=3):
    chan.send(cmd + "\n")
    time.sleep(wait)
    out = ""
    while chan.recv_ready():
        out += chan.recv(65535).decode("utf-8", errors="replace")
        time.sleep(0.3)
    return out

for i in range(1, 11):
    out = run("show config interfaces ge800-0/0/10.%d" % i, 3)
    print("=== ge800-0/0/10.%d ===" % i)
    for line in out.split("\n"):
        s = line.strip()
        if s and "Q3D" not in s and "show" not in s and "config-" not in s:
            print("  %s" % s)

out = run("show config interfaces ge800-0/0/31", 3)
print("=== ge800-0/0/31 ===")
for line in out.split("\n"):
    s = line.strip()
    if s and "Q3D" not in s and "show" not in s and "config-" not in s:
        print("  %s" % s)

out = run("show config interfaces lo1", 3)
print("=== lo1 ===")
for line in out.split("\n"):
    s = line.strip()
    if s and "Q3D" not in s and "show" not in s and "config-" not in s:
        print("  %s" % s)

out = run("show pim neighbors", 5)
print("\n=== PIM Neighbors ===")
print(out)

ssh.close()
