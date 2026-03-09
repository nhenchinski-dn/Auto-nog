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

def run_nopager(cmd, wait=5, max_pages=50):
    chan.send(cmd + "\n")
    time.sleep(wait)
    out = ""
    while chan.recv_ready():
        out += chan.recv(65535).decode("utf-8", errors="replace")
        time.sleep(0.3)
    pages = 0
    while "-- More --" in out and pages < max_pages:
        chan.send(" ")
        time.sleep(2)
        while chan.recv_ready():
            out += chan.recv(65535).decode("utf-8", errors="replace")
            time.sleep(0.3)
        pages += 1
    return out

print("=" * 70)
print("1. PIM NEIGHBORS")
print("=" * 70)
out = run("show pim neighbors", 6)
print(out)

print("=" * 70)
print("2. PIM INTERFACE")
print("=" * 70)
out = run("show pim interface", 8)
print(out)

print("=" * 70)
print("3. PIM RPS")
print("=" * 70)
out = run("show pim rps", 5)
print(out)

print("=" * 70)
print("4. PIM TREE (first 200 lines)")
print("=" * 70)
out = run_nopager("show pim tree", wait=8, max_pages=10)
lines = out.split("\n")
for line in lines[:200]:
    print(line)
if len(lines) > 200:
    print("... (truncated, %d total lines)" % len(lines))

star_g = re.findall(r"\(\*,\s*(239\.\d+\.\d+\.\d+)\)", out)
s_g_sm = re.findall(r"\(\d+\.\d+\.\d+\.\d+,\s*(239\.\d+\.\d+\.\d+)\)\s*SM", out)
s_g_ssm = re.findall(r"\(\d+\.\d+\.\d+\.\d+,\s*(232\.\d+\.\d+\.\d+)\)\s*SSM", out)

print("\n--- PIM Tree Summary ---")
print("  (*,G) ASM entries: %d" % len(star_g))
print("  (S,G) SM entries:  %d" % len(s_g_sm))
print("  (S,G) SSM entries: %d" % len(s_g_ssm))

print("\n" + "=" * 70)
print("5. MULTICAST ROUTE SUMMARY")
print("=" * 70)
out = run("show multicast route summary", 6)
print(out)

print("=" * 70)
print("6. MULTICAST FORWARDING-TABLE SUMMARY")
print("=" * 70)
out = run("show multicast forwarding-table summary", 6)
print(out)

print("=" * 70)
print("7. PIM STATISTICS")
print("=" * 70)
out = run_nopager("show pim statistics", wait=6, max_pages=5)
lines = out.split("\n")
for line in lines:
    s = line.strip()
    if any(k in s.lower() for k in ["register", "join", "prune", "hello", "bootstrap", "assert"]):
        print("  %s" % s)

print("\n" + "=" * 70)
print("8. IGMP GROUPS (first 50 lines)")
print("=" * 70)
out = run_nopager("show igmp groups", wait=6, max_pages=3)
lines = out.split("\n")
for line in lines[:50]:
    print(line)
if len(lines) > 50:
    print("... (truncated, %d total lines)" % len(lines))

ssh.close()
print("\nDone.")
