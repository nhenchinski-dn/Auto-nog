import paramiko, time, re

HOST = "100.64.6.171"
USER = "dnroot"
PASS = "dnroot"

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASS, look_for_keys=False)
chan = ssh.invoke_shell(width=300, height=1000)
time.sleep(2)
chan.recv(65535)

def run_nopager(cmd, max_wait=30):
    chan.send(cmd + "\n")
    time.sleep(3)
    out = ""
    last_data_time = time.time()
    while (time.time() - last_data_time) < max_wait:
        if chan.recv_ready():
            chunk = chan.recv(65535).decode("utf-8", errors="replace")
            out += chunk
            last_data_time = time.time()
            if "More" in chunk:
                chan.send(" ")
                time.sleep(0.1)
        else:
            time.sleep(0.5)
    return out

print("=" * 60)
print("PIM interface join counts")
print("=" * 60)
out = run_nopager("show pim interface", 15)
for line in out.split("\n"):
    s = line.strip()
    if "ge800" in s or "Interface" in s or "---" in s or "+" in s:
        print("  %s" % s)

print("\n" + "=" * 60)
print("Multicast route summary")
print("=" * 60)
out = run_nopager("show multicast route summary", 15)
for line in out.split("\n"):
    s = line.strip()
    if s and "Q3D" not in s and "show" not in s:
        print("  %s" % s)

print("\n" + "=" * 60)
print("Multicast forwarding-table summary")
print("=" * 60)
out = run_nopager("show multicast forwarding-table summary", 15)
for line in out.split("\n"):
    s = line.strip()
    if s and "Q3D" not in s and "show" not in s:
        print("  %s" % s)

ssh.close()
