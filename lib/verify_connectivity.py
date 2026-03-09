#!/usr/bin/env python3
"""Quick SSH connectivity check for both Y1731 test devices."""
import sys, time
import paramiko

DEVICES = [
    ("WKY1C7VD00008P2", "dnroot", "dnroot"),
    ("xec1e3vr00008", "dnroot", "dnroot"),
]

def check_device(host, user, password, timeout=15):
    print(f"\n{'='*60}")
    print(f"Checking: {host}")
    print(f"{'='*60}")
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(host, username=user, password=password,
                       timeout=timeout, banner_timeout=timeout, auth_timeout=timeout)
        transport = client.get_transport()
        if transport:
            transport.set_keepalive(30)
        
        channel = client.invoke_shell()
        channel.settimeout(timeout)
        
        buf = ""
        end = time.time() + timeout
        while time.time() < end:
            if channel.recv_ready():
                buf += channel.recv(4096).decode(errors="ignore")
                if buf.strip().endswith("#") or buf.strip().endswith(">"):
                    break
            else:
                time.sleep(0.2)
        
        print(f"  [OK] Connected. Banner/prompt received.")
        
        cmds = [
            "show system information | match hostname",
            "show config services ethernet-oam connectivity-fault-management | display-set | no-more",
            "show config services performance-monitoring | display-set | no-more",
        ]
        for cmd in cmds:
            channel.send(cmd + "\n")
            out = ""
            end = time.time() + 15
            while time.time() < end:
                if channel.recv_ready():
                    out += channel.recv(4096).decode(errors="ignore")
                    if out.strip().endswith("#") or out.strip().endswith(">"):
                        break
                else:
                    time.sleep(0.2)
            lines = [l.strip() for l in out.splitlines() if l.strip() and not l.strip().endswith(("#", ">"))]
            print(f"\n  CMD: {cmd}")
            for l in lines[:20]:
                print(f"    {l}")
            if len(lines) > 20:
                print(f"    ... ({len(lines)} lines total)")
        
        channel.close()
        client.close()
        return True
    except Exception as e:
        print(f"  [FAIL] {type(e).__name__}: {e}")
        return False

results = {}
for host, user, pw in DEVICES:
    results[host] = check_device(host, user, pw)

print(f"\n{'='*60}")
print("CONNECTIVITY SUMMARY")
print(f"{'='*60}")
for host, ok in results.items():
    print(f"  {host}: {'OK' if ok else 'FAILED'}")
