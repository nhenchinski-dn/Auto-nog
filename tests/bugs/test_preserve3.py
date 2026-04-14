#!/usr/bin/env python3
import paramiko, time, sys

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
# TEST 6: preserve-preserve action (no TPID args) on double-tagged (QinQ) sub-intf
# ========================================
print("\n=== TEST 6: preserve-preserve on QinQ sub-intf ===", flush=True)
cmd("configure", 2)
cmd("interfaces ge10-0/0/1.500 vlan-tags outer-vlan-id 500 inner-vlan-id 50", 2)
cmd("interfaces ge10-0/0/1.500 l2-service enabled", 2)
cmd("interfaces ge10-0/0/1.500 vlan-manipulation ingress-mapping action preserve-preserve", 2)
result = cmd("commit", 15)
print(f"preserve-preserve commit: {result}", flush=True)
cmd("exit", 2)
out = cmd("show interfaces ge10-0/0/1.500", 6)
for line in out.split("\n"):
    if "manipulation" in line.lower() or "preserve" in line.lower() or "mapping" in line.lower() or "vlan" in line.lower() or "encapsulation" in line.lower():
        print(f"  {line.strip()}", flush=True)

# ========================================
# TEST 7: preserve on untagged sub-interface
# ========================================
print("\n=== TEST 7: preserve on untagged sub-interface ===", flush=True)
cmd("configure", 2)
cmd("interfaces ge10-0/0/2.600 untagged", 2)
cmd("interfaces ge10-0/0/2.600 l2-service enabled", 2)
cmd("interfaces ge10-0/0/2.600 vlan-manipulation ingress-mapping action preserve outer-tpid 0x8100", 2)
result = cmd("commit", 15)
print(f"preserve on untagged commit: {result}", flush=True)
if "ERROR" in result:
    print("  GOOD: Rejected on untagged", flush=True)
else:
    print("  POTENTIAL BUG: Accepted on untagged!", flush=True)
    cmd("exit", 2)
    out = cmd("show interfaces ge10-0/0/2.600", 6)
    for line in out.split("\n"):
        if "manipulation" in line.lower() or "preserve" in line.lower() or "mapping" in line.lower() or "encapsulation" in line.lower():
            print(f"  {line.strip()}", flush=True)
cmd("configure", 2)
cmd("rollback", 2)

# ========================================
# TEST 8: preserve with same TPID as encapsulation (e.g., vlan-id uses 0x8100, preserve to 0x8100)
# ========================================
print("\n=== TEST 8: preserve with same TPID as encapsulation ===", flush=True)
out = cmd("show config interfaces ge10-0/0/0.100", 5)
print(out, flush=True)

# ========================================
# TEST 9: Check show config display set format
# ========================================
print("\n=== TEST 9: show config display set ===", flush=True)
cmd("exit", 2)
out = cmd("show config interfaces ge10-0/0/0.100 display set", 5)
print(out, flush=True)

# ========================================
# TEST 10: Check system alarms for any preserve-related alarms
# ========================================
print("\n=== TEST 10: System alarms ===", flush=True)
out = cmd("show system alarms", 8)
print(out, flush=True)

# ========================================
# TEST 11: Check rollback config shows preserve correctly
# ========================================
print("\n=== TEST 11: Rollback config ===", flush=True)
out = cmd("show rollback ?", 5)
print(out, flush=True)

chan.close()
ssh.close()
