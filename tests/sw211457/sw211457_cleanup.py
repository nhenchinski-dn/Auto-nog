#!/usr/bin/env python3
import paramiko, time

DEVICE_IP = "100.64.3.239"
USER = "dnroot"
PASS = "dnroot"

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(DEVICE_IP, username=USER, password=PASS, look_for_keys=False, allow_agent=False, timeout=30)
shell = client.invoke_shell(width=200, height=50)
shell.settimeout(120)
time.sleep(3)

def send(cmd, wait=2):
    shell.send(cmd + "\n")
    time.sleep(wait)
    out = ""
    while shell.recv_ready():
        out += shell.recv(65536).decode('utf-8', errors='replace')
        time.sleep(0.3)
    print(f">>> {cmd}")
    print(out[-200:] if len(out) > 200 else out)
    return out

send("configure", 2)

for i in range(100, 120):
    send(f"no interfaces irb{i}", 0.5)
    send(f"no network-services evpn instance evpn{i}", 0.5)

send("no interfaces irb66", 0.5)
send("no network-services evpn instance kfkfkf", 0.5)
send("no network-services vrf instance testvrf", 0.5)
send("no network-services vrf instance alpha", 0.5)
send("top", 1)
out = send("show config compare | no-more", 5)
if "Deleted" in out or "Changed" in out:
    send("commit", 30)
    print("Cleanup committed")
else:
    send("rollback 0", 2)
    print("Nothing to clean")

send("exit", 1)
send("exit", 1)
shell.close()
client.close()
print("Done")
