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

run("configure", 2)
run("top", 1)

# Check address-family options
run("protocols isis instance 1 address-family ?", 3)

# Try ipv4-unicast
run("protocols isis instance 1 address-family ipv4-unicast ?", 3)
run("protocols isis instance 1 address-family ipv4-unicast admin-state enabled", 2)

# Interface options
run("protocols isis instance 1 interface ?", 3)

# Add source-facing interface (ge800-0/0/31) for ISIS adjacency with Spirent
run("protocols isis instance 1 interface ge800-0/0/31 ?", 3)
run("protocols isis instance 1 interface ge800-0/0/31 admin-state enabled", 2)
run("protocols isis instance 1 interface ge800-0/0/31 network-type point-to-point", 2)

# Add loopback for advertising 8.8.8.8/32
run("protocols isis instance 1 interface lo1 ?", 3)
run("protocols isis instance 1 interface lo1 admin-state enabled", 2)
run("protocols isis instance 1 interface lo1 passive", 2)

run("top", 1)

# Commit
out = run("commit", 15)
if "succeed" in out.lower():
    print("  Commit OK")
else:
    print("  Check commit result above")

run("exit", 2)

# Verify
print("\n=== ISIS Config ===")
run("show config protocols isis", 5)

print("\n=== ISIS Neighbors ===")
run("show isis neighbors", 6)

print("\n=== ISIS Database ===")
run("show isis database", 6)

ssh.close()
