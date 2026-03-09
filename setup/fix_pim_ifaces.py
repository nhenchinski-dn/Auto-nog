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

# Check current config first
out = run("show config protocols pim", 8)
print("Current PIM config:")
print(out)

run("configure", 2)
run("top", 1)

# Re-add all interfaces
for i in range(1, 11):
    print("Adding ge800-0/0/10.%d to PIM..." % i)
    run("protocols pim address-family ipv4 interface ge800-0/0/10.%d admin-state enabled" % i, 1)

print("Adding ge800-0/0/31 to PIM...")
run("protocols pim address-family ipv4 interface ge800-0/0/31 admin-state enabled", 1)

print("Adding lo1 to PIM...")
run("protocols pim address-family ipv4 interface lo1 admin-state enabled", 1)

print("\nCommitting...")
out = run("commit", wait=30)
print(out)

# Verify
out = run("show pim interface", 8)
print("\nPIM interfaces:")
for line in out.split("\n"):
    s = line.strip()
    if "ge800" in s or "lo1" in s or "Interface" in s or "+" in s:
        print("  %s" % s)

out = run("show pim rps", 5)
print("\nRP:")
for line in out.split("\n"):
    s = line.strip()
    if "8.8.8.8" in s:
        print("  %s" % s)

ssh.close()
print("\nDone.")
