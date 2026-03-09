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

print("Configuring ISIS on DUT\n")

run("configure", 2)
run("top", 1)

# ISIS instance 1
# NET address: area.systemid.nsel  (49.0001.0080.0800.8008.00)
# area=49.0001, system-id derived from 8.8.8.8 -> 0080.0800.8008, nsel=00
run("protocols isis instance 1 iso-network 49.0001.0080.0800.8008.00", 3)
run("protocols isis instance 1 admin-state enabled", 2)

# Enable IPv4 address-family
run("protocols isis instance 1 address-family ipv4 ?", 3)

ssh.close()
