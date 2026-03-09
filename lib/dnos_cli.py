#!/usr/bin/env python3
"""DNOS CLI helper for sending commands via SSH with pager handling."""
import pexpect
import sys
import re

HOST = "WKY1C7VD00008P2"
USER = "dnroot"
PASS = "dnroot"
PROMPT_RE = r'[\w\-]+[#>]\s*$'
MORE_RE = r'-- More --'
TIMEOUT = 30

def connect():
    cmd = (
        f"sshpass -p '{PASS}' ssh -tt "
        f"-o StrictHostKeyChecking=no "
        f"-o PreferredAuthentications=password,keyboard-interactive "
        f"-o PubkeyAuthentication=no "
        f"{USER}@{HOST}"
    )
    child = pexpect.spawn(cmd, encoding='utf-8', timeout=TIMEOUT,
                          maxread=200000)
    child.expect(PROMPT_RE)
    # Try to disable pager
    child.sendline("set cli screen-length 0")
    try:
        child.expect(PROMPT_RE, timeout=5)
    except:
        pass
    return child

def clean(text):
    text = re.sub(r'\x1b\[[0-9;]*[mKHJrl]', '', text)
    text = re.sub(r'\x1b\[\?[0-9;]*[hl]', '', text)
    text = re.sub(r'\r\s+\r', '\n', text)
    text = re.sub(r'\r\n', '\n', text)
    text = re.sub(r'\r', '', text)
    return text.strip()

def send_cmd(child, cmd, timeout=TIMEOUT):
    child.sendline(cmd)
    output = ""
    while True:
        idx = child.expect([PROMPT_RE, MORE_RE, pexpect.TIMEOUT],
                          timeout=timeout)
        output += child.before
        if idx == 0:   # Got prompt
            break
        elif idx == 1: # Got pager, press space
            child.send(" ")
        else:          # Timeout
            break
    return clean(output)

def disconnect(child):
    try:
        child.sendline("exit")
        child.expect(pexpect.EOF, timeout=5)
    except:
        child.close()

if __name__ == "__main__":
    cmds = sys.argv[1:] if len(sys.argv) > 1 else [
        "show services ethernet-oam connectivity-fault-management summary"
    ]
    child = connect()
    for cmd in cmds:
        print(f"\n{'='*60}")
        print(f"CMD: {cmd}")
        print(f"{'='*60}")
        out = send_cmd(child, cmd)
        print(out)
    disconnect(child)
