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

# Add lo1 to PIM
print("Adding lo1 to PIM...")
run("interfaces lo1 admin-state enabled", 1)
run("interfaces lo1 ipv4-address 8.8.8.8/32", 1)
run("protocols pim address-family ipv4 interface lo1 admin-state enabled", 2)

# Add static RP
print("Adding static-rp 8.8.8.8...")
run("protocols pim static-rp 8.8.8.8", 2)

# Commit
print("\nCommitting...")
out = run("commit", wait=30)
print(out)

# Verify
print("Verifying...")
out = run("show pim rps", 6)
for line in out.split("\n"):
    s = line.strip()
    if s and "Q3D" not in s and "show" not in s:
        print("  %s" % s)

out = run("show pim ranges", 5)
for line in out.split("\n"):
    s = line.strip()
    if s and "Q3D" not in s and "show" not in s:
        print("  %s" % s)

out = run("show pim interface", 8)
for line in out.split("\n"):
    s = line.strip()
    if "ge800" in s or "lo1" in s or "Interface" in s or "+" in s:
        print("  %s" % s)

ssh.close()
print("\nDone — ASM configured with static RP 8.8.8.8 on lo1.")
