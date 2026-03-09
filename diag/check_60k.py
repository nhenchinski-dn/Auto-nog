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
    while (time.time() - last_data_time) < 10:
        if chan.recv_ready():
            chunk = chan.recv(65535).decode("utf-8", errors="replace")
            out += chunk
            last_data_time = time.time()
            if "More" in chunk:
                chan.send(" ")
                time.sleep(0.2)
        else:
            time.sleep(0.5)
        if (time.time() - last_data_time) > max_wait:
            break
    return out

print("=" * 60)
print("1. Multicast route summary")
print("=" * 60)
out = run_nopager("show multicast route summary", 15)
for line in out.split("\n"):
    s = line.strip()
    if s and "Q3D" not in s and "More" not in s and "show " not in s:
        print("  %s" % s)

print("\n" + "=" * 60)
print("2. Multicast config (limits)")
print("=" * 60)
out = run_nopager("show config multicast", 10)
for line in out.split("\n"):
    s = line.strip()
    if s and "Q3D" not in s and "#" not in s:
        print("  %s" % s)

print("\n" + "=" * 60)
print("3. PIM interface join counts")
print("=" * 60)
out = run_nopager("show pim interface", 10)
for line in out.split("\n"):
    s = line.strip()
    if s and ("Interface" in s or "ge800" in s or "Join" in s or "---" in s or "+" in s):
        print("  %s" % s)

print("\n" + "=" * 60)
print("4. Check for Failed (F flag) entries in mroute")
print("=" * 60)
out = run_nopager("show multicast route summary", 10)
for line in out.split("\n"):
    s = line.strip()
    if "failed" in s.lower() or "limit" in s.lower() or "Number" in s:
        print("  %s" % s)

print("\n" + "=" * 60)
print("5. PIM statistics")
print("=" * 60)
out = run_nopager("show pim statistics", 10)
for line in out.split("\n"):
    s = line.strip()
    if s and "Q3D" not in s and "More" not in s:
        print("  %s" % s)

print("\n" + "=" * 60)
print("6. Multicast route count by interface")
print("=" * 60)
out = run_nopager("show multicast route summary", 10)
print(out)

print("\n" + "=" * 60)
print("7. Check multicast maximum-routes config")
print("=" * 60)
out = run_nopager("show config multicast maximum-routes", 10)
for line in out.split("\n"):
    s = line.strip()
    if s:
        print("  %s" % s)

out = run_nopager("show multicast route summary vrf default", 10)
for line in out.split("\n"):
    s = line.strip()
    if s and "Q3D" not in s:
        print("  %s" % s)

ssh.close()
