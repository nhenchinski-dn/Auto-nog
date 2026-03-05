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

def run(cmd, wait=5):
    chan.send(cmd + "\n")
    time.sleep(wait)
    out = ""
    while chan.recv_ready():
        out += chan.recv(65535).decode("utf-8", errors="replace")
        time.sleep(0.3)
    return out

# Disable pagination
run("terminal length 0", 3)
run("set cli screen-length 0", 3)
run("environment screen-length 0", 3)

print("Collecting PIM tree (full, no pagination)...")
tree_out = ""
chan.send("show pim tree\n")
time.sleep(5)
# Keep reading until prompt returns
for _ in range(120):
    if chan.recv_ready():
        chunk = chan.recv(65535).decode("utf-8", errors="replace")
        tree_out += chunk
        time.sleep(0.5)
    else:
        time.sleep(1)
    if tree_out.rstrip().endswith("#") and len(tree_out) > 500:
        # Check if we got enough data and the prompt appeared
        last_lines = tree_out.strip().split("\n")[-3:]
        if any("Q3D-nog#" in l for l in last_lines):
            break

sg_entries = re.findall(r"\(\d+\.\d+\.\d+\.\d+,\s*\d+\.\d+\.\d+\.\d+\)\s*(SSM|SM)", tree_out)
star_g = re.findall(r"\(\*,\s*\d+\.\d+\.\d+\.\d+\)\s*(SSM|SM)", tree_out)
truncated = "More" in tree_out

print("PIM tree (S,G) entries: %d" % len(sg_entries))
print("PIM tree (*,G) entries: %d" % len(star_g))
print("PIM tree total: %d" % (len(sg_entries) + len(star_g)))
if truncated:
    print("WARNING: Output still truncated")

# Extract unique groups
groups = set()
for m in re.finditer(r"\(\d+\.\d+\.\d+\.\d+,\s*(\d+\.\d+\.\d+\.\d+)\)", tree_out):
    groups.add(m.group(1))
if groups:
    sorted_groups = sorted(groups, key=lambda x: list(map(int, x.split("."))))
    print("\nGroup range: %s to %s" % (sorted_groups[0], sorted_groups[-1]))
    print("Unique groups: %d" % len(groups))

# Now check multicast route count
print("\n" + "=" * 60)
print("Collecting multicast route (full)...")
mroute_out = ""
chan.send("show multicast route\n")
time.sleep(5)
for _ in range(120):
    if chan.recv_ready():
        chunk = chan.recv(65535).decode("utf-8", errors="replace")
        mroute_out += chunk
        time.sleep(0.5)
    else:
        time.sleep(1)
    if mroute_out.rstrip().endswith("#") and len(mroute_out) > 500:
        last_lines = mroute_out.strip().split("\n")[-3:]
        if any("Q3D-nog#" in l for l in last_lines):
            break

sg_mroutes = re.findall(r"\(\d+\.\d+\.\d+\.\d+,\s*\d+\.\d+\.\d+\.\d+/32\)", mroute_out)
star_mroutes = re.findall(r"\(\*,\s*\d+\.\d+\.\d+\.\d+/\d+\)", mroute_out)
failed_entries = re.findall(r"Flags:.*?F", mroute_out)

print("Multicast route (S,G) entries: %d" % len(sg_mroutes))
print("Multicast route (*,G) entries: %d" % len(star_mroutes))
print("Multicast route total: %d" % (len(sg_mroutes) + len(star_mroutes)))
if "More" in mroute_out:
    print("WARNING: Output still truncated")

# Count entries with F (Failed) flag
f_count = 0
for line in mroute_out.split("\n"):
    if "Flags:" in line and " F" in line:
        f_count += 1
print("Entries with F (Failed to install) flag: %d" % f_count)

# Check PIM interface join counts
print("\n" + "=" * 60)
iface_out = run("show pim interface", 6)
print("PIM Interface state:")
print(iface_out)

# Check multicast config for any limits
cfg_out = run("show config multicast", 5)
print("Multicast config:")
print(cfg_out)

ssh.close()
