#!/usr/bin/env python3
"""SW-258863 — uRPF on breakout interfaces. Runs all steps in one SSH session and
logs each step's CLI output to /tmp/sw258863/<step>.log."""
import paramiko, time, re, os, sys

HOST = "WKY1C7VD00008P2"
LOG_DIR = "/tmp/sw258863"
os.makedirs(LOG_DIR, exist_ok=True)

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
    drain(sh)
    return c, sh

def drain(sh, max_wait=2):
    out = ""
    end = time.time() + max_wait
    while time.time() < end:
        if sh.recv_ready():
            out += sh.recv(65536).decode('utf-8', errors='replace')
            end = time.time() + 0.8
        else:
            time.sleep(0.15)
    return out

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
    print(f"\n========== {label} ==========")
    chunks = []
    for c in cmds:
        chunks.append(f"\n>>> {c}\n")
        chunks.append(run(sh, c, settle=settle))
    body = "".join(chunks)
    path = os.path.join(LOG_DIR, label.replace(' ', '_').replace('/', '_').replace(':', '') + ".log")
    with open(path, 'w') as f:
        f.write(body)
    print(body)
    return body

def main():
    c, sh = connect()
    try:
        # Step 0/1: baseline + verify pre-existing breakout
        step(sh, "00_baseline", [
            "show system version | no-more",
            "show interfaces breakout | no-more",
            "show interfaces ge100-0/0/3/0 | no-more",
            "show interfaces ge100-0/0/3/1 | no-more",
            "show interfaces ge100-0/0/3/2 | no-more",
            "show interfaces ge100-0/0/3/3 | no-more",
        ])

        # Step 2: global uRPF strict on /0
        step(sh, "02_step2_global_urpf_apply", [
            "configure",
            "interfaces ge100-0/0/3/0 urpf admin-state enabled",
            "interfaces ge100-0/0/3/0 urpf mode strict",
            "interfaces ge100-0/0/3/0 urpf allow-default disabled",
            "commit",
            "end",
        ])
        step(sh, "02_step2_verify", [
            "show config interfaces | no-more",
            "show interfaces ge100-0/0/3/0 | no-more",
            "show interfaces detail ge100-0/0/3/0 | no-more",
        ], settle=6)

        # Step 3: per-AFI knobs ipv4 strict, ipv6 loose
        step(sh, "03_step3_perafi_apply", [
            "configure",
            "interfaces ge100-0/0/3/0 urpf address-family ipv4 admin-state enabled",
            "interfaces ge100-0/0/3/0 urpf address-family ipv4 mode strict",
            "interfaces ge100-0/0/3/0 urpf address-family ipv6 admin-state enabled",
            "interfaces ge100-0/0/3/0 urpf address-family ipv6 mode loose",
            "commit",
            "end",
        ])
        step(sh, "03_step3_verify", [
            "show config interfaces | no-more",
            "show interfaces ge100-0/0/3/0 | no-more",
            "show interfaces detail ge100-0/0/3/0 | no-more",
        ], settle=6)

        # Step 4: counters
        step(sh, "04_step4_counters", [
            "show interfaces counters ge100-0/0/3/0 | no-more",
        ], settle=6)

        # Step 5: loose mode on /1, independence
        step(sh, "05_step5_apply_and_verify", [
            "configure",
            "interfaces ge100-0/0/3/1 urpf admin-state enabled",
            "interfaces ge100-0/0/3/1 urpf mode loose",
            "commit",
            "end",
            "show interfaces ge100-0/0/3/0 | no-more",
            "show interfaces ge100-0/0/3/1 | no-more",
            "show config interfaces | no-more",
        ], settle=5)

        # Negative A: uRPF on parent (ge400-0/0/3) post-breakout
        step(sh, "06_negA_parent_urpf", [
            "configure",
            "interfaces ge400-0/0/3 urpf admin-state enabled",
            "commit",
            "rollback",
            "end",
            "show config interfaces | no-more",
        ], settle=5)

        # Negative B: no breakout while children admin-up; expect commit failure
        step(sh, "07_negB_no_breakout_with_children_up", [
            "configure",
            "no interfaces ge400-0/0/3 breakout",
            "commit",
        ], settle=8)
        # Always rollback any uncommitted changes; verify breakout still in place
        step(sh, "07_negB_rollback_verify", [
            "rollback",
            "end",
            "show interfaces breakout | no-more",
        ], settle=5)

        # Negative C: parent urpf show after breakout (must show no urpf under ge400-0/0/3)
        step(sh, "08_negC_parent_show_urpf", [
            "show config interfaces | no-more",
        ], settle=6)

        # Step 6: sub-interface on /2 with uRPF
        step(sh, "09_step6_subif_apply", [
            "configure",
            "interfaces ge100-0/0/3/2 admin-state enabled",
            "interfaces ge100-0/0/3/2.103 vlan-id 103",
            "interfaces ge100-0/0/3/2.103 admin-state enabled",
            "interfaces ge100-0/0/3/2.103 ipv4-address 10.103.0.1/24",
            "interfaces ge100-0/0/3/2.103 ipv6-address 2001:db8:103::1/64",
            "interfaces ge100-0/0/3/2.103 urpf admin-state enabled",
            "interfaces ge100-0/0/3/2.103 urpf mode strict",
            "commit",
            "end",
        ], settle=6)
        step(sh, "09_step6_verify", [
            "show interfaces ge100-0/0/3/2.103 | no-more",
            "show interfaces detail ge100-0/0/3/2.103 | no-more",
            "show config interfaces | no-more",
        ], settle=6)

        # Step 7 (partial cleanup): remove all uRPF/sub-int we added; keep breakout intact
        step(sh, "10_step7_cleanup", [
            "configure",
            "no interfaces ge100-0/0/3/0 urpf",
            "no interfaces ge100-0/0/3/1 urpf",
            "no interfaces ge100-0/0/3/2.103",
            "interfaces ge100-0/0/3/2 admin-state disabled",
            "commit",
            "end",
            "show interfaces breakout | no-more",
            "show config interfaces | no-more",
            "show interfaces ge100-0/0/3/0 | no-more",
            "show interfaces ge100-0/0/3/1 | no-more",
        ], settle=6)

        run(sh, "exit", settle=1)
    finally:
        c.close()

if __name__ == '__main__':
    main()
