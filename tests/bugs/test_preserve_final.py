#!/usr/bin/env python3
import paramiko, time

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("XGU1F7VC0001AP2", username="dnroot", password="dnroot", timeout=15, allow_agent=False, look_for_keys=False)
print("Connected", flush=True)
chan = ssh.invoke_shell(width=250, height=50)
time.sleep(4)
while chan.recv_ready(): chan.recv(65536)

def cmd(c, w=5):
    chan.send(c + "\n")
    time.sleep(w)
    d = b""
    while chan.recv_ready(): d += chan.recv(65536)
    return d.decode(errors='replace')

cmd("set cli screen-length 0", 2)

# ========================================
# FINAL STATE: Show all configured sub-interfaces
# ========================================
print("\n=== FINAL: show config interfaces ===", flush=True)
out = cmd("show config interfaces", 8)
print(out, flush=True)

# Show each interface operational state
for intf in ["ge10-0/0/0.100", "ge10-0/0/0.200", "ge10-0/0/0.300", "ge10-0/0/1.500", "ge10-0/0/1.700", "ge10-0/0/2.600"]:
    out = cmd(f"show interfaces {intf}", 6)
    print(f"\n=== {intf} ===", flush=True)
    for line in out.split("\n"):
        if any(k in line.lower() for k in ["manipulation", "preserve", "mapping", "vlan", "encap", "l2-service", "untag"]):
            print(f"  {line.strip()}", flush=True)

# ========================================  
# TEST: NETCONF access
# ========================================
print("\n=== NETCONF status ===", flush=True)
out = cmd("show system netconf", 5)
print(out, flush=True)

# ========================================
# TEST: Try deleting ONLY ingress preserve, keeping egress
# ========================================
print("\n=== TEST: Delete only ingress preserve, keep egress ===", flush=True)
cmd("configure", 2)
result = cmd("no interfaces ge10-0/0/0.200 vlan-manipulation ingress-mapping", 3)
print(f"Delete ingress only: {result}", flush=True)
result = cmd("commit", 15)
print(f"Commit: {result}", flush=True)
if "succeeded" in result.lower():
    cmd("exit", 2)
    out = cmd("show interfaces ge10-0/0/0.200", 6)
    for line in out.split("\n"):
        if any(k in line.lower() for k in ["manipulation", "preserve", "mapping"]):
            print(f"  {line.strip()}", flush=True)
else:
    cmd("rollback", 2)
    cmd("exit", 2)

# ========================================
# CLEANUP: remove all test configs
# ========================================
print("\n=== CLEANUP ===", flush=True)
cmd("configure", 2)
for intf in ["ge10-0/0/0.100", "ge10-0/0/0.200", "ge10-0/0/0.300", "ge10-0/0/1.500", "ge10-0/0/1.700", "ge10-0/0/2.600"]:
    cmd(f"no interfaces {intf}", 2)
result = cmd("commit", 15)
print(f"Cleanup commit: {result}", flush=True)
cmd("exit", 2)

# Verify clean state
out = cmd("show config interfaces", 5)
print(f"Post-cleanup config:\n{out}", flush=True)

chan.close()
ssh.close()
print("\nAll tests complete!", flush=True)
