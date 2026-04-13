#!/usr/bin/env python3
"""Debug: test configuration changes via paramiko with prompt detection"""

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
    initial = chan.recv(65535).decode(errors='replace')
    print(f"Initial prompt:\n{repr(initial[-200:])}")
    return ssh, chan

def send_and_read(chan, cmd, wait=5):
    print(f"\n>>> Sending: {repr(cmd)}")
    chan.send(cmd + '\r')
    time.sleep(wait)
    out = b''
    while chan.recv_ready():
        out += chan.recv(65535)
    decoded = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', out.decode(errors='replace'))
    print(f"<<< Response ({len(decoded)} chars):\n{decoded}")
    return decoded

def main():
    ssh, chan = connect()

    # Step 1: Check current mode
    out = send_and_read(chan, '', 2)

    # Step 2: Enter configure mode
    out = send_and_read(chan, 'configure', 5)

    # Step 3: Check we're in config mode
    out = send_and_read(chan, '', 2)

    # Step 4: Try to delete DM_CLI_TAB_mep3
    out = send_and_read(chan, 'delete services performance-monitoring cfm two-way-delay-measurement DM_CLI_TAB_mep3', 5)

    # Step 5: Show config compare
    out = send_and_read(chan, 'show config compare', 10)

    # Step 6: Commit
    out = send_and_read(chan, 'commit', 15)

    # Step 7: Verify
    out = send_and_read(chan, 'show services performance-monitoring cfm tests proactive | no-more', 10)

    # Step 8: Try deleting DM_CLI_TAB_mep1
    out = send_and_read(chan, 'delete services performance-monitoring cfm two-way-delay-measurement DM_CLI_TAB_mep1', 5)

    # Step 9: Show config compare
    out = send_and_read(chan, 'show config compare', 10)

    # Step 10: Commit
    out = send_and_read(chan, 'commit', 15)

    # Step 11: Verify
    out = send_and_read(chan, 'show services performance-monitoring cfm tests proactive | no-more', 10)

    # Step 12: Re-add DM_CLI_TAB_mep1
    send_and_read(chan, 'services performance-monitoring cfm two-way-delay-measurement DM_CLI_TAB_mep1 admin-state enabled', 3)
    send_and_read(chan, 'services performance-monitoring cfm two-way-delay-measurement DM_CLI_TAB_mep1 description cli_tab_test', 3)
    send_and_read(chan, 'services performance-monitoring cfm two-way-delay-measurement DM_CLI_TAB_mep1 profile DM_PROF_CLI', 3)
    send_and_read(chan, 'services performance-monitoring cfm two-way-delay-measurement DM_CLI_TAB_mep1 source maintenance-domain MD-CUST maintenance-association MA-CUST mep-id 1', 3)
    send_and_read(chan, 'services performance-monitoring cfm two-way-delay-measurement DM_CLI_TAB_mep1 target mep-id 2', 3)

    # Step 13: Commit
    out = send_and_read(chan, 'commit', 15)

    # Step 14: End and verify
    send_and_read(chan, 'end', 3)

    time.sleep(10)
    out = send_and_read(chan, 'show services performance-monitoring cfm tests proactive | no-more', 10)

    # On-demand test
    time.sleep(3)
    out = send_and_read(chan,
        'run ethernet-oam cfm on-demand delay-measurement two-way maintenance-domain MD-CUST1 maintenance-association MA-CUST1 target mac-address 84:40:76:90:cd:15',
        25)

    time.sleep(3)
    out = send_and_read(chan,
        'run ethernet-oam cfm on-demand delay-measurement two-way maintenance-domain MD-CUST1 maintenance-association MA-CUST1 target mep-id 4',
        25)

    ssh.close()
    print("\nDone.")

if __name__ == '__main__':
    main()
