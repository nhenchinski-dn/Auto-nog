#!/usr/bin/env python3
"""Further probe: confirm ge100-0/0/3/0 exists and is usable; check Spirent 1/25."""
import paramiko
import time
import re

HOST = "WKY1C7VD00008P2"

ANSI_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')


def clean(t):
    return re.sub(r'-- More -- \(Press q to quit\)\s*', '',
                  re.sub(r'\r', '', ANSI_RE.sub('', t)))


def recv_all(shell, timeout=6):
    out = b""
    end = time.time() + timeout
    while time.time() < end:
        time.sleep(0.3)
        while shell.recv_ready():
            out += shell.recv(65536)
            end = time.time() + 1.2
    return clean(out.decode(errors='replace'))


def run(shell, cmd, wait=3):
    shell.send(cmd + "\n")
    time.sleep(wait)
    return recv_all(shell, timeout=6)


def main():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username='dnroot', password='dnroot',
                look_for_keys=False, allow_agent=False, timeout=20)
    shell = ssh.invoke_shell(width=250, height=5000)
    time.sleep(6)
    shell.recv(65535)

    cmds = [
        "show interfaces ge100-0/0/3/0 | no-more",
        "show interfaces ge100-0/0/3/1 | no-more",
        "show config interfaces ge100-0/0/3/0 | no-more",
        "show interfaces | inc ge100-0/0/3 | no-more",
        "show lldp neighbors | no-more",
        "show system cp-utilization | no-more",
    ]
    for c in cmds:
        print(f"\n############ {c} ############", flush=True)
        print(run(shell, c, wait=3), flush=True)

    ssh.close()


if __name__ == "__main__":
    main()
