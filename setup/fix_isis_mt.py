"""
Disable IS-IS multi-topology for IPv6 on the DUT so it accepts TLV 236 routes from Spirent.
"""
import paramiko, time, re

HOST = 'WKY1C7VD00008P2'

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(HOST, username='dnroot', password='dnroot',
               look_for_keys=False, allow_agent=False, timeout=15)
shell = client.invoke_shell(width=250, height=5000)
time.sleep(6)
shell.recv(65535)

def run(cmd, wait=5):
    shell.send(cmd + '\n')
    time.sleep(wait)
    out = b''
    while shell.recv_ready():
        out += shell.recv(65535)
        time.sleep(0.3)
    txt = out.decode('utf-8', errors='replace')
    txt = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', txt)
    txt = re.sub(r'\r', '', txt)
    return txt

# Show current IS-IS summary (before)
print("=== BEFORE: IS-IS summary (topologies section) ===")
out = run('show isis | no-more', wait=8)
for line in out.split('\n'):
    if any(kw in line.lower() for kw in ['topolog', 'ipv6', 'instance']):
        print(f"  {line.strip()}")

# Show current IPv6 IS-IS routes
print("\n=== BEFORE: IPv6 IS-IS routes ===")
print(run('show route table ipv6-unicast protocol isis | no-more', wait=8))

# Disable multi-topology for IPv6
print("=== Disabling IPv6 multi-topology ===")
print(run('configure', wait=3))
print(run('protocols isis', wait=3))
print(run('instance urpf', wait=3))
print(run('address-family ipv6-unicast', wait=3))
print(run('topology disabled', wait=3))
print(run('commit', wait=8))
print(run('end', wait=3))

# Wait for IS-IS to reconverge
print("\nWaiting 15s for IS-IS reconvergence...")
time.sleep(15)

# Verify
print("=== AFTER: IS-IS summary (topologies section) ===")
out = run('show isis | no-more', wait=8)
for line in out.split('\n'):
    if any(kw in line.lower() for kw in ['topolog', 'ipv6', 'instance']):
        print(f"  {line.strip()}")

print("\n=== AFTER: IS-IS IPv6 route table ===")
print(run('show isis route table ipv6-unicast | no-more', wait=8))

print("\n=== AFTER: IPv6 IS-IS routes in RIB ===")
print(run('show route table ipv6-unicast protocol isis | no-more', wait=8))

client.close()
print("\nDone.")
