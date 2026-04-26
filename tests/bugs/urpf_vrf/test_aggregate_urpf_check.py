"""Check uRPF drops after aggregate test, then cleanup."""
import paramiko, time, re

HOST = 'WKY1C7VD00008P2'
SPIRENT_IF = 'ge400-0/0/3.100'

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

# Check routes still present
print("=== Routes ===")
print(run('show route 10.40.1.0/24 | no-more', wait=5))
print(run('show route 10.40.0.0/16 | no-more', wait=5))

# Check uRPF drops
print("=== Interface counters ===")
print(run(f'show interfaces {SPIRENT_IF} counters | no-more', wait=8))

# Check uRPF specific
print("=== uRPF stats ===")
print(run(f'show interfaces {SPIRENT_IF} | no-more', wait=8))

# Cleanup routes
print("=== Cleanup ===")
run('configure', wait=3)
run('protocols static address-family ipv4-unicast', wait=3)
run('no route 10.40.1.0/24', wait=3)
run('no route 10.40.0.0/16', wait=3)
print(run('commit', wait=8))
print(run('end', wait=3))

# Verify cleanup
print("=== After cleanup ===")
print(run('show route 10.40.1.0/24 | no-more', wait=5))

# Also clean up the Spirent session
client.close()

from stcrestclient import stchttp
stc = stchttp.StcHttp('il-auto-containers', port=80)
for s in stc.sessions():
    if 'aggr_test' in s:
        try:
            stc.join_session(s)
            stc.end_session(s)
            print(f"Ended Spirent session: {s}")
        except Exception as e:
            print(f"Could not end {s}: {e}")

print("\nDone.")
