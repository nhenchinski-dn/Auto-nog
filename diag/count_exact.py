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

def run_nopager(cmd, max_wait=180):
    """Run command, pressing space to bypass -- More -- pagination"""
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
        if time.time() - last_data_time > max_wait:
            break
    return out

# Try to get multicast route summary
print("=" * 60)
print("Trying summary commands...")
print("=" * 60)

out = run_nopager("show multicast route summary", 30)
print("show multicast route summary:")
for line in out.split("\n"):
    s = line.strip()
    if s and "Q3D" not in s and "More" not in s:
        print("  %s" % s)

out = run_nopager("show pim tree summary", 30)
print("\nshow pim tree summary:")
for line in out.split("\n"):
    s = line.strip()
    if s and "Q3D" not in s and "More" not in s:
        print("  %s" % s)

# Now get the full PIM tree with space-pressing to bypass More
print("\n" + "=" * 60)
print("Collecting full PIM tree (pressing space through pages)...")
print("=" * 60)

tree_out = run_nopager("show pim tree", 180)

sg_entries = re.findall(r"\(\d+\.\d+\.\d+\.\d+,\s*(\d+\.\d+\.\d+\.\d+)\)\s*(SSM|SM)", tree_out)
star_g = re.findall(r"\(\*,\s*\d+\.\d+\.\d+\.\d+\)\s*(SSM|SM)", tree_out)
truncated = "More" in tree_out and not tree_out.rstrip().endswith("#")

print("PIM tree (S,G) entries found: %d" % len(sg_entries))
print("PIM tree (*,G) entries found: %d" % len(star_g))

groups = set(m[0] for m in sg_entries)
if groups:
    sorted_groups = sorted(groups, key=lambda x: list(map(int, x.split("."))))
    print("Group range: %s to %s" % (sorted_groups[0], sorted_groups[-1]))
    print("Unique groups: %d" % len(groups))

if truncated:
    print("WARNING: Output may still be truncated")

# Now get full multicast route with space-pressing
print("\n" + "=" * 60)
print("Collecting full multicast route (pressing space through pages)...")
print("=" * 60)

mroute_out = run_nopager("show multicast route", 180)

sg_mroutes = re.findall(r"\(\d+\.\d+\.\d+\.\d+,\s*(\d+\.\d+\.\d+\.\d+)/32\)", mroute_out)
star_mroutes = re.findall(r"\(\*,\s*\d+\.\d+\.\d+\.\d+/\d+\)", mroute_out)

print("Multicast route (S,G) entries: %d" % len(sg_mroutes))
print("Multicast route (*,G) entries: %d" % len(star_mroutes))
print("Multicast route total: %d" % (len(sg_mroutes) + len(star_mroutes)))

# Count Failed entries
failed_lines = [l for l in mroute_out.split("\n") if "Flags:" in l and " F " in l]
print("Entries with F (Failed) flag: %d" % len(failed_lines))

mroute_groups = set(m for m in sg_mroutes)
if mroute_groups:
    sorted_mg = sorted(mroute_groups, key=lambda x: list(map(int, x.split("."))))
    print("Mroute group range: %s to %s" % (sorted_mg[0], sorted_mg[-1]))
    print("Unique mroute groups: %d" % len(mroute_groups))

if "More" in mroute_out and not mroute_out.rstrip().endswith("#"):
    print("WARNING: Output may still be truncated")

ssh.close()
