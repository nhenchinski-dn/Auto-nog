#!/usr/bin/env python3
"""Reproduce SW-241906: BGP local-as non-descriptive error message."""
import requests, json, threading, time, paramiko, sys, re

BASE_URL = 'http://192.168.174.88:8080'

def mcp_call(method_name, arguments, timeout=30):
    resp = requests.get(f'{BASE_URL}/sse', stream=True, timeout=10, headers={'Accept': 'text/event-stream'})
    for line in resp.iter_lines(decode_unicode=True):
        if line.startswith('data:'): break
    resp.close()
    results = {'session_id': None, 'ready': False, 'responses': []}
    def listen(r):
        try:
            rr = requests.get(f'{BASE_URL}/sse', stream=True, timeout=max(timeout+10,60), headers={'Accept': 'text/event-stream'})
            for line in rr.iter_lines(decode_unicode=True):
                if line.startswith('data:'):
                    data = line.split('data:', 1)[1].strip()
                    if data.startswith('/messages/'): r['session_id'] = data.split('session_id=')[1]; r['ready'] = True
                    else:
                        try: r['responses'].append(json.loads(data))
                        except: pass
        except: pass
    t = threading.Thread(target=listen, args=(results,), daemon=True); t.start()
    for _ in range(50):
        if results.get('ready'): break
        time.sleep(0.1)
    if not results.get('ready'): return "Failed to get session"
    sid = results['session_id']; url = f'{BASE_URL}/messages/?session_id={sid}'
    init = {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"repro","version":"1.0"}}}
    requests.post(url, json=init, timeout=10)
    for _ in range(50):
        if results['responses']: break
        time.sleep(0.1)
    call = {"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":method_name,"arguments":arguments}}
    requests.post(url, json=call, timeout=10)
    end_time = time.time() + timeout
    while time.time() < end_time:
        for r in results['responses']:
            if r.get('id') == 2:
                content = r.get('result',{}).get('content',[])
                texts = [c['text'] for c in content if c.get('type') == 'text']
                if texts: return '\n'.join(texts)
                return json.dumps(r, indent=2)
        time.sleep(0.2)
    return "Timeout"

def ssh_interactive(host, user, password, commands, timeout=30):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, username=user, password=password, timeout=timeout, banner_timeout=timeout, auth_timeout=timeout)
    transport = client.get_transport()
    if transport: transport.set_keepalive(30)
    chan = client.invoke_shell(width=200, height=50)
    chan.settimeout(timeout)
    time.sleep(2)
    output = ""
    if chan.recv_ready(): output += chan.recv(65535).decode('utf-8', errors='replace')
    for cmd in commands:
        print(f">>> Sending: {cmd}", flush=True)
        chan.send(cmd + "\n")
        time.sleep(3)
        chunk = ""
        end_t = time.time() + 15
        while time.time() < end_t:
            if chan.recv_ready():
                data = chan.recv(65535).decode('utf-8', errors='replace')
                chunk += data; time.sleep(0.5)
            else:
                time.sleep(0.5)
                if not chan.recv_ready(): break
        output += chunk
        print(chunk, flush=True)
    chan.close(); client.close()
    return output

device_name = "NCP3-CFM-nog"
print(f"=== Get management interfaces for {device_name} ===", flush=True)
mgmt_info = mcp_call("get_device_management_interfaces", {"device_name": device_name}, timeout=15)
print(mgmt_info, flush=True)
ips = re.findall(r'(\d+\.\d+\.\d+\.\d+)', mgmt_info)
print(f"Found IPs: {ips}", flush=True)
ssh_ip = None
for ip in ips:
    if ip.startswith('100.64.'):
        print(f"Trying SSH to {ip}...", flush=True)
        try:
            c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            c.connect(ip, username='dnroot', password='dnroot', timeout=10); c.close()
            ssh_ip = ip; print(f"SSH OK: {ip}", flush=True); break
        except Exception as e: print(f"SSH fail {ip}: {e}", flush=True)
if ssh_ip:
    print(f"\n=== Reproducing bug via SSH to {ssh_ip} ===", flush=True)
    out = ssh_interactive(ssh_ip, 'dnroot', 'dnroot', [
        "config",
        "protocols bgp 65001 neighbor 1.1.1.1 remote-as 65001",
        "protocols bgp 65001 neighbor 1.1.1.1 local-as 65001",
        "commit",
    ])
    print("\n=== Cleanup ===", flush=True)
    try: ssh_interactive(ssh_ip, 'dnroot', 'dnroot', ["config", "rollback 0", "commit"])
    except Exception as e: print(f"Cleanup err: {e}", flush=True)
    print("\n=== FULL OUTPUT ===", flush=True)
    print(out, flush=True)
else:
    print("No SSH, trying device_shell_execute", flush=True)
    r = mcp_call("device_shell_execute", {"device_name": device_name, "command": "cli -c 'config ; protocols bgp 65001 neighbor 1.1.1.1 remote-as 65001 ; protocols bgp 65001 neighbor 1.1.1.1 local-as 65001 ; commit'"}, timeout=30)
    print(r, flush=True)
print("=== DONE ===", flush=True)
