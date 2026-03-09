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

# Clear stuck state
run("no", 2)
time.sleep(1)

run("configure", 2)
run("rollback 0", 5)
run("exit", 2)

# Fresh start
print("=== Configuring ISIS step by step ===\n")
run("configure", 2)
run("top", 1)

# Navigate into isis instance
run("protocols isis instance 1", 2)
run("iso-network 49.0001.0080.0800.8008.00", 3)
run("admin-state enabled", 2)

# Enable ipv4-unicast
run("address-family ipv4-unicast", 2)
run("top", 1)

# Add source-facing interface
run("protocols isis instance 1 interface ge800-0/0/31", 2)
run("admin-state enabled", 2)
run("network-type point-to-point", 2)
run("top", 1)

# Add loopback (passive) to advertise 8.8.8.8/32
run("protocols isis instance 1 interface lo1", 2)
run("admin-state enabled", 2)
run("passive ?", 3)

run("top", 1)

# Commit
out = run("commit", 15)
if "succeed" in out.lower():
    print("  Commit OK")
elif "error" in out.lower():
    print("  COMMIT ERROR - checking...")
    run("show config protocols isis", 5)
    # Try without passive
    run("top", 1)
    out2 = run("commit", 15)
else:
    print("  Check output above")

run("exit", 2)

# Verify
print("\n=== ISIS Config ===")
run("show config protocols isis", 5)

print("\n=== Wait 15s for ISIS adjacency ===")
time.sleep(15)

print("\n=== ISIS Neighbors ===")
run("show isis neighbors", 6)

print("\n=== ISIS Routes ===")
run("show isis routes", 6)

print("\n=== PIM tree check ===")
run("show pim tree", 8)

ssh.close()
