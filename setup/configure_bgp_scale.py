#!/usr/bin/env python3
"""Configure BGP peering on WKY1C7VD00008P2 for Spirent scale test.

DUT: ge400-0/0/3 = 10.0.0.1/16  (connected to Spirent port 3)
Spirent: 10.0.0.2, AS 65002
DUT: AS 65001, router-id 10.0.0.1
"""
import sys
import time
import re
import paramiko

HOST = "WKY1C7VD00008P2"
DUT_AS = 65001
DUT_RID = "10.0.0.1"
SPIRENT_IP = "10.0.0.2"
SPIRENT_AS = 65002


def connect(host):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, username='dnroot', password='dnroot',
                   look_for_keys=False, allow_agent=False, timeout=15)
    chan = client.invoke_shell(width=250, height=5000)
    time.sleep(6)
    while chan.recv_ready():
        chan.recv(65536)
    return client, chan


def send_cmd(chan, cmd, wait=5):
    chan.send(cmd + "\n")
    time.sleep(wait)
    out = ""
    retries = 0
    while True:
        if chan.recv_ready():
            out += chan.recv(65536).decode('utf-8', errors='replace')
            retries = 0
        else:
            retries += 1
            if retries > 4:
                break
            time.sleep(1)
    out = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', out)
    out = re.sub(r'\r', '', out)
    out = re.sub(r'-- More -- \(Press q to quit\)\s*', '', out)
    return out


def main():
    print(f"Connecting to {HOST}...")
    client, chan = connect(HOST)

    print("Entering config mode...")
    send_cmd(chan, "configure", wait=3)

    bgp_cmds = [
        f"protocols bgp {DUT_AS} router-id {DUT_RID}",
        f"protocols bgp {DUT_AS} neighbor {SPIRENT_IP} remote-as {SPIRENT_AS}",
        f"protocols bgp {DUT_AS} neighbor {SPIRENT_IP} admin-state enabled",
        f"protocols bgp {DUT_AS} neighbor {SPIRENT_IP} address-family ipv4-unicast",
    ]

    for cmd in bgp_cmds:
        send_cmd(chan, "top", wait=2)
        print(f"  > {cmd}")
        out = send_cmd(chan, cmd, wait=3)
        if "ERROR" in out:
            print(f"    ERROR: {out.strip()}")
        else:
            print(f"    OK")

    send_cmd(chan, "top", wait=2)
    print("\nCommitting configuration...")
    out = send_cmd(chan, "commit", wait=20)
    print(out)

    if "NOTICE: commit action is not applicable" in out:
        print("WARNING: Commit reported no changes")
    elif "ERROR" in out:
        print(f"ERROR during commit")
    else:
        print("Commit successful!")

    send_cmd(chan, "top", wait=2)
    send_cmd(chan, "exit", wait=2)

    print("\n=== Verifying BGP config ===")
    out = send_cmd(chan, f"show config protocols bgp {DUT_AS} | no-more", wait=5)
    print(out)

    print("\n=== BGP neighbor status ===")
    out = send_cmd(chan, "show bgp neighbor | no-more", wait=5)
    print(out)

    print("\n=== uRPF status ===")
    for intf in ["ge400-0/0/3", "ge400-0/0/3.1", "ge400-0/0/33"]:
        out = send_cmd(chan, f"show interfaces detail {intf} | include uRPF", wait=3)
        for line in out.split('\n'):
            if 'uRPF' in line:
                print(f"  {intf}: {line.strip()}")

    print("\n=== Route summary ===")
    out = send_cmd(chan, "show route summary | no-more", wait=5)
    print(out)

    send_cmd(chan, "exit", wait=2)
    client.close()
    print("\nDone! BGP peering configured. Waiting for Spirent to connect.")


if __name__ == "__main__":
    main()
