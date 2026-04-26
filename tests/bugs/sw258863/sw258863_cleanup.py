#!/usr/bin/env python3
"""SW-258863 cleanup recovery — handle out-of-sync prompt, finish cleanup, verify."""
import paramiko, time, re, os

HOST = "WKY1C7VD00008P2"
LOG_DIR = "/tmp/sw258863"
ANSI = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')

def clean(s):
    s = ANSI.sub('', s)
    s = s.replace('\r', '')
    s = re.sub(r'-- More -- \(Press q to quit\)\s*', '', s)
    return s

def connect():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, username='dnroot', password='dnroot',
              look_for_keys=False, allow_agent=False, timeout=20)
    sh = c.invoke_shell(width=300, height=10000)
    time.sleep(7)
    return c, sh

def run(sh, cmd, settle=4):
    sh.send(cmd + "\n")
    time.sleep(settle)
    out = ""
    retries = 0
    while True:
        if sh.recv_ready():
            out += sh.recv(65536).decode('utf-8', errors='replace')
            retries = 0
        else:
            retries += 1
            if retries > 5:
                break
            time.sleep(0.6)
    return clean(out)

def step(sh, label, cmds, settle=4):
    chunks = []
    for c in cmds:
        chunks.append(f"\n>>> {c}\n")
        chunks.append(run(sh, c, settle=settle))
    body = "".join(chunks)
    path = os.path.join(LOG_DIR, label + ".log")
    with open(path, 'w') as f:
        f.write(body)
    print(body)

def main():
    c, sh = connect()
    try:
        # Drain banner
        run(sh, "", settle=2)
        # Re-attempt cleanup with full attention. Fresh session avoids stale prompt.
        step(sh, "11_cleanup_retry", [
            "configure",
            "no interfaces ge100-0/0/3/0 urpf",
            "no interfaces ge100-0/0/3/1 urpf",
            "no interfaces ge100-0/0/3/2.103",
            "interfaces ge100-0/0/3/2 admin-state disabled",
            "commit",
            "end",
        ], settle=5)
        # If commit was already applied earlier, the above commit will be a no-op or
        # report nothing-to-commit. Either way, verify final state.
        step(sh, "12_final_verify", [
            "show interfaces breakout | no-more",
            "show config interfaces | no-more",
            "show interfaces ge100-0/0/3/0 | no-more",
            "show interfaces ge100-0/0/3/1 | no-more",
            "show interfaces ge100-0/0/3/2 | no-more",
            "show interfaces ge100-0/0/3/2.103 | no-more",
        ], settle=6)
        run(sh, "exit", settle=1)
    finally:
        c.close()

if __name__ == '__main__':
    main()
