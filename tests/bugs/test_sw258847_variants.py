import paramiko, time, re, sys

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

results = []

def test_urpf_on_interface(label, iface, config_cmds, cleanup_cmds, expect_disabled=False):
    """Apply config, check show interfaces, show interfaces detail, cleanup."""
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")

    # Apply config
    print(run("configure", 5))
    for cmd in config_cmds:
        print(f"  >>> {cmd}")
        out = run(cmd, 5)
        print(out)
        if "ERROR" in out:
            print(f"  [CONFIG ERROR] {cmd}")
    print("  >>> commit")
    commit_out = run("commit", 30)
    print(commit_out)
    commit_ok = "Commit succeeded" in commit_out
    if not commit_ok and "no configuration changes" in commit_out:
        commit_ok = True  # no changes needed = OK
    print(run("exit", 3))

    if not commit_ok and "ERROR" in commit_out:
        print(f"  [FAIL] {label}: Commit failed")
        results.append((label, "FAIL", f"Commit failed: {commit_out.strip()[:120]}"))
        # cleanup
        print(run("configure", 5))
        for cmd in cleanup_cmds:
            run(cmd, 5)
        run("commit", 30)
        print(run("exit", 3))
        return

    # Show interfaces
    show_out = run(f"show interfaces {iface} | include uRPF | no-more", 10)
    print(f"--- show interfaces {iface} | include uRPF ---")
    print(show_out)

    # Show interfaces detail
    detail_out = run(f"show interfaces detail {iface} | include uRPF | no-more", 10)
    print(f"--- show interfaces detail {iface} | include uRPF ---")
    print(detail_out)

    # Show config
    config_out = run(f"show config interfaces {iface} urpf | no-more", 10)
    print(f"--- show config interfaces {iface} urpf ---")
    print(config_out)

    # Analyze
    ipv4_line = ""
    ipv6_line = ""
    for line in show_out.split('\n'):
        if 'uRPF IPv4' in line:
            ipv4_line = line.strip()
        if 'uRPF IPv6' in line:
            ipv6_line = line.strip()

    if expect_disabled:
        if 'disabled' in ipv4_line and 'disabled' in ipv6_line:
            results.append((label, "PASS", f"Both disabled as expected. IPv4: {ipv4_line}, IPv6: {ipv6_line}"))
        else:
            results.append((label, "FAIL", f"Expected disabled. IPv4: {ipv4_line}, IPv6: {ipv6_line}"))
    else:
        results.append((label, "INFO", f"IPv4: {ipv4_line} | IPv6: {ipv6_line}"))

    # Cleanup
    print(run("configure", 5))
    for cmd in cleanup_cmds:
        print(f"  cleanup>>> {cmd}")
        print(run(cmd, 5))
    print("  cleanup>>> commit")
    print(run("commit", 30))
    print(run("exit", 3))

print(run("", 2))

# ============================================================
# VARIANT 1: ge sub-interface
# ============================================================
test_urpf_on_interface(
    "V1: ge sub-interface (ge10-0/0/5.100) — global strict",
    "ge10-0/0/5.100",
    [
        "interfaces ge10-0/0/5.100 vlan-id 100",
        "interfaces ge10-0/0/5.100 urpf admin-state enabled",
        "interfaces ge10-0/0/5.100 urpf mode strict",
        "interfaces ge10-0/0/5.100 urpf allow-default disabled",
    ],
    ["no interfaces ge10-0/0/5.100"]
)

# ============================================================
# VARIANT 2: bundle interface
# ============================================================
test_urpf_on_interface(
    "V2: bundle interface (bundle-99) — global strict",
    "bundle-99",
    [
        "interfaces bundle-99 admin-state enabled",
        "interfaces bundle-99 urpf admin-state enabled",
        "interfaces bundle-99 urpf mode strict",
        "interfaces bundle-99 urpf allow-default disabled",
    ],
    ["no interfaces bundle-99"]
)

# ============================================================
# VARIANT 3: bundle sub-interface
# ============================================================
test_urpf_on_interface(
    "V3: bundle sub-interface (bundle-99.100) — global strict",
    "bundle-99.100",
    [
        "interfaces bundle-99 admin-state enabled",
        "interfaces bundle-99.100 vlan-id 100",
        "interfaces bundle-99.100 urpf admin-state enabled",
        "interfaces bundle-99.100 urpf mode strict",
        "interfaces bundle-99.100 urpf allow-default disabled",
    ],
    ["no interfaces bundle-99.100", "no interfaces bundle-99"]
)

# ============================================================
# VARIANT 4: IRB interface
# ============================================================
test_urpf_on_interface(
    "V4: IRB interface (irb-1) — global strict",
    "irb-1",
    [
        "interfaces irb-1 admin-state enabled",
        "interfaces irb-1 urpf admin-state enabled",
        "interfaces irb-1 urpf mode strict",
        "interfaces irb-1 urpf allow-default disabled",
    ],
    ["no interfaces irb-1"]
)

# ============================================================
# CONFIG VARIANT: global-only (no per-AFI)
# ============================================================
test_urpf_on_interface(
    "V5: Config global-only (ge10-0/0/5) — strict, no per-AFI",
    "ge10-0/0/5",
    [
        "interfaces ge10-0/0/5 urpf admin-state enabled",
        "interfaces ge10-0/0/5 urpf mode strict",
        "interfaces ge10-0/0/5 urpf allow-default disabled",
    ],
    ["no interfaces ge10-0/0/5 urpf"]
)

# ============================================================
# CONFIG VARIANT: per-AFI only (no global knobs)
# ============================================================
test_urpf_on_interface(
    "V6: Config per-AFI only (ge10-0/0/5) — ipv4 strict, ipv6 loose, no global",
    "ge10-0/0/5",
    [
        "interfaces ge10-0/0/5 urpf address-family ipv4 admin-state enabled",
        "interfaces ge10-0/0/5 urpf address-family ipv4 mode strict",
        "interfaces ge10-0/0/5 urpf address-family ipv4 allow-default disabled",
        "interfaces ge10-0/0/5 urpf address-family ipv6 admin-state enabled",
        "interfaces ge10-0/0/5 urpf address-family ipv6 mode loose",
        "interfaces ge10-0/0/5 urpf address-family ipv6 allow-default disabled",
    ],
    ["no interfaces ge10-0/0/5 urpf"]
)

# ============================================================
# CONFIG VARIANT: strict→loose mode change
# ============================================================
print(f"\n{'='*60}")
print("  V7: strict→loose mode change (ge10-0/0/5)")
print(f"{'='*60}")
# First set strict
print(run("configure", 5))
for cmd in [
    "interfaces ge10-0/0/5 urpf admin-state enabled",
    "interfaces ge10-0/0/5 urpf mode strict",
]:
    run(cmd, 3)
print("  >>> commit (strict)")
print(run("commit", 30))
print(run("exit", 3))
print("--- show interfaces after strict ---")
show_strict = run("show interfaces ge10-0/0/5 | include uRPF | no-more", 10)
print(show_strict)

# Now change to loose
print(run("configure", 5))
run("interfaces ge10-0/0/5 urpf mode loose", 5)
print("  >>> commit (loose)")
print(run("commit", 30))
print(run("exit", 3))
print("--- show interfaces after loose ---")
show_loose = run("show interfaces ge10-0/0/5 | include uRPF | no-more", 10)
print(show_loose)

if "strict" in show_strict and "loose" in show_loose:
    results.append(("V7: strict→loose", "PASS", "Mode updated from strict to loose correctly"))
elif "strict" in show_strict and "strict" in show_loose:
    results.append(("V7: strict→loose", "FAIL", "Mode still shows strict after changing to loose (stale)"))
else:
    results.append(("V7: strict→loose", "INFO", f"strict: {show_strict.strip()} | loose: {show_loose.strip()}"))

# Cleanup V7
print(run("configure", 5))
run("no interfaces ge10-0/0/5 urpf", 5)
print(run("commit", 30))
print(run("exit", 3))

# ============================================================
# NEGATIVE: L2-service interface
# ============================================================
test_urpf_on_interface(
    "NEG-L2: L2-service interface (ge10-0/0/0) — expect disabled",
    "ge10-0/0/0",
    [],  # no config to apply - just check show output on existing L2 interface
    [],
    expect_disabled=True
)

# ============================================================
# SUMMARY
# ============================================================
print("\n" + "=" * 60)
print("  VARIANT TEST SUMMARY")
print("=" * 60)
for label, status, note in results:
    print(f"  [{status}] {label}: {note}")

chan.close()
ssh.close()
print("\nDone.")
