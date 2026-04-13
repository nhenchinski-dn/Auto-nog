#!/usr/bin/env python3
"""Check remote device (NCP3-CFM-nog) QoS ingress counters delta
while proactive SLM with PCP 3 runs on the local device."""

import paramiko, time, re, sys

REMOTE_IP = None  # will discover from local device

def connect(ip):
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(ip, username='dnroot', password='dnroot',
                timeout=30, look_for_keys=False, allow_agent=False)
    chan = ssh.invoke_shell(width=300)
    time.sleep(5)
    chan.recv(65535)
    return ssh, chan

def run(chan, cmd, wait=10):
    chan.send(cmd + '\n')
    time.sleep(wait)
    out = b''
    while chan.recv_ready():
        out += chan.recv(65535)
    text = out.decode(errors='replace')
    return re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)

def ensure_op(chan):
    run(chan, '', 2)
    check = run(chan, '', 2)
    if '(cfg' in check:
        run(chan, 'top', 2)
        r = run(chan, 'end', 3)
        if 'uncommitted' in r.lower():
            run(chan, 'no', 5)

def parse_counters(text, iface_filter='ge400-0/0/33.100'):
    counters = {}
    for line in text.splitlines():
        if iface_filter in line and '|' in line:
            parts = [p.strip() for p in line.split('|')]
            if len(parts) >= 11:
                iface = parts[1]
                direction = parts[2]
                rule = parts[4]
                tc = parts[5]
                try:
                    match_pkts = int(parts[9])
                    key = f"{direction}|{rule}|{tc}"
                    counters[key] = match_pkts
                except:
                    pass
    return counters

# First get the remote device's management IP from the local device
print("Connecting to local device to find remote IP...")
ssh_local, chan_local = connect("100.64.4.93")
ensure_op(chan_local)
out = run(chan_local, 'show lldp neighbors interface ge100-0/0/70 | no-more', 10)
print("LLDP neighbor on ge100-0/0/70:")
for line in out.splitlines():
    if 'management' in line.lower() or 'address' in line.lower() or 'ip' in line.lower() or 'NCP3' in line:
        print(f"  {line.strip()}")

# Try to get the management IP from LLDP detail
out2 = run(chan_local, 'show lldp neighbors interface ge100-0/0/70 detail | no-more', 10)
mgmt_ip = None
for line in out2.splitlines():
    if 'management' in line.lower() and re.search(r'\d+\.\d+\.\d+\.\d+', line):
        m = re.search(r'(\d+\.\d+\.\d+\.\d+)', line)
        if m:
            mgmt_ip = m.group(1)
            break
    if 'chassis-id' in line.lower() or 'system-name' in line.lower():
        print(f"  {line.strip()}")

if not mgmt_ip:
    # Try getting it from the device management interfaces
    print("\nLLDP detail output (looking for IP):")
    for line in out2.splitlines():
        line_s = line.strip()
        if line_s and not line_s.startswith('show') and not line_s.startswith('ncpl'):
            print(f"  {line_s}")

ssh_local.close()

# Now try NCP3-CFM-nog. Let's try the management IP or guess from naming.
# The device list showed NCP3-CFM-nog_priv_26.2.0.58
# Try common management IPs or use the same approach
if not mgmt_ip:
    print("\nCould not find mgmt IP from LLDP. Trying to SSH directly...")
    # From device list, NCP3-CFM-nog might share the cluster
    # Let's try to find it via the management interfaces
    ssh_local2, chan_local2 = connect("100.64.4.93")
    ensure_op(chan_local2)
    # Check if there are management interfaces that might help
    out3 = run(chan_local2, 'show system ncc | no-more', 10)
    print("\nSystem NCC info:")
    for line in out3.splitlines():
        if re.search(r'\d+\.\d+\.\d+\.\d+', line):
            print(f"  {line.strip()}")
    ssh_local2.close()

# Try connecting to NCP3-CFM-nog via its known name
# The network mapper has it. Let me try common management network pattern.
# ncpl-cfm-nog is at 100.64.4.93 (NCP-Light). NCP3-CFM-nog might be 100.64.4.94 or similar.
print("\n\nAttempting to find NCP3-CFM-nog management IP...")
# Let's try connecting via the network mapper output. The device might share the same
# base box. Let's try using a script that SSHes to the local and checks the bridge.
ssh3, chan3 = connect("100.64.4.93")
ensure_op(chan3)
out4 = run(chan3, 'show system management-interface | no-more', 10)
print("\nManagement interfaces:")
for line in out4.splitlines():
    if re.search(r'\d+\.\d+\.\d+\.\d+', line):
        print(f"  {line.strip()}")

# Also check the peer NCC
out5 = run(chan3, 'show system ncc-detail | no-more', 10)
for line in out5.splitlines():
    if re.search(r'\d+\.\d+\.\d+\.\d+', line) or 'ncc' in line.lower():
        print(f"  {line.strip()}")

ssh3.close()
print("\nDone. Use the remote device IP to check QoS.")
