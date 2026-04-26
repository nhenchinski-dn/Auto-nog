#!/usr/bin/env python3
"""Enable LLDP on all physical ge400 interfaces of wky1c7vd00008p2."""
import paramiko
import time
import re

HOST = "wky1c7vd00008p2"
USER = "dnroot"
PASS = "dnroot"

PHYSICAL_INTERFACES = [
    "ge400-0/0/0",
    "ge400-0/0/2",
    "ge400-0/0/3",
    "ge400-0/0/4",
    "ge400-0/0/5",
    "ge400-0/0/6",
    "ge400-0/0/7",
    "ge400-0/0/8",
    "ge400-0/0/9",
    "ge400-0/0/10",
    "ge400-0/0/11",
    "ge400-0/0/12",
    "ge400-0/0/13",
    "ge400-0/0/14",
    "ge400-0/0/15",
    "ge400-0/0/16",
    "ge400-0/0/18",
    "ge400-0/0/20",
    "ge400-0/0/21",
    "ge400-0/0/22",
    "ge400-0/0/23",
    "ge400-0/0/24",
    "ge400-0/0/25",
    "ge400-0/0/26",
    "ge400-0/0/27",
    "ge400-0/0/28",
    "ge400-0/0/29",
    "ge400-0/0/30",
    "ge400-0/0/31",
    "ge400-0/0/32",
    "ge400-0/0/33",
    "ge400-0/0/34",
]


def clean(text):
    text = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text)
    text = re.sub(r"\r", "", text)
    text = re.sub(r"-- More -- \(Press q to quit\)\s*", "", text)
    return text


def recv_all(shell, timeout=5):
    out = ""
    end = time.time() + timeout
    while time.time() < end:
        time.sleep(0.4)
        while shell.recv_ready():
            out += shell.recv(65536).decode("utf-8", errors="replace")
            end = time.time() + 1.5
    return out


def send(shell, cmd, wait=1.5):
    shell.send(cmd + "\n")
    time.sleep(wait)
    return recv_all(shell, timeout=3)


print(f"=== Connecting to {HOST} ===", flush=True)
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(HOST, username=USER, password=PASS,
               look_for_keys=False, allow_agent=False, timeout=15)
shell = client.invoke_shell(width=250, height=5000)
time.sleep(6)
banner = recv_all(shell, timeout=3)
print(clean(banner).strip()[-120:], flush=True)

send(shell, "set cli-no-confirm", wait=1)

print("\n=== Entering config mode ===", flush=True)
out = send(shell, "configure", wait=2)
print(clean(out).strip()[-200:], flush=True)

print("\n=== Applying LLDP config ===", flush=True)
send(shell, "top", wait=1)
out = send(shell, "protocols lldp", wait=1)
out = send(shell, "admin-state enabled", wait=1)

for intf in PHYSICAL_INTERFACES:
    send(shell, f"interface {intf}", wait=0.6)
    send(shell, "exit", wait=0.4)

print(f"  Declared {len(PHYSICAL_INTERFACES)} interfaces under protocols lldp", flush=True)

print("\n=== Commit ===", flush=True)
out = send(shell, "top", wait=1)
shell.send("commit\n")
commit_out = ""
out_of_sync_handled = False
start = time.time()
while time.time() - start < 60:
    time.sleep(1)
    while shell.recv_ready():
        commit_out += shell.recv(65536).decode("utf-8", errors="replace")
    cleaned = clean(commit_out)
    if (not out_of_sync_handled) and "out of sync" in cleaned.lower():
        shell.send("commit\n")
        out_of_sync_handled = True
        print("  -> Answered 'commit' to out-of-sync prompt", flush=True)
        time.sleep(2)
        continue
    if re.search(r"Commit succeeded|Aborted|aborted|error", cleaned, re.IGNORECASE):
        break
    if re.search(r"\(cfg\)#\s*$", cleaned):
        break

print(clean(commit_out).strip()[-1200:], flush=True)

low = clean(commit_out).lower()
if "abort" in low or "fail" in low or ("error" in low and "succeeded" not in low):
    print("\n!!! COMMIT FAILED — rolling back !!!", flush=True)
    out = send(shell, "rollback", wait=3)
    print(clean(out).strip()[-400:], flush=True)
    client.close()
    raise SystemExit(1)

print("\n=== Exit config ===", flush=True)
send(shell, "end", wait=1)

print("\n=== Verification: show config protocols lldp ===", flush=True)
out = send(shell, "show config protocols lldp | no-more", wait=3)
print(clean(out), flush=True)

print("\n=== Verification: show lldp interfaces (brief) ===", flush=True)
out = send(shell, "show lldp interfaces | no-more", wait=4)
print(clean(out), flush=True)

send(shell, "exit", wait=1)
client.close()
print("\n=== DONE ===", flush=True)
