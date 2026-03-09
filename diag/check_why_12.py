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

# Check PIM interface join counts
print("=" * 60)
print("PIM interface state")
print("=" * 60)
out = run_nopager("show pim interface", 15)
for line in out.split("\n"):
    s = line.strip()
    if "ge800" in s or "Interface" in s or "+" in s or "Join" in s:
        print("  %s" % s)

# Check PIM neighbors - which ones are sending joins
print("\n" + "=" * 60)
print("PIM neighbors")
print("=" * 60)
out = run_nopager("show pim neighbors", 10)
for line in out.split("\n"):
    s = line.strip()
    if s and "Q3D" not in s and "show" not in s:
        print("  %s" % s)

# Check multicast route summary
print("\n" + "=" * 60)
print("Multicast route summary")
print("=" * 60)
out = run_nopager("show multicast route summary", 10)
for line in out.split("\n"):
    s = line.strip()
    if s and "Q3D" not in s and "show" not in s:
        print("  %s" % s)

# Check PIM statistics per interface to see who is sending joins
print("\n" + "=" * 60)
print("PIM Join/Prune Rx per interface")
print("=" * 60)
for i in range(1, 21):
    iface = "ge800-0/0/10.%d" % i
    out = run_nopager("show pim statistics interface %s" % iface, 5)
    jp_match = re.search(r"Join/Prune\s+\|\s*(\d+)\s*\|\s*(\d+)", out)
    if jp_match:
        rx, tx = jp_match.group(1), jp_match.group(2)
        print("  %-20s  Join/Prune Rx: %s  Tx: %s" % (iface, rx, tx))
    elif "Unknown" in out or "ERROR" in out:
        # Try without interface filter
        break

# If per-interface stats didn't work, check PIM tree for OIF distribution
print("\n" + "=" * 60)
print("Sampling PIM tree - checking OIF lists")
print("=" * 60)
out = run_nopager("show pim tree", 30)

current_sg = None
oif_per_iface = defaultdict(int)
total_entries = 0
entries_with_oifs = defaultdict(int)

for line in out.split("\n"):
    line_clean = re.sub(r'\x1b\[[^m]*m', '', line).strip()
    sg_match = re.match(r'\((\d+\.\d+\.\d+\.\d+),\s*(\d+\.\d+\.\d+\.\d+)\)\s*(SSM|SM)', line_clean)
    if sg_match:
        total_entries += 1
        current_sg = True
    elif current_sg and re.match(r'ge800-0/0/10', line_clean):
        iface = line_clean.split(",")[0].strip()
        oif_per_iface[iface] += 1

print("  Entries sampled: %d" % total_entries)
print("\n  OIF count per sub-interface:")
for iface in sorted(oif_per_iface.keys(), key=lambda x: (x.split(".")[0], int(x.split(".")[-1]) if "." in x else 0)):
    print("    %-20s: %d entries" % (iface, oif_per_iface[iface]))

ssh.close()
