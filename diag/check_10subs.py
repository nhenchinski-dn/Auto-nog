import paramiko, time, re
from collections import defaultdict

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
                time.sleep(0.05)
        else:
            time.sleep(0.5)
    return out

print("=" * 60)
print("PIM neighbors per interface")
print("=" * 60)
out = run_nopager("show pim neighbors", 10)
for line in out.split("\n"):
    s = line.strip()
    if s and "Q3D" not in s and "show" not in s:
        print("  %s" % s)

print("\n" + "=" * 60)
print("Multicast route summary")
print("=" * 60)
out = run_nopager("show multicast route summary", 10)
for line in out.split("\n"):
    s = line.strip()
    if s and "Q3D" not in s and "show" not in s:
        print("  %s" % s)

print("\n" + "=" * 60)
print("Forwarding-table summary")
print("=" * 60)
out = run_nopager("show multicast forwarding-table summary", 10)
for line in out.split("\n"):
    s = line.strip()
    if s and "Q3D" not in s and "show" not in s:
        print("  %s" % s)

print("\n" + "=" * 60)
print("Sampling first 20 (S,G) entries for OIF count...")
print("=" * 60)

# Get first page of PIM tree to check OIF lists
out = run_nopager("show pim tree", 30)

# Parse entries and their output interfaces
current_sg = None
entries = {}
for line in out.split("\n"):
    line_clean = re.sub(r'\x1b\[[^m]*m', '', line).strip()
    sg_match = re.match(r'\((\d+\.\d+\.\d+\.\d+),\s*(\d+\.\d+\.\d+\.\d+)\)\s*(SSM|SM)', line_clean)
    if sg_match:
        current_sg = "%s, %s" % (sg_match.group(1), sg_match.group(2))
        entries[current_sg] = []
    elif current_sg and "Output Interface List:" in line_clean:
        pass
    elif current_sg and re.match(r'ge800-0/0/10', line_clean):
        iface = line_clean.split(",")[0].strip()
        entries[current_sg].append(iface)

# Print first 20
count = 0
for sg, oifs in entries.items():
    if count >= 20:
        break
    print("  (%s) — %d OIFs: %s" % (sg, len(oifs), ", ".join(oifs)))
    count += 1

# OIF frequency
iface_count = defaultdict(int)
for sg, oifs in entries.items():
    for oif in oifs:
        iface_count[oif] += 1

print("\n" + "=" * 60)
print("OIF frequency (from sampled entries):")
print("=" * 60)
for iface in sorted(iface_count.keys(), key=lambda x: (x.split(".")[0], int(x.split(".")[-1]) if "." in x else 0)):
    print("  %-20s: %d" % (iface, iface_count[iface]))

ssh.close()
