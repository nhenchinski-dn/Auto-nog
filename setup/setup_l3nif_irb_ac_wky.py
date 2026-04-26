#!/usr/bin/env python3
"""
Configure Spirent -> L3 NIF -> IRB -> AC -> Spirent topology on WKY1C7VD00008P2.

Topology:
  Spirent(A, 10.10.10.10/24, untagged) <---> ge100-0/0/3/0 (L3 NIF, 10.10.10.1/24)
                                              |
                                              | routed via IRB100 (20.20.20.1/24)
                                              |
                                              bridge-domain bd100 (router-interface irb100)
                                              |
                                              | AC = ge100-0/0/3/1.100 (l2-service, vlan 100)
                                              |
  Spirent(B, 20.20.20.10/24, vlan 100) <---> ge100-0/0/3/1
"""
import paramiko
import time
import re

HOST = "wky1c7vd00008p2"
USER = "dnroot"
PASS = "dnroot"

L3_NIF_PORT = "ge100-0/0/3/0"
L3_NIF_IP = "10.10.10.1/24"

AC_PARENT_PORT = "ge100-0/0/3/1"
AC_SUBINTF = "ge100-0/0/3/1.100"
AC_VLAN = 100

IRB_ID = 100
IRB_NAME = f"irb{IRB_ID}"
IRB_IP = "20.20.20.1/24"

BD_NAME = "bd100"


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


def send(shell, cmd, wait=1.2):
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

cfg_cmds = [
    "top",

    # L3 NIF
    "interfaces",
    f"{L3_NIF_PORT}",
    "admin-state enabled",
    f"ipv4-address {L3_NIF_IP}",
    "top",

    # IRB
    "interfaces",
    f"{IRB_NAME}",
    "admin-state enabled",
    f"ipv4-address {IRB_IP}",
    "top",

    # AC parent + sub-interface
    "interfaces",
    f"{AC_PARENT_PORT}",
    "admin-state enabled",
    "top",
    "interfaces",
    f"{AC_SUBINTF}",
    "admin-state enabled",
    "l2-service enabled",
    f"vlan-id {AC_VLAN}",
    "top",

    # Bridge-domain
    "network-services",
    "bridge-domain",
    f"instance {BD_NAME}",
    "admin-state enabled",
    f"router-interface {IRB_NAME}",
    f"interface {AC_SUBINTF}",
    "top",
]

print("\n=== Sending config ===", flush=True)
for c in cfg_cmds:
    out = send(shell, c, wait=0.6)
    tail = clean(out).strip().splitlines()[-1:] if clean(out).strip() else []
    print(f"  > {c}   | {tail}", flush=True)

print("\n=== Commit ===", flush=True)
send(shell, "top", wait=1)
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

print(clean(commit_out).strip()[-2000:], flush=True)

low = clean(commit_out).lower()
if ("abort" in low or ("error" in low and "succeeded" not in low)):
    print("\n!!! COMMIT FAILED — rolling back !!!", flush=True)
    out = send(shell, "rollback", wait=3)
    print(clean(out).strip()[-400:], flush=True)
    client.close()
    raise SystemExit(1)

print("\n=== Exit config ===", flush=True)
send(shell, "end", wait=1)

print("\n############ VERIFICATION ############\n", flush=True)
for cmd in [
    f"show config interfaces {L3_NIF_PORT} | no-more",
    f"show config interfaces {IRB_NAME} | no-more",
    f"show config interfaces {AC_SUBINTF} | no-more",
    "show config network-services bridge-domain | no-more",
    f"show interfaces detail {L3_NIF_PORT} | no-more",
    f"show interfaces detail {IRB_NAME} | no-more",
    f"show interfaces detail {AC_SUBINTF} | no-more",
    "show network-services bridge-domain summary | no-more",
    "show bridge-domain summary | no-more",
]:
    print(f"\n######## {cmd} ########", flush=True)
    out = send(shell, cmd, wait=3)
    print(clean(out), flush=True)

send(shell, "exit", wait=1)
client.close()
print("\n=== DONE ===", flush=True)
