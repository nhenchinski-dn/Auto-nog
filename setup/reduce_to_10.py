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

run("configure", 2)
run("top", 1)

for i in range(11, 21):
    print("Removing ge800-0/0/10.%d ..." % i)
    run("no protocols pim address-family ipv4 interface ge800-0/0/10.%d" % i, 1)
    run("no interfaces ge800-0/0/10.%d" % i, 1)

print("\nCommitting...")
out = run("commit", wait=30)
print(out)

# Verify
print("Verifying...")
out = run("show pim interface", 8)
for line in out.split("\n"):
    s = line.strip()
    if "ge800" in s or "Interface" in s or "+" in s:
        print("  %s" % s)

ssh.close()
print("\nDone — removed sub-interfaces 11-20, keeping 1-10.")
