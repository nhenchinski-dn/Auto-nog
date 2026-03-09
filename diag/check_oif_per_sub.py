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

def run_full(cmd, max_wait=300):
    """Run command, pressing space through all pages"""
    chan.send(cmd + "\n")
    time.sleep(3)
    out = ""
    last_data_time = time.time()
    while (time.time() - last_data_time) < 15:
        if chan.recv_ready():
            chunk = chan.recv(65535).decode("utf-8", errors="replace")
            out += chunk
            last_data_time = time.time()
            if "More" in chunk:
                chan.send(" ")
                time.sleep(0.05)
        else:
            time.sleep(0.3)
        if (time.time() - last_data_time) > max_wait:
            break
    return out

# Collect full multicast route table
print("Collecting full multicast route table (this may take a while)...")
mroute = run_full("show multicast route", 300)

# Parse (S,G) entries and their OIFs
entries = {}
current_sg = None
in_outgoing = False

for line in mroute.split("\n"):
    line_clean = re.sub(r'\x1b\[[^m]*m', '', line).strip()
    if not line_clean or "More" in line_clean:
        continue

    sg_match = re.match(r'\((\d+\.\d+\.\d+\.\d+),\s*(\d+\.\d+\.\d+\.\d+)/32\)', line_clean)
    if sg_match:
        current_sg = (sg_match.group(1), sg_match.group(2))
        entries[current_sg] = {"oifs": [], "flags": ""}
        in_outgoing = False
        # Check for F flag
        if "Flags:" in line_clean:
            entries[current_sg]["flags"] = line_clean.split("Flags:")[-1].strip()
        continue

    if current_sg:
        if "Flags:" in line_clean and not entries[current_sg]["flags"]:
            entries[current_sg]["flags"] = line_clean.split("Flags:")[-1].strip()
        if "Outgoing Interfaces:" in line_clean:
            in_outgoing = True
            continue
        if in_outgoing:
            iface_match = re.match(r'(ge\S+|bundle\S+|lo\S+)', line_clean)
            if iface_match:
                entries[current_sg]["oifs"].append(iface_match.group(1).rstrip(","))
            elif "Incoming" in line_clean or "Counters:" in line_clean or "Forwarded" in line_clean:
                in_outgoing = False

print("Parsed %d (S,G) entries\n" % len(entries))

# Count OIFs per entry
oif_counts = defaultdict(int)
for sg, data in entries.items():
    oif_counts[len(data["oifs"])] += 1

print("=" * 60)
print("OIF count distribution:")
print("=" * 60)
for count in sorted(oif_counts.keys()):
    print("  %2d OIFs: %d entries" % (count, oif_counts[count]))

total_oifs = sum(len(d["oifs"]) for d in entries.values())
print("\nTotal OIFs across all entries: %d" % total_oifs)
print("Expected (11000 x 20): %d" % (11000 * 20))
print("Missing: %d" % (11000 * 20 - total_oifs))

# Count per-interface OIF presence
iface_oif_count = defaultdict(int)
iface_missing = defaultdict(int)
for sg, data in entries.items():
    for oif in data["oifs"]:
        iface_oif_count[oif] += 1

print("\n" + "=" * 60)
print("OIF presence per interface (out of %d entries):" % len(entries))
print("=" * 60)
for iface in sorted(iface_oif_count.keys(), key=lambda x: (x.split(".")[0], int(x.split(".")[-1]) if "." in x else 0)):
    count = iface_oif_count[iface]
    missing = len(entries) - count
    pct = 100.0 * count / len(entries) if entries else 0
    status = "OK" if missing == 0 else "MISSING %d" % missing
    print("  %-20s: %6d / %d  (%5.1f%%)  %s" % (iface, count, len(entries), pct, status))

# Show entries with F (Failed) flag
failed = [(sg, d) for sg, d in entries.items() if "F" in d["flags"]]
if failed:
    print("\n" + "=" * 60)
    print("Entries with FAILED flag: %d" % len(failed))
    print("=" * 60)
    for sg, d in failed[:5]:
        print("  (%s, %s) OIFs: %d, Flags: %s" % (sg[0], sg[1], len(d["oifs"]), d["flags"]))

# Show some entries with < 20 OIFs
short_entries = [(sg, d) for sg, d in entries.items() if len(d["oifs"]) < 20]
if short_entries:
    print("\n" + "=" * 60)
    print("Sample entries with < 20 OIFs (first 10):")
    print("=" * 60)
    for sg, d in short_entries[:10]:
        present = set(d["oifs"])
        all_subs = set("ge800-0/0/10.%d" % i for i in range(1, 21))
        missing_ifaces = sorted(all_subs - present, key=lambda x: int(x.split(".")[-1]))
        print("  (%s, %s) — %d OIFs, missing: %s" % (sg[0], sg[1], len(d["oifs"]), ", ".join(missing_ifaces)))

ssh.close()
