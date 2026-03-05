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
    print("  [%s]" % cmd)
    for line in out.strip().split("\n"):
        s = line.strip()
        if s and "Q3D-nog" not in s and not s.startswith(cmd[:15]):
            print("    %s" % s)
    return out

# Check current full config
print("=== Current ISIS config ===")
run("show config protocols isis", 5)

# Fix: add address-family ipv4-unicast under interface
run("configure", 2)
run("top", 1)

# Enable interface address families
run("protocols isis instance 1 interface ge800-0/0/31", 2)
run("address-family ipv4-unicast ?", 3)
run("address-family ipv4-unicast admin-state enabled", 2)
run("top", 1)

run("protocols isis instance 1 interface lo1", 2)
run("address-family ipv4-unicast admin-state enabled", 2)
run("top", 1)

out = run("commit", 15)
if "succeed" in out.lower():
    print("  Commit OK")

run("exit", 2)

run("show config protocols isis", 5)

print("\nWaiting 20s...")
time.sleep(20)

run("show isis interface", 6)
run("show isis neighbors", 6)

ssh.close()
