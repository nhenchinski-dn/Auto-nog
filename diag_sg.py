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

def run(cmd, wait=5):
    chan.send(cmd + "\n")
    time.sleep(wait)
    out = ""
    while chan.recv_ready():
        out += chan.recv(65535).decode("utf-8", errors="replace")
        time.sleep(0.3)
    print("\n>>> %s" % cmd)
    for line in out.strip().split("\n"):
        s = line.strip()
        if s and "Q3D-nog" not in s:
            print("  %s" % s)
    return out

# Check ARP - is source 3.5.0.2 resolved?
run("show arp", 5)

# Check route to source
run("show route 3.5.0.2", 5)

# Check PIM Register stats
run("show pim statistics", 6)

# Check if asm-override is needed
run("configure", 2)
run("protocols pim asm-override ?", 3)
run("rollback 0", 2)
run("exit", 2)

# Check multicast rpf for source 3.5.0.2
run("show multicast rpf 3.5.0.2", 5)

ssh.close()
