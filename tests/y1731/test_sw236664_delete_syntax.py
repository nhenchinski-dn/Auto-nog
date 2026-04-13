#!/usr/bin/env python3
"""Test various delete/remove syntaxes on DNOS"""

import paramiko
import time
import re

DEVICE_IP = "100.64.3.184"
USERNAME = "dnroot"
PASSWORD = "dnroot"

def connect():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(DEVICE_IP, username=USERNAME, password=PASSWORD,
                timeout=30, look_for_keys=False, allow_agent=False)
    chan = ssh.invoke_shell(width=400)
    time.sleep(5)
    chan.recv(65535)
    return ssh, chan

def clean_ansi(text):
    return re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)

def send(chan, cmd, wait=5):
    print(f"\n>>> {cmd}")
    chan.send(cmd + '\r')
    time.sleep(wait)
    out = b''
    while chan.recv_ready():
        out += chan.recv(65535)
    decoded = clean_ansi(out.decode(errors='replace'))
    print(f"<<< {decoded.strip()}")
    return decoded

def main():
    ssh, chan = connect()

    send(chan, 'configure', 5)

    # Try different delete syntaxes
    print("\n=== Testing delete syntaxes ===")

    # 1. 'no' prefix
    send(chan, 'no services performance-monitoring cfm two-way-delay-measurement DM_CLI_TAB_mep3', 5)

    # 2. 'remove' keyword
    send(chan, 'remove services performance-monitoring cfm two-way-delay-measurement DM_CLI_TAB_mep3', 5)

    # 3. Check if '?' help shows available commands
    send(chan, '?', 5)

    # 4. Try entering the hierarchy and then deleting
    send(chan, 'services performance-monitoring cfm', 3)
    send(chan, '?', 5)
    send(chan, 'exit', 3)

    # 5. Try 'rollback' to see if it works
    send(chan, 'rollback ?', 5)

    # 6. Try 'load override'
    send(chan, 'load ?', 5)

    send(chan, 'end', 3)
    ssh.close()
    print("\nDone.")

if __name__ == '__main__':
    main()
