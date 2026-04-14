import paramiko, time, re

HOST = "XEC1E3VR00008"
USER = "dnroot"
PASS = "dnroot"
IF = "ge10-0/0/5"

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

# Re-apply global + per-AFI for Step 6
print("SETUP: Re-apply config (global strict + per-AFI ipv4=strict, ipv6=loose)")
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
print("commit:")
print(run("commit", 30))
print(run("exit", 3))

print("--- verify config applied ---")
print(run(f"show config interfaces {IF} urpf | no-more", 10))

# Step 6: Delete per-AFI entries one by one
print("=" * 60)
print("STEP 6: Delete per-AFI config")
print("=" * 60)
print(run("configure", 5))
print("  >>> no interfaces {IF} urpf address-family ipv4")
print(run(f"no interfaces {IF} urpf address-family ipv4", 5))
print("  >>> no interfaces {IF} urpf address-family ipv6")
print(run(f"no interfaces {IF} urpf address-family ipv6", 5))
print("  >>> commit")
print(run("commit", 30))
print(run("exit", 3))

print("--- show config urpf (should only have global) ---")
print(run(f"show config interfaces {IF} urpf | no-more", 10))
print("--- show interfaces uRPF lines (should fallback to global strict) ---")
print(run(f"show interfaces {IF} | include uRPF | no-more", 10))

# Final cleanup
print("CLEANUP: no urpf")
print(run("configure", 5))
print(run(f"no interfaces {IF} urpf", 5))
print(run("commit", 30))
print(run("exit", 3))
print("--- verify clean ---")
print(run(f"show interfaces {IF} | include uRPF | no-more", 10))

chan.close()
ssh.close()
print("\nDone.")
