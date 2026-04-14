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

# First clean up any pending changes
cmd("configure", 2)
cmd("rollback", 2)

# ========================================
# TEST 6b: preserve-preserve on QinQ (correct vlan-tags syntax)
# ========================================
print("\n=== TEST 6b: preserve-preserve on QinQ ===", flush=True)
# Check correct QinQ syntax
out = cmd("interfaces ge10-0/0/1.500 vlan-tags ?", 3)
print(f"vlan-tags ?: {out}", flush=True)

out = cmd("interfaces ge10-0/0/1.500 vlan-tags outer-vlan-id 500 inner-vlan-id 50", 3)
print(f"vlan-tags config: {out}", flush=True)

cmd("interfaces ge10-0/0/1.500 l2-service enabled", 2)
cmd("interfaces ge10-0/0/1.500 vlan-manipulation ingress-mapping action preserve-preserve", 2)

result = cmd("commit", 15)
print(f"preserve-preserve commit: {result}", flush=True)
if "ERROR" in result:
    print("  Commit rejected", flush=True)
    cmd("rollback", 2)
else:
    print("  Commit succeeded!", flush=True)
    cmd("exit", 2)
    out = cmd("show interfaces ge10-0/0/1.500", 6)
    for line in out.split("\n"):
        if any(k in line.lower() for k in ["manipulation", "preserve", "mapping", "vlan", "encap"]):
            print(f"  {line.strip()}", flush=True)
    cmd("configure", 2)

# ========================================
# TEST 7b: preserve on untagged sub-interface (proper test)
# ========================================
print("\n=== TEST 7b: preserve on untagged sub-interface ===", flush=True)
cmd("interfaces ge10-0/0/2.600 untagged", 2)
cmd("interfaces ge10-0/0/2.600 l2-service enabled", 2)
cmd("interfaces ge10-0/0/2.600 vlan-manipulation ingress-mapping action preserve outer-tpid 0x8100", 2)
result = cmd("commit", 15)
print(f"preserve on untagged commit: {result}", flush=True)
if "ERROR" in result:
    print("  GOOD: Rejected on untagged", flush=True)
    cmd("rollback", 2)
else:
    print("  ** FINDING: Accepted on untagged! **", flush=True)
    cmd("exit", 2)
    out = cmd("show interfaces ge10-0/0/2.600", 6)
    for line in out.split("\n"):
        if any(k in line.lower() for k in ["manipulation", "preserve", "mapping", "vlan", "encap", "untag"]):
            print(f"  {line.strip()}", flush=True)
    cmd("configure", 2)

# ========================================  
# TEST 9b: display set format
# ========================================
print("\n=== TEST 9b: show config display set ===", flush=True)
cmd("exit", 2)
out = cmd("show config interfaces ge10-0/0/0.100 display set", 6)
print(out, flush=True)

# ========================================
# TEST 10b: System alarms
# ========================================
print("\n=== TEST 10b: System alarms ===", flush=True)
out = cmd("show system alarms", 8)
print(out[:2000], flush=True)

# ========================================
# TEST 12: Check dnos-internal for preserve/vlan-manipulation
# ========================================
print("\n=== TEST 12: dnos-internal ? ===", flush=True)
out = cmd("show dnos-internal ?", 5)
print(out[:2000], flush=True)

chan.close()
ssh.close()
