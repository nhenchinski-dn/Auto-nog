#!/usr/bin/env python3
"""Run DNOS CLI commands on a remote device via SSH interactive shell."""
import sys
import time
import paramiko
import re

def run_commands(host, commands, user='dnroot', password='dnroot', timeout=120):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, username=user, password=password, look_for_keys=False, allow_agent=False, timeout=15)

    shell = client.invoke_shell(width=250, height=5000)
    time.sleep(6)

    output = ""
    while shell.recv_ready():
        output += shell.recv(65536).decode('utf-8', errors='replace')

    for cmd in commands:
        shell.send(cmd + "\n")
        time.sleep(5)
        chunk = ""
        retries = 0
        while True:
            if shell.recv_ready():
                chunk += shell.recv(65536).decode('utf-8', errors='replace')
                retries = 0
            else:
                retries += 1
                if retries > 4:
                    break
                time.sleep(1)
        output += chunk

    shell.send("exit\n")
    time.sleep(1)
    while shell.recv_ready():
        output += shell.recv(65536).decode('utf-8', errors='replace')

    client.close()

    output = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', output)
    output = re.sub(r'\r', '', output)
    output = re.sub(r'-- More -- \(Press q to quit\)\s*', '', output)
    print(output)

if __name__ == '__main__':
    host = sys.argv[1]
    commands = sys.argv[2:]
    run_commands(host, commands)
