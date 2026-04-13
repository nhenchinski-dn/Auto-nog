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
# TEST 6d: Correct QinQ syntax
# ========================================
print("\n=== TEST 6d: QinQ with correct syntax ===", flush=True)
cmd("configure", 2)

# vlan-tags outer-tag takes a number directly
out = cmd("interfaces ge10-0/0/1.500 vlan-tags outer-tag 500 ?", 3)
print(f"outer-tag 500 ?: {out}", flush=True)

# Try: vlan-tags outer-tag 500 inner-tag 50
out = cmd("interfaces ge10-0/0/1.500 vlan-tags outer-tag 500 inner-tag 50", 3)
print(f"QinQ config result: {out}", flush=True)
cmd("interfaces ge10-0/0/1.500 l2-service enabled", 2)

# preserve on QinQ (single outer TPID rewrite)
cmd("interfaces ge10-0/0/1.500 vlan-manipulation ingress-mapping action preserve outer-tpid 0x8100", 2)
cmd("interfaces ge10-0/0/1.500 vlan-manipulation egress-mapping action preserve outer-tpid 0x88a8", 2)
result = cmd("commit", 15)
print(f"preserve on QinQ commit: {result}", flush=True)

if "ERROR" not in result and "succeeded" in result.lower():
    cmd("exit", 2)
    out = cmd("show interfaces ge10-0/0/1.500", 8)
    for line in out.split("\n"):
        if any(k in line.lower() for k in ["manipulation", "preserve", "mapping", "vlan", "encap"]):
            print(f"  {line.strip()}", flush=True)
    cmd("configure", 2)
else:
    cmd("rollback", 2)

# ========================================
# TEST 15: preserve-preserve on QinQ
# ========================================
print("\n=== TEST 15: preserve-preserve on QinQ ===", flush=True)
cmd("interfaces ge10-0/0/1.700 vlan-tags outer-tag 700 inner-tag 70", 3)
cmd("interfaces ge10-0/0/1.700 l2-service enabled", 2)
cmd("interfaces ge10-0/0/1.700 vlan-manipulation ingress-mapping action preserve-preserve", 2)
result = cmd("commit", 15)
print(f"preserve-preserve on QinQ: {result}", flush=True)
if "succeeded" in result.lower():
    cmd("exit", 2)
    out = cmd("show interfaces ge10-0/0/1.700", 8)
    for line in out.split("\n"):
        if any(k in line.lower() for k in ["manipulation", "preserve", "mapping", "vlan", "encap"]):
            print(f"  {line.strip()}", flush=True)
    cmd("configure", 2)
else:
    cmd("rollback", 2)

# ========================================
# TEST 16: Check npu-resources for preserve entries
# ========================================
print("\n=== TEST 16: NPU resources ===", flush=True)
cmd("exit", 2)
out = cmd("show system npu-resources ?", 5)
print(out[:1000], flush=True)

# ========================================
# TEST 17: Stress test - rapid TPID value changes
# ========================================
print("\n=== TEST 17: Rapid TPID changes (10 cycles) ===", flush=True)
tpid_values = ["0x8100", "0x88a8", "0x9100", "0x9200"]
cmd("configure", 2)
for i in range(10):
    tpid = tpid_values[i % 4]
    cmd(f"interfaces ge10-0/0/0.100 vlan-manipulation ingress-mapping action preserve outer-tpid {tpid}", 1)
    result = cmd("commit", 8)
    if "ERROR" in result:
        print(f"  Cycle {i}: FAILED commit with {tpid}: {result.strip()}", flush=True)
        break
    else:
        pass  # Success

# Verify final state
cmd("exit", 2)
out = cmd("show interfaces ge10-0/0/0.100", 6)
final_tpid = tpid_values[9 % 4]
print(f"After 10 cycles, expected TPID: {final_tpid}", flush=True)
for line in out.split("\n"):
    if any(k in line.lower() for k in ["ingress mapping", "preserve"]):
        print(f"  Actual: {line.strip()}", flush=True)

# Check alarms after stress
out = cmd("show system alarms", 5)
alarm_count = "0 alarms" if "0 alarms" in out else "ALARMS PRESENT"
print(f"System alarms: {alarm_count}", flush=True)

chan.close()
ssh.close()
print("\nDone!", flush=True)
