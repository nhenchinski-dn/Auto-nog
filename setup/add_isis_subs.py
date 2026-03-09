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
    print("  > %s" % cmd)
    return out

run("configure", 2)
run("top", 1)

# Use correct DNOS CLI hierarchy: protocols isis instance 1 interface ...
for i in range(1, 11):
    print("Adding ge800-0/0/10.%d to ISIS instance 1..." % i)
    run("protocols isis instance 1 interface ge800-0/0/10.%d admin-state enabled" % i, 1)
    run("protocols isis instance 1 interface ge800-0/0/10.%d network-type point-to-point" % i, 1)
    run("protocols isis instance 1 interface ge800-0/0/10.%d address-family ipv4-unicast" % i, 1)
    run("top", 1)

# Also make lo1 passive
run("protocols isis instance 1 interface lo1 passive true", 2)

print("\nCommitting...")
out = run("commit", wait=30)
print(out)

# Verify
out = run("show isis interface", 8)
print("\nISIS Interfaces:")
print(out)

out = run("show isis neighbors", 8)
print("\nISIS Neighbors:")
print(out)

ssh.close()
print("\nDone.")
