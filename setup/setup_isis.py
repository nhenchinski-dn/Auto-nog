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

# Check if ISIS already exists
out = run("show config protocols isis", 5)
print("Current ISIS config:")
print(out)

run("configure", 2)
run("top", 1)

# Configure ISIS instance
print("Configuring ISIS...")
run("protocols isis 1 admin-state enabled", 2)
run("protocols isis 1 net 49.0001.0080.0800.8008.00", 2)
run("protocols isis 1 level 2 metric-style wide", 2)

# Add lo1 to ISIS (passive, to advertise 8.8.8.8)
print("Adding lo1 to ISIS (passive)...")
run("protocols isis 1 interface lo1 admin-state enabled", 2)
run("protocols isis 1 interface lo1 passive true", 2)

# Add source interface
print("Adding ge800-0/0/31 to ISIS...")
run("protocols isis 1 interface ge800-0/0/31 admin-state enabled", 2)
run("protocols isis 1 interface ge800-0/0/31 point-to-point true", 2)

# Add all 10 sub-interfaces
for i in range(1, 11):
    print("Adding ge800-0/0/10.%d to ISIS..." % i)
    run("protocols isis 1 interface ge800-0/0/10.%d admin-state enabled" % i, 1)
    run("protocols isis 1 interface ge800-0/0/10.%d point-to-point true" % i, 1)

print("\nCommitting...")
out = run("commit", wait=30)
print(out)

# Verify
print("\nVerifying ISIS...")
out = run("show isis neighbors", 8)
print("ISIS Neighbors:")
print(out)

out = run("show isis interface", 8)
print("ISIS Interfaces:")
print(out)

ssh.close()
print("\nDone — ISIS configured on all interfaces with lo1 (8.8.8.8) advertised.")
