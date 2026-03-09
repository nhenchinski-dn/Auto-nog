#!/usr/bin/env python3
"""Helper to call network-mapper MCP tools."""
import requests, json, threading, time, sys

BASE_URL = 'http://192.168.174.88:8080'

def mcp_call(method_name, arguments, timeout=30):
    resp = requests.get(f'{BASE_URL}/sse', stream=True, timeout=10, headers={'Accept': 'text/event-stream'})
    for line in resp.iter_lines(decode_unicode=True):
        if line.startswith('data:'):
            break
    resp.close()
    results = {'session_id': None, 'ready': False, 'responses': []}
    def listen(r):
        try:
            rr = requests.get(f'{BASE_URL}/sse', stream=True, timeout=max(timeout+10,60), headers={'Accept': 'text/event-stream'})
            for line in rr.iter_lines(decode_unicode=True):
                if line.startswith('data:'):
                    data = line.split('data:', 1)[1].strip()
                    if data.startswith('/messages/'):
                        r['session_id'] = data.split('session_id=')[1]
                        r['ready'] = True
                    else:
                        try: r['responses'].append(json.loads(data))
                        except: pass
        except: pass
    t = threading.Thread(target=listen, args=(results,), daemon=True)
    t.start()
    for _ in range(50):
        if results.get('ready'): break
        time.sleep(0.1)
    if not results.get('ready'): return "Failed to get session"
    sid = results['session_id']
    url = f'{BASE_URL}/messages/?session_id={sid}'
    init = {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}
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
                for c in content:
                    if c.get('type') == 'text': return c['text']
                return json.dumps(r, indent=2)
        time.sleep(0.2)
    return "Timeout"

if __name__ == '__main__':
    tool = sys.argv[1]
    args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    tout = int(sys.argv[3]) if len(sys.argv) > 3 else 30
    print(mcp_call(tool, args, timeout=tout))
