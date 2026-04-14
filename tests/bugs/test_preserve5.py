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
# TEST 7c: Deep inspection of preserve on untagged
# ========================================
print("\n=== TEST 7c: Full details of untagged with preserve ===", flush=True)
out = cmd("show interfaces ge10-0/0/2.600 detail", 8)
print(out, flush=True)

# Check show config for the untagged interface
out = cmd("show config interfaces ge10-0/0/2.600", 5)
print("Config of untagged with preserve:", flush=True)
print(out, flush=True)

# ========================================
# TEST 6c: Find correct QinQ vlan-tags syntax
# ========================================
print("\n=== TEST 6c: QinQ vlan-tags correct syntax ===", flush=True)
cmd("configure", 2)
out = cmd("interfaces ge10-0/0/1.500 vlan-tags outer-tag ?", 3)
print(f"outer-tag ?: {out}", flush=True)

out = cmd("interfaces ge10-0/0/1.500 vlan-tags outer-tag vlan-id 500 ?", 3)
print(f"outer-tag vlan-id 500 ?: {out}", flush=True)

# Try the correct syntax path
cmd("interfaces ge10-0/0/1.500 vlan-tags outer-tag vlan-id 500 inner-tag vlan-id 50", 3)
cmd("interfaces ge10-0/0/1.500 l2-service enabled", 2)
cmd("interfaces ge10-0/0/1.500 vlan-manipulation ingress-mapping action preserve-preserve", 2)
result = cmd("commit", 15)
print(f"\npreserve-preserve on QinQ commit: {result}", flush=True)
if "ERROR" not in result:
    cmd("exit", 2)
    out = cmd("show interfaces ge10-0/0/1.500", 8)
    for line in out.split("\n"):
        if any(k in line.lower() for k in ["manipulation", "preserve", "mapping", "vlan", "encap"]):
            print(f"  {line.strip()}", flush=True)
    cmd("configure", 2)
else:
    cmd("rollback", 2)

# ========================================
# TEST 13: preserve-swap on QinQ
# ========================================
print("\n=== TEST 13: preserve-swap on QinQ ===", flush=True)
cmd("interfaces ge10-0/0/1.600 vlan-tags outer-tag vlan-id 600 inner-tag vlan-id 60", 3)
cmd("interfaces ge10-0/0/1.600 l2-service enabled", 2)
out = cmd("interfaces ge10-0/0/1.600 vlan-manipulation ingress-mapping action preserve-swap ?", 3)
print(f"preserve-swap ?: {out}", flush=True)
out = cmd("interfaces ge10-0/0/1.600 vlan-manipulation ingress-mapping action preserve-swap outer-tpid ?", 3)
print(f"preserve-swap outer-tpid ?: {out}", flush=True)
# Check if there are more params needed after outer-tpid
cmd("interfaces ge10-0/0/1.600 vlan-manipulation ingress-mapping action preserve-swap outer-tpid 0x8100", 2)
result = cmd("commit", 15)
print(f"\npreserve-swap commit: {result}", flush=True)
if "ERROR" not in result:
    cmd("exit", 2)
    out = cmd("show interfaces ge10-0/0/1.600", 6)
    for line in out.split("\n"):
        if any(k in line.lower() for k in ["manipulation", "preserve", "swap", "mapping", "vlan", "encap"]):
            print(f"  {line.strip()}", flush=True)
    cmd("configure", 2)
else:
    cmd("rollback", 2)

# ========================================
# TEST 14: Check operational DB for preserve entries
# ========================================
print("\n=== TEST 14: dnos-internal oper-data for interfaces ===", flush=True)
cmd("exit", 2)
out = cmd("show dnos-internal oper-data ?", 5)
print(out[:1500], flush=True)

chan.close()
ssh.close()
