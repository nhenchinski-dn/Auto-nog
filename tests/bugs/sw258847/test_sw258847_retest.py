import paramiko, time, re

HOST = "XEC1E3VR00008"
USER = "dnroot"
PASS = "dnroot"

def clean(text):
    text = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)
    text = re.sub(r'\r', '', text)
    text = re.sub(r'-- More -- \(Press q to quit\)\s*', '', text)
    return text

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASS, timeout=30, look_for_keys=False, allow_agent=False)
chan = ssh.invoke_shell(width=300, height=5000)
time.sleep(6)
chan.recv(65535)

def run(cmd, wait=8):
    chan.send(cmd + '\n')
    time.sleep(wait)
    out = b''
    while chan.recv_ready():
        out += chan.recv(65535)
    return clean(out.decode(errors='replace'))

print(run("", 2))

# Capture version
print("=== System Version ===")
print(run("show system version | no-more", 10))

# Ensure clean state
print("=== Ensure clean ===")
print(run("configure", 5))
print(run("rollback 0", 5))
print(run("no interfaces ge10-0/0/5 urpf", 5))
print(run("no interfaces ge10-0/0/5.200", 5))
print(run("commit", 30))
print(run("exit", 3))

IF = "ge10-0/0/5"

# ============================================================
# STEP 1: Configure global strict + per-AFI (ipv4=strict, ipv6=loose)
# ============================================================
print("=" * 60)
print("STEP 1: Configure global strict + per-AFI (ipv4=strict, ipv6=loose)")
print("=" * 60)
print(run("configure", 5))
for cmd in [
    f"interfaces {IF} urpf admin-state enabled",
    f"interfaces {IF} urpf mode strict",
    f"interfaces {IF} urpf allow-default disabled",
    f"interfaces {IF} urpf address-family ipv4 admin-state enabled",
    f"interfaces {IF} urpf address-family ipv4 mode strict",
    f"interfaces {IF} urpf address-family ipv4 allow-default disabled",
    f"interfaces {IF} urpf address-family ipv6 admin-state enabled",
    f"interfaces {IF} urpf address-family ipv6 mode loose",
    f"interfaces {IF} urpf address-family ipv6 allow-default disabled",
]:
    run(cmd, 3)
print(">>> commit")
print(run("commit", 30))
print(run("exit", 3))

print("--- show config ---")
print(run(f"show config interfaces {IF} urpf | no-more", 10))

# ============================================================
# STEP 2: show interfaces — per-AFI mode override
# ============================================================
print("=" * 60)
print("STEP 2: show interfaces — expect IPv4=strict, IPv6=loose")
print("=" * 60)
show2 = run(f"show interfaces {IF} | include uRPF | no-more", 10)
print(show2)

# ============================================================
# STEP 3: show interfaces detail — should match step 2
# ============================================================
print("=" * 60)
print("STEP 3: show interfaces detail — expect same as step 2")
print("=" * 60)
show3 = run(f"show interfaces detail {IF} | include uRPF | no-more", 10)
print(show3)

# ============================================================
# STEP 4: Change ipv6 from loose to strict
# ============================================================
print("=" * 60)
print("STEP 4: Change ipv6 loose -> strict")
print("=" * 60)
print(run("configure", 5))
run(f"interfaces {IF} urpf address-family ipv6 mode strict", 5)
print(">>> commit")
print(run("commit", 30))
print(run("exit", 3))
print("--- show interfaces ---")
show4 = run(f"show interfaces {IF} | include uRPF | no-more", 10)
print(show4)
print("--- show interfaces detail ---")
show4d = run(f"show interfaces detail {IF} | include uRPF | no-more", 10)
print(show4d)

# ============================================================
# STEP 5: Enable allow-default on ipv4 per-AFI
# ============================================================
print("=" * 60)
print("STEP 5: Enable allow-default on ipv4 per-AFI")
print("=" * 60)
print(run("configure", 5))
run(f"interfaces {IF} urpf address-family ipv4 allow-default enabled", 5)
print(">>> commit")
print(run("commit", 30))
print(run("exit", 3))
print("--- show config ---")
print(run(f"show config interfaces {IF} urpf | no-more", 10))
print("--- show interfaces ---")
show5 = run(f"show interfaces {IF} | include uRPF | no-more", 10)
print(show5)
print("--- show interfaces detail ---")
show5d = run(f"show interfaces detail {IF} | include uRPF | no-more", 10)
print(show5d)

# ============================================================
# STEP 6: Delete per-AFI, verify fallback to global
# ============================================================
print("=" * 60)
print("STEP 6: Delete per-AFI, verify fallback to global strict")
print("=" * 60)
print(run("configure", 5))
run(f"no interfaces {IF} urpf address-family ipv4", 5)
run(f"no interfaces {IF} urpf address-family ipv6", 5)
print(">>> commit")
print(run("commit", 30))
print(run("exit", 3))
print("--- show config ---")
print(run(f"show config interfaces {IF} urpf | no-more", 10))
print("--- show interfaces ---")
show6 = run(f"show interfaces {IF} | include uRPF | no-more", 10)
print(show6)

# ============================================================
# STEP 7: Delete all uRPF, verify disabled
# ============================================================
print("=" * 60)
print("STEP 7: Delete all uRPF, verify disabled")
print("=" * 60)
print(run("configure", 5))
run(f"no interfaces {IF} urpf", 5)
print(">>> commit")
print(run("commit", 30))
print(run("exit", 3))
print("--- show interfaces ---")
show7 = run(f"show interfaces {IF} | include uRPF | no-more", 10)
print(show7)
print("--- show interfaces detail ---")
show7d = run(f"show interfaces detail {IF} | include uRPF | no-more", 10)
print(show7d)

# ============================================================
# VARIANT: sub-interface per-AFI override
# ============================================================
SUB = "ge10-0/0/5.200"
print("=" * 60)
print(f"VARIANT: Sub-interface {SUB} per-AFI override")
print("=" * 60)
print(run("configure", 5))
for cmd in [
    f"interfaces {SUB} vlan-id 200",
    f"interfaces {SUB} urpf admin-state enabled",
    f"interfaces {SUB} urpf mode strict",
    f"interfaces {SUB} urpf allow-default disabled",
    f"interfaces {SUB} urpf address-family ipv4 admin-state enabled",
    f"interfaces {SUB} urpf address-family ipv4 mode strict",
    f"interfaces {SUB} urpf address-family ipv4 allow-default disabled",
    f"interfaces {SUB} urpf address-family ipv6 admin-state enabled",
    f"interfaces {SUB} urpf address-family ipv6 mode loose",
    f"interfaces {SUB} urpf address-family ipv6 allow-default disabled",
]:
    run(cmd, 3)
print(">>> commit")
print(run("commit", 30))
print(run("exit", 3))
print(f"--- show config {SUB} urpf ---")
print(run(f"show config interfaces {SUB} urpf | no-more", 10))
print(f"--- show interfaces {SUB} | include uRPF ---")
show_sub = run(f"show interfaces {SUB} | include uRPF | no-more", 10)
print(show_sub)

# Enable allow-default on sub-if
print(f"\n--- Enable allow-default on {SUB} ipv4 per-AFI ---")
print(run("configure", 5))
run(f"interfaces {SUB} urpf address-family ipv4 allow-default enabled", 5)
print(">>> commit")
print(run("commit", 30))
print(run("exit", 3))
print(f"--- show interfaces {SUB} | include uRPF ---")
show_sub_ad = run(f"show interfaces {SUB} | include uRPF | no-more", 10)
print(show_sub_ad)

# Cleanup sub-interface
print(run("configure", 5))
run(f"no interfaces {SUB}", 5)
print(run("commit", 30))
print(run("exit", 3))

# ============================================================
# SUMMARY
# ============================================================
print("\n" + "=" * 60)
print("RETEST SUMMARY")
print("=" * 60)

def check_line(output, afi, expected_mode=None, expected_ad=None):
    for line in output.split('\n'):
        if f'uRPF {afi} check' in line:
            line = line.strip()
            mode_ok = expected_mode is None or f"Mode: {expected_mode}" in line
            ad_ok = expected_ad is None or f"Allow-default: {expected_ad}" in line
            return line, mode_ok, ad_ok
    return "(not found)", False, False

tests = [
    ("Step 2: show intf IPv4 (expect strict)", show2, "IPv4", "strict", None),
    ("Step 2: show intf IPv6 (expect loose)", show2, "IPv6", "loose", None),
    ("Step 3: show detail IPv4 (expect strict)", show3, "IPv4", "strict", None),
    ("Step 3: show detail IPv6 (expect loose)", show3, "IPv6", "loose", None),
    ("Step 4: ipv6 changed to strict", show4, "IPv6", "strict", None),
    ("Step 5: ipv4 allow-default (expect enabled)", show5, "IPv4", None, "enabled"),
    ("Step 5: ipv6 allow-default (expect disabled)", show5, "IPv6", None, "disabled"),
    ("Step 6: fallback global IPv4 (strict)", show6, "IPv4", "strict", None),
    ("Step 6: fallback global IPv6 (strict)", show6, "IPv6", "strict", None),
    ("Step 7: deleted IPv4 (disabled)", show7, "IPv4", None, None),
    ("Step 7: deleted IPv6 (disabled)", show7, "IPv6", None, None),
    ("Sub-if: IPv6 per-AFI (expect loose)", show_sub, "IPv6", "loose", None),
    ("Sub-if: IPv4 allow-default (expect enabled)", show_sub_ad, "IPv4", None, "enabled"),
]

for label, output, afi, exp_mode, exp_ad in tests:
    line, mode_ok, ad_ok = check_line(output, afi, exp_mode, exp_ad)
    if "disabled" in line and exp_mode is None and exp_ad is None:
        status = "PASS"
    elif mode_ok and ad_ok:
        status = "PASS"
    else:
        status = "FAIL"
    print(f"  [{status}] {label}")
    print(f"         {line}")

chan.close()
ssh.close()
print("\nDone.")
