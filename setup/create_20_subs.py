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

for i in range(1, 21):
    ip = "3.5.%d.1/24" % (i + 1)
    print("Creating ge800-0/0/10.%d  IP %s  VLAN %d" % (i, ip, i))
    run("interfaces ge800-0/0/10.%d admin-state enabled" % i, 1)
    run("interfaces ge800-0/0/10.%d ipv4-address %s" % (i, ip), 1)
    run("interfaces ge800-0/0/10.%d vlan-id %d" % (i, i), 1)
    run("protocols pim address-family ipv4 interface ge800-0/0/10.%d admin-state enabled" % i, 1)

print("\nCommitting...")
out = run("commit", wait=30)
print(out)

# Verify
print("\nVerifying sub-interfaces...")
out = run("show interfaces ge800-0/0/10.*", 8)
for line in out.split("\n"):
    s = line.strip()
    if "ge800-0/0/10." in s and ("up" in s or "down" in s or "enabled" in s):
        print("  %s" % s)

out = run("show pim interface", 8)
for line in out.split("\n"):
    s = line.strip()
    if "ge800-0/0/10." in s:
        print("  %s" % s)

ssh.close()
print("\nDone — 20 sub-interfaces created.")
