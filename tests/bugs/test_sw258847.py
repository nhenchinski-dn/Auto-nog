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

def show_urpf():
    print(run(f"show interfaces {IF} | include uRPF | no-more", 10))

def show_config_urpf():
    print(run(f"show config interfaces {IF} urpf | no-more", 10))

def config_cmds(cmds):
    print(run("configure", 5))
    for cmd in cmds:
        print(f"  >>> {cmd}")
        print(run(cmd, 5))
    print("  >>> commit")
    print(run("commit", 30))
    print(run("exit", 3))

print(run("", 2))

# Re-apply global + per-AFI config for Step 6 test
print("=" * 60)
print("SETUP: Re-apply global strict + per-AFI (ipv4=strict, ipv6=loose)")
print("=" * 60)
config_cmds([
    f"interfaces {IF} urpf admin-state enabled",
    f"interfaces {IF} urpf mode strict",
    f"interfaces {IF} urpf allow-default disabled",
    f"interfaces {IF} urpf address-family ipv4 admin-state enabled",
    f"interfaces {IF} urpf address-family ipv4 mode strict",
    f"interfaces {IF} urpf address-family ipv4 allow-default disabled",
    f"interfaces {IF} urpf address-family ipv6 admin-state enabled",
    f"interfaces {IF} urpf address-family ipv6 mode loose",
    f"interfaces {IF} urpf address-family ipv6 allow-default disabled",
])
print("--- show config urpf ---")
show_config_urpf()
print("--- show interfaces (uRPF lines) ---")
show_urpf()

# Step 6: Delete per-AFI config properly
print("=" * 60)
print("STEP 6: Delete per-AFI config (both ipv4 and ipv6)")
print("=" * 60)
config_cmds([
    f"no interfaces {IF} urpf address-family ipv4",
    f"no interfaces {IF} urpf address-family ipv6",
])
print("--- show config urpf ---")
show_config_urpf()
print("--- show interfaces (uRPF lines) ---")
show_urpf()

# Cleanup: delete all uRPF
print("=" * 60)
print("CLEANUP: Delete all uRPF")
print("=" * 60)
config_cmds([
    f"no interfaces {IF} urpf",
])
print("--- show interfaces (uRPF lines) ---")
show_urpf()

chan.close()
ssh.close()
print("\nDone.")
