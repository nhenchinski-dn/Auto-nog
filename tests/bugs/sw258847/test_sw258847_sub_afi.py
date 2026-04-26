import paramiko, time, re

HOST = "XEC1E3VR00008"
USER = "dnroot"
PASS = "dnroot"
IF = "ge10-0/0/5.200"

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

# First: clean up any stale state
print("=== Cleanup stale config ===")
print(run("configure", 5))
print(run("rollback 0", 5))
print(run("no interfaces ge10-0/0/5 urpf", 5))
print(run("no interfaces ge10-0/0/5.200", 5))
print(run("commit", 30))
print(run("exit", 3))

# Verify clean
print("=== Verify ge10-0/0/5 is clean ===")
print(run("show config interfaces ge10-0/0/5 urpf | no-more", 10))

# Now create sub-interface with global strict + per-AFI (ipv4=strict, ipv6=loose)
print("=" * 60)
print("TEST: Per-AFI override on ge sub-interface")
print("=" * 60)
print(run("configure", 5))
for cmd in [
    f"interfaces {IF} vlan-id 200",
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

print(f"\n--- show config interfaces {IF} urpf ---")
print(run(f"show config interfaces {IF} urpf | no-more", 10))

print(f"--- show interfaces {IF} | include uRPF ---")
print(run(f"show interfaces {IF} | include uRPF | no-more", 10))

print(f"--- show interfaces detail {IF} | include uRPF ---")
print(run(f"show interfaces detail {IF} | include uRPF | no-more", 10))

# Test per-AFI allow-default override
print("\n=== Now enable allow-default on ipv4 per-AFI ===")
print(run("configure", 5))
run(f"interfaces {IF} urpf address-family ipv4 allow-default enabled", 5)
print(">>> commit")
print(run("commit", 30))
print(run("exit", 3))

print(f"--- show config interfaces {IF} urpf ---")
print(run(f"show config interfaces {IF} urpf | no-more", 10))

print(f"--- show interfaces {IF} | include uRPF ---")
print(run(f"show interfaces {IF} | include uRPF | no-more", 10))

# Cleanup
print("\n=== Final cleanup ===")
print(run("configure", 5))
run(f"no interfaces {IF}", 5)
print(run("commit", 30))
print(run("exit", 3))

chan.close()
ssh.close()
print("\nDone.")
