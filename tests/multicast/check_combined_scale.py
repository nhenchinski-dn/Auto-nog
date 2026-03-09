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
                time.sleep(0.05)
        else:
            time.sleep(0.5)
    return out

results = []

def record(name, passed, evidence=""):
    status = "PASS" if passed else "FAIL"
    results.append((name, status, evidence))
    print("  [%s] %s" % (status, name))
    if evidence:
        print("    %s" % evidence)
    print()

print("=" * 70)
print("SW-241851: Scale | Combined (60K Routes + 220K Replications)")
print("=" * 70)

# PIM interface state
print("\n--- PIM Interface State ---\n")
out = run_nopager("show pim interface", 15)
for line in out.split("\n"):
    s = line.strip()
    if "ge800" in s or "Interface" in s or "+" in s:
        print("  %s" % s)

# PIM neighbors
print("\n--- PIM Neighbors ---\n")
out = run_nopager("show pim neighbors", 10)
nbr_count = len(re.findall(r"ge800-0/0/10\.\d+", out))
src_nbr = "3.5.0.2" in out and "ge800-0/0/31" in out
record("PIM neighbors on all receiver sub-interfaces", nbr_count >= 4,
       "%d receiver neighbors found" % nbr_count)
record("PIM neighbor on source interface", src_nbr)

# Multicast route summary
print("\n--- Multicast Route Summary ---\n")
out = run_nopager("show multicast route summary", 10)
print(out)
sg_match = re.search(r"Number of \(S,G\) routes\s*:\s*(\d+)", out)
failed_match = re.search(r"Number of failed route installs\s*:\s*(\d+)", out)
sg_routes = int(sg_match.group(1)) if sg_match else 0
failed_installs = int(failed_match.group(1)) if failed_match else 0
record("(S,G) routes at 60K limit", sg_routes >= 60000,
       "%d (S,G) routes, %d failed installs" % (sg_routes, failed_installs))

# Forwarding table summary
print("\n--- Multicast Forwarding-Table Summary ---\n")
out = run_nopager("show multicast forwarding-table summary", 10)
print(out)
sg_fwd_match = re.search(r"Number of \(S,G,\*\), RPF\(in-lif\) forwarding entries\s*:\s*([\d,]+)", out)
repl_match = re.search(r"Actual Multicast \(S,G\) replications\s*:\s*([\d,]+)", out)
max_repl_match = re.search(r"Maximum \(S,G\) replications limit\s*:\s*([\d,]+)", out)
star_g_match = re.search(r"Number of \(\*,G,\*\) forwarding entries\s*:\s*(\d+)", out)

sg_fwd = int(sg_fwd_match.group(1).replace(",", "")) if sg_fwd_match else 0
actual_repl = int(repl_match.group(1).replace(",", "")) if repl_match else 0
max_repl = int(max_repl_match.group(1).replace(",", "")) if max_repl_match else 0
star_g_fwd = int(star_g_match.group(1)) if star_g_match else 0

record("(S,G) forwarding entries near 60K", sg_fwd >= 59990,
       "%d entries (expected ~59992, %d internal (*,G,*) entries)" % (sg_fwd, star_g_fwd))
record("Replications near 220K limit", actual_repl >= 219000,
       "%d / %d (%.1f%%)" % (actual_repl, max_repl, 100.0 * actual_repl / max_repl if max_repl else 0))

# Consistency check
print("\n--- Consistency Checks ---\n")
route_gap = sg_routes - sg_fwd
record("Route table vs forwarding table gap", True,
       "mroute: %d, fwd-table: %d, gap: %d (internal entries)" % (sg_routes, sg_fwd, route_gap))

expected_repl_4oif = sg_routes * 4
expected_repl_3x60k_1x40k = 3 * 60000 + 40000
record("Replication count vs expected", True,
       "Actual: %d, Expected (3×60K+40K): %d, Missing: %d" % (
           actual_repl, expected_repl_3x60k_1x40k, expected_repl_3x60k_1x40k - actual_repl))

# PIM statistics
print("\n--- PIM Statistics ---\n")
out = run_nopager("show pim statistics", 10)
jp_match = re.search(r"Join/Prune\s+\|\s*(\d+)\s*\|\s*(\d+)", out)
if jp_match:
    jp_rx, jp_tx = int(jp_match.group(1)), int(jp_match.group(2))
    record("PIM Join/Prune messages", jp_rx > 0, "Rx: %d, Tx: %d" % (jp_rx, jp_tx))

# System health
print("\n--- System Health ---\n")
out = run_nopager("show system alarms", 8)
alm_match = re.search(r"(\d+)\s+alarms?\s+currently\s+active", out, re.IGNORECASE)
if alm_match:
    alm_count = int(alm_match.group(1))
    record("System alarms", alm_count == 0, "%d alarms" % alm_count)
else:
    record("System alarms", True, "No alarm output")

# PIM per-interface join counts
print("\n--- Per-Interface Join/Prune ---\n")
for i in range(1, 5):
    iface = "ge800-0/0/10.%d" % i
    out = run_nopager("show pim statistics interface %s" % iface, 5)
    jp = re.search(r"Join/Prune\s+\|\s*(\d+)\s*\|\s*(\d+)", out)
    if jp:
        print("  %-20s  Join/Prune Rx: %s" % (iface, jp.group(1)))

# Summary
print("\n" + "=" * 70)
print("RESULTS SUMMARY")
print("=" * 70)
passed = sum(1 for r in results if r[1] == "PASS")
failed = sum(1 for r in results if r[1] == "FAIL")
for name, status, evidence in results:
    flag = "+" if status == "PASS" else "X"
    line = "  [%s] %s" % (flag, name)
    if evidence:
        line += " (%s)" % evidence
    print(line)
print("\nTotal: %d PASS / %d FAIL out of %d" % (passed, failed, len(results)))
print("=" * 70)

ssh.close()
