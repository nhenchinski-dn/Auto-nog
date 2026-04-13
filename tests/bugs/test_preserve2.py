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
# TEST 1: Action transition - change TPID value on committed config
# ========================================
print("\n=== TEST 1: Change TPID value (0x8100 -> 0x88a8) on committed intf ===", flush=True)
cmd("configure", 2)
cmd("interfaces ge10-0/0/0.100 vlan-manipulation ingress-mapping action preserve outer-tpid 0x88a8", 2)
result = cmd("commit", 15)
print(result, flush=True)
cmd("exit", 2)
out = cmd("show interfaces ge10-0/0/0.100", 6)
for line in out.split("\n"):
    if "manipulation" in line.lower() or "preserve" in line.lower() or "mapping" in line.lower():
        print(f"  {line.strip()}", flush=True)

# ========================================
# TEST 2: Change from preserve to swap action
# ========================================
print("\n=== TEST 2: Change from preserve to swap action ===", flush=True)
cmd("configure", 2)
cmd("interfaces ge10-0/0/0.200 vlan-manipulation ingress-mapping action swap outer-vlan-id 500", 2)
result = cmd("commit", 15)
print(f"Commit result: {result}", flush=True)
cmd("exit", 2)
out = cmd("show interfaces ge10-0/0/0.200", 6)
for line in out.split("\n"):
    if "manipulation" in line.lower() or "preserve" in line.lower() or "mapping" in line.lower() or "swap" in line.lower():
        print(f"  {line.strip()}", flush=True)

# ========================================
# TEST 3: Rollback after TPID change
# ========================================
print("\n=== TEST 3: Rollback 1 ===", flush=True)
cmd("configure", 2)
result = cmd("rollback 1", 15)
print(f"Rollback 1 result: {result}", flush=True)
# Check if rollback requires commit
result = cmd("commit", 15)
print(f"Commit after rollback: {result}", flush=True)
cmd("exit", 2)

# Verify .100 went back to 0x8100 and .200 went back to preserve
out = cmd("show interfaces ge10-0/0/0.100", 6)
print("\n.100 after rollback:", flush=True)
for line in out.split("\n"):
    if "manipulation" in line.lower() or "preserve" in line.lower() or "mapping" in line.lower():
        print(f"  {line.strip()}", flush=True)

out = cmd("show interfaces ge10-0/0/0.200", 6)
print("\n.200 after rollback:", flush=True)
for line in out.split("\n"):
    if "manipulation" in line.lower() or "preserve" in line.lower() or "mapping" in line.lower() or "swap" in line.lower():
        print(f"  {line.strip()}", flush=True)

# ========================================
# TEST 4: Delete preserve config (no vlan-manipulation)
# ========================================
print("\n=== TEST 4: Delete preserve config ===", flush=True)
cmd("configure", 2)
cmd("no interfaces ge10-0/0/0.400 vlan-manipulation", 2)
result = cmd("commit", 15)
print(f"Delete commit: {result}", flush=True)
cmd("exit", 2)
out = cmd("show interfaces ge10-0/0/0.400", 6)
print(".400 after delete:", flush=True)
for line in out.split("\n"):
    if "manipulation" in line.lower() or "preserve" in line.lower() or "mapping" in line.lower():
        print(f"  {line.strip()}", flush=True)
print("(should be empty)", flush=True)

# ========================================
# TEST 5: Check system logs for errors
# ========================================
print("\n=== TEST 5: System logs ===", flush=True)
out = cmd("show system logging last 30", 10)
print(out, flush=True)

chan.close()
ssh.close()
