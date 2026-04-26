#!/usr/bin/env python3
"""Probe DUT state, clean leftover BGP/BFD, verify Spirent 1/25 -> ge100-0/0/3/0 path."""
import paramiko
import time
import re
import sys

from stcrestclient import stchttp

HOST = "WKY1C7VD00008P2"
LABSERVER = "il-auto-containers"
CHASSIS_IP = "100.64.15.236"
SLOT = 1
PORT = 25

ANSI = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')


def clean(t):
    return re.sub(r'-- More -- \(Press q to quit\)\s*', '',
                  re.sub(r'\r', '', ANSI.sub('', t)))


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
    return recv_all(shell, timeout=5)


def main():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username='dnroot', password='dnroot',
                look_for_keys=False, allow_agent=False, timeout=20)
    shell = ssh.invoke_shell(width=250, height=5000)
    time.sleep(6)
    shell.recv(65535)

    print("=== Current state ===")
    print(run(shell, "set cli-no-confirm", wait=2))
    print(run(shell, "show config protocols | no-more", wait=3))
    print(run(shell, "show config access-lists | no-more", wait=3))
    print(run(shell, f"show config interfaces ge100-0/0/3/0 | no-more", wait=3))

    print("\n=== Cleanup stale config (BGP, uBFD, revert ACL to allow, drop ingress uRPF) ===")
    run(shell, "configure", wait=3)
    run(shell, "no protocols bgp", wait=2)
    run(shell, "no protocols bfd", wait=2)
    run(shell, "no access-lists ipv4 egress-bfd rule 1", wait=2)
    run(shell, "access-lists ipv4 egress-bfd rule 1 allow", wait=2)
    run(shell, "no interfaces ge100-0/0/3/0 urpf", wait=2)
    out = run(shell, "commit", wait=10)
    print(out)
    if 'out of sync' in out.lower():
        print("Handling out-of-sync ...")
        shell.send("commit\n")
        time.sleep(8)
        print(recv_all(shell, timeout=10))
    run(shell, "top", wait=1)
    run(shell, "exit", wait=1)

    print("\n=== State after cleanup ===")
    print(run(shell, "show config protocols | no-more", wait=3))
    print(run(shell, "show config access-lists | no-more", wait=3))
    print(run(shell, "show config interfaces ge100-0/0/3/0 | no-more", wait=3))

    # ------- Verify traffic path -------
    print("\n=== Clearing interface counters and starting Spirent traffic ===")
    run(shell, "clear counters interfaces", wait=3)
    time.sleep(2)

    # Snapshot before
    print(">>> before traffic:")
    print(run(shell, "show interfaces counters ge100-0/0/3/0 | no-more", wait=5))

    stc = stchttp.StcHttp(LABSERVER, port=80)
    for s in stc.sessions():
        if 'sw244107_retest' in s or 'sw244107_probe' in s:
            try:
                stc.join_session(s)
                stc.end_session(s)
            except Exception:
                pass

    sid = stc.new_session('dn', 'sw244107_probe')
    stc.join_session(sid)
    project = stc.get('system1', 'children-project')
    port = stc.create('port', under=project)
    stc.config(port, {'location': f'//{CHASSIS_IP}/{SLOT}/{PORT}'})
    stc.perform('AttachPorts', params={'RevokeOwner': 'true'})
    stc.apply()
    online = stc.get(port, 'Online')
    print(f"Spirent port online={online}")

    sb = stc.create('streamBlock', under=port)
    stc.config(sb, {
        'Name': 'probe', 'FixedFrameLength': '128',
        'LoadUnit': 'FRAMES_PER_SECOND', 'Load': '1000000',
        'InsertSig': 'FALSE',
    })
    eth = stc.get(sb, 'children-ethernet:EthernetII').split()[0]
    stc.config(eth, {'srcMac': '00:10:94:00:00:25', 'dstMac': 'e8:c5:7a:d6:30:18'})
    ipv4 = stc.get(sb, 'children-ipv4:IPv4').split()[0]
    stc.config(ipv4, {'sourceAddr': '10.10.10.2', 'destAddr': '20.0.0.2', 'ttl': '64'})
    stc.apply()

    gen = stc.get(port, 'children-generator')
    gen_cfg = stc.get(gen, 'children-generatorconfig')
    stc.config(gen_cfg, {
        'SchedulingMode': 'PORT_BASED', 'DurationMode': 'CONTINUOUS',
        'LoadUnit': 'FRAMES_PER_SECOND', 'FixedLoad': '1000000',
    })
    stc.apply()
    stc.perform('GeneratorStart', params={'GeneratorList': gen})
    print("Generator started, waiting 10 s ...")
    time.sleep(10)

    # spirent TX count
    gen_results = stc.get(port, 'children-generatorportresults')
    tx_pkts = stc.get(gen_results, 'TotalFrameCount')
    print(f"Spirent TotalFrameCount after 10s: {tx_pkts}")
    ana = stc.get(port, 'children-analyzerportresults')
    rx_pkts = stc.get(ana, 'TotalFrameCount')
    print(f"Spirent analyzer RX (on same port): {rx_pkts}")

    print("\n>>> DUT ingress counters during traffic:")
    print(run(shell, "show interfaces counters ge100-0/0/3/0 | no-more", wait=5))

    print("\n>>> DUT egress bundle-10 counters during traffic:")
    print(run(shell, "show interfaces counters bundle-10 | no-more", wait=5))

    print("\n>>> ACL counters:")
    print(run(shell, "show access-lists counters bundle-10 | no-more", wait=5))

    print("\n>>> show route 20.0.0.2/32:")
    print(run(shell, "show route 20.0.0.2 | no-more", wait=4))

    stc.perform('GeneratorStop', params={'GeneratorList': gen})
    time.sleep(2)
    stc.end_session(sid)

    ssh.close()


if __name__ == "__main__":
    main()
