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

def run(cmd, wait=6):
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

# Check multicast forwarding table for 239.1.1.1
run("show multicast forwarding-table", 8)

# Check PIM tree again
run("show pim tree", 8)

# Check multicast route for 239.1.1.1 specifically
run("show multicast route", 8)

# Check PIM RPF for source
run("show pim rpf", 6)

# Check if there's asm-override or any special config
run("show config protocols pim", 5)

ssh.close()
