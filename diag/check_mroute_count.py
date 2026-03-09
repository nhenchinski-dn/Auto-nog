import paramiko, time, re

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
    return out

print("=" * 60)
print("Checking PIM tree count...")
print("=" * 60)

tree_out = run("show pim tree", 15)
sg_entries = re.findall(r"\(\d+\.\d+\.\d+\.\d+,\s*\d+\.\d+\.\d+\.\d+\)\s*(SSM|SM)", tree_out)
star_g_entries = re.findall(r"\(\*,\s*\d+\.\d+\.\d+\.\d+\)\s*(SSM|SM)", tree_out)
print("PIM tree (S,G) entries: %d" % len(sg_entries))
print("PIM tree (*,G) entries: %d" % len(star_g_entries))
print("PIM tree total: %d" % (len(sg_entries) + len(star_g_entries)))

# Check if output was truncated by "More"
if "More" in tree_out:
    print("WARNING: PIM tree output was TRUNCATED (-- More --)")

print("\n" + "=" * 60)
print("Checking multicast route count...")
print("=" * 60)

mroute_out = run("show multicast route", 15)
sg_mroutes = re.findall(r"\(\d+\.\d+\.\d+\.\d+,\s*\d+\.\d+\.\d+\.\d+/32\)", mroute_out)
star_mroutes = re.findall(r"\(\*,\s*\d+\.\d+\.\d+\.\d+/\d+\)", mroute_out)
print("Multicast route (S,G) entries: %d" % len(sg_mroutes))
print("Multicast route (*,G) entries: %d" % len(star_mroutes))
print("Multicast route total: %d" % (len(sg_mroutes) + len(star_mroutes)))

if "More" in mroute_out:
    print("WARNING: Multicast route output was TRUNCATED (-- More --)")

print("\n" + "=" * 60)
print("Checking multicast summary/limits...")
print("=" * 60)

out = run("show multicast route summary", 8)
print(out)

out = run("show pim summary", 8)
print(out)

out = run("show multicast limits", 8)
print(out)

out = run("show config multicast", 5)
print("CONFIG:\n%s" % out)

out = run("show pim interface", 6)
print(out)

ssh.close()
