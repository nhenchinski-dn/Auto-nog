#!/usr/bin/env python3
"""SW-244107: Focused SH-BFD confirmation.

Uses Linux shell ping (via SSH port 2222) as data-plane traffic source.
Also restores the static ARP so ping packets can egress bundle-10.

Phase A: ACL only, no SH-BFD → ping → counters should increment
Phase B: Add SH-BFD config   → ping → counters should freeze
Phase C: Remove SH-BFD       → ping → counters should recover
"""

import paramiko, time, re, sys, threading

HOST = "WKY1C7VD00008P2"
USER = "dnroot"
PASS = "dnroot"

def clean(t):
    t = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', t)
    t = re.sub(r'\r', '', t)
    t = re.sub(r'-- More -- \(Press q to quit\)\s*', '', t)
    return t

def recv_all(chan, timeout=8):
    end = time.time() + timeout
    buf = b''
    while time.time() < end:
        if chan.recv_ready():
            buf += chan.recv(65535)
            end = time.time() + 1
        else:
            time.sleep(0.2)
    return clean(buf.decode(errors='replace'))

def run(chan, cmd, wait=3, timeout=10):
    chan.send(cmd + '\n')
    time.sleep(wait)
    return recv_all(chan, timeout=timeout)

def show(chan, cmd, wait=5):
    full = cmd if '| no-more' in cmd else cmd + ' | no-more'
    return run(chan, full, wait=wait, timeout=12)

def get_rule1_matches(chan):
    out = show(chan, "show access-lists counters")
    for line in out.split('\n'):
        if 'egress-bfd' in line and '| 1 ' in line:
            cols = [c.strip() for c in line.split('|') if c.strip()]
            for c in reversed(cols):
                if c.isdigit():
                    return int(c)
    return -1

def commit(chan, label):
    out = run(chan, "commit", wait=25, timeout=30)
    ok = not bool(re.search(r'(?i)error|fail|reject|abort', out))
    print(f"  COMMIT [{label}]: {'OK' if ok else 'ERROR'}")
    if not ok:
        for line in out.strip().split('\n'):
            s = line.strip()
            if s and ('error' in s.lower() or 'fail' in s.lower()):
                print(f"    {s}")
    return ok

def linux_ping(host, dest, count, interval=0.2):
    """Run ping from Linux shell via SSH port 2222."""
    ssh2 = paramiko.SSHClient()
    ssh2.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh2.connect(host, port=2222, username="dnroot", password="dnroot",
                     timeout=15, look_for_keys=False, allow_agent=False)
        cmd = f"ping -c {count} -i {interval} {dest}"
        stdin, stdout, stderr = ssh2.exec_command(cmd, timeout=count*2+30)
        out = stdout.read().decode(errors='replace')
        for line in out.strip().split('\n'):
            if 'transmitted' in line or 'rtt' in line:
                print(f"    {line.strip()}")
        return out
    except Exception as e:
        print(f"    Linux ping error: {e}")
        # Fallback: try through access_host.sh
        return ""
    finally:
        ssh2.close()

def measure_with_ping(cli_chan, wait_sec, ping_count, label):
    """Measure ACL counter delta while sending pings."""
    before = get_rule1_matches(cli_chan)
    print(f"  [{label}] Matches BEFORE: {before}")

    print(f"  [{label}] Sending {ping_count} pings from Linux shell...")
    ping_thread = threading.Thread(
        target=linux_ping, args=(HOST, "20.0.0.2", ping_count, 0.5))
    ping_thread.start()

    time.sleep(wait_sec)
    ping_thread.join(timeout=10)

    after = get_rule1_matches(cli_chan)
    delta = after - before
    print(f"  [{label}] Matches AFTER:  {after}")
    print(f"  [{label}] Delta: +{delta}")
    return delta

def main():
    print("SW-244107 SH-BFD Confirmation Test (Linux ping)")
    print("=" * 60)

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print("Connecting to CLI...")
    ssh.connect(HOST, username=USER, password=PASS, timeout=30,
                look_for_keys=False, allow_agent=False)
    chan = ssh.invoke_shell(width=400, height=5000)
    time.sleep(6)
    chan.recv(65535)
    print("Connected.\n")

    # ============================================================
    # SETUP: uBFD, ACL, static ARP, no SH-BFD
    # ============================================================
    print("--- Setting up clean state ---")
    run(chan, "configure", wait=2)

    # Static ARP so ping can egress bundle-10
    run(chan, "interfaces bundle-10", wait=2)
    run(chan, "arp host-address 20.0.0.2 mac-address 00:11:22:33:44:55", wait=2)
    run(chan, "access-list ipv4 egress-bfd direction out", wait=2)
    run(chan, "top", wait=1)

    # uBFD
    run(chan, "protocols bfd", wait=2)
    run(chan, "interface bundle-10", wait=2)
    run(chan, "local-address 20.0.0.1", wait=2)
    run(chan, "neighbor 20.0.1.1", wait=2)
    run(chan, "top", wait=1)
    run(chan, "protocols bfd", wait=2)
    run(chan, "interface bundle-20", wait=2)
    run(chan, "local-address 20.0.1.1", wait=2)
    run(chan, "neighbor 20.0.0.1", wait=2)
    run(chan, "top", wait=1)

    # Remove any leftover SH-BFD
    run(chan, "protocols segment-routing mpls", wait=2)
    run(chan, "no policy TEST_SBFD_POLICY", wait=2)
    run(chan, "top", wait=1)

    commit(chan, "Setup: ARP + ACL + uBFD, no SH-BFD")
    run(chan, "exit", wait=2)
    time.sleep(10)

    # Quick test that Linux ping works
    print("\n  Testing Linux shell ping (port 2222)...")
    linux_ping(HOST, "20.0.0.2", 3, 1)

    PING_COUNT = 30
    WAIT = 20

    # ============================================================
    # PHASE A: No SH-BFD → ping → counters should increment
    # ============================================================
    print(f"\n{'='*60}")
    print("PHASE A: uBFD + ACL, NO SH-BFD — sending pings")
    print(f"{'='*60}")

    delta_a = measure_with_ping(chan, WAIT, PING_COUNT, "Phase A")
    result_a = "PASS - counters increment" if delta_a > 0 else "FAIL - counters stuck"
    print(f"  PHASE A: {result_a}")

    # ============================================================
    # PHASE B: Add SH-BFD → ping → counters should freeze
    # ============================================================
    print(f"\n{'='*60}")
    print("PHASE B: ADD SR-MPLS seamless-bfd config")
    print(f"{'='*60}")

    run(chan, "configure", wait=2)
    run(chan, "protocols segment-routing mpls", wait=2)
    run(chan, "policy TEST_SBFD_POLICY", wait=2)
    run(chan, "destination 20.0.1.1 strict-spf", wait=2)
    run(chan, "color 100", wait=2)
    run(chan, "seamless-bfd", wait=2)
    run(chan, "admin-state enabled", wait=2)
    run(chan, "remote-reflector-discriminator 20.0.1.1", wait=2)
    run(chan, "interval 300", wait=2)
    run(chan, "multiplier 3", wait=2)
    run(chan, "top", wait=1)
    commit(chan, "Add SH-BFD")
    run(chan, "exit", wait=2)
    time.sleep(5)

    delta_b = measure_with_ping(chan, WAIT, PING_COUNT, "Phase B")
    result_b = "UNEXPECTED - counters still work" if delta_b > 0 else "SH-BFD BREAKS egress ACL counters"
    print(f"  PHASE B: {result_b}")

    # ============================================================
    # PHASE C: Remove SH-BFD → ping → counters recover?
    # ============================================================
    print(f"\n{'='*60}")
    print("PHASE C: REMOVE SR-MPLS seamless-bfd config")
    print(f"{'='*60}")

    run(chan, "configure", wait=2)
    run(chan, "protocols segment-routing mpls", wait=2)
    run(chan, "no policy TEST_SBFD_POLICY", wait=2)
    run(chan, "top", wait=1)
    commit(chan, "Remove SH-BFD")
    run(chan, "exit", wait=2)
    time.sleep(5)

    delta_c = measure_with_ping(chan, WAIT, PING_COUNT, "Phase C")
    result_c = "RECOVERED" if delta_c > 0 else "STILL BROKEN"
    print(f"  PHASE C: {result_c}")

    # ============================================================
    # SUMMARY
    # ============================================================
    print(f"\n{'='*60}")
    print("SUMMARY — SW-244107: Egress ACL + SH-BFD")
    print(f"{'='*60}")
    print(f"  Phase A (no SH-BFD):      +{delta_a:>4} matches  [{result_a}]")
    print(f"  Phase B (SH-BFD added):   +{delta_b:>4} matches  [{result_b}]")
    print(f"  Phase C (SH-BFD removed): +{delta_c:>4} matches  [{result_c}]")

    run(chan, "exit", wait=2)
    chan.close()
    ssh.close()
    print("\n=== Done ===")

if __name__ == "__main__":
    main()
