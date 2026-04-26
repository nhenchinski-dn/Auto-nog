#!/usr/bin/env python3
"""Probe WKY1C7VD00008P2 current state before re-running SW-244107.

Checks:
  - software version
  - current ACL config (egress-bfd)
  - current interfaces bundle-10/20 config
  - current BGP neighbors
  - current BFD sessions
  - current SR policy config
"""
import paramiko
import time
import re
import sys

HOST = "WKY1C7VD00008P2"
USER = "dnroot"
PASS = "dnroot"

ANSI_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')
CR_RE = re.compile(r'\r')
MORE_RE = re.compile(r'-- More -- \(Press q to quit\)\s*')


def clean(text):
    text = ANSI_RE.sub('', text)
    text = CR_RE.sub('', text)
    text = MORE_RE.sub('', text)
    return text


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
    print(f"=== Connecting to {HOST} ===", flush=True)
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASS,
                look_for_keys=False, allow_agent=False, timeout=20)
    shell = ssh.invoke_shell(width=250, height=5000)
    time.sleep(6)
    shell.recv(65535)

    cmds = [
        "show system | no-more",
        "show config access-lists | no-more",
        "show config interfaces bundle-10 | no-more",
        "show config interfaces bundle-20 | no-more",
        "show config interfaces ge400-0/0/3 | no-more",
        "show config interfaces ge400-0/0/11 | no-more",
        "show config interfaces ge400-0/0/12 | no-more",
        "show config protocols bgp | no-more",
        "show config protocols bfd | no-more",
        "show config protocols segment-routing | no-more",
        "show bgp neighbors brief | no-more",
        "show bfd sessions | no-more",
        "show access-lists counters bundle-10 | no-more",
    ]
    for c in cmds:
        print(f"\n############ {c} ############", flush=True)
        print(run(shell, c, wait=3), flush=True)

    ssh.close()


if __name__ == "__main__":
    main()
