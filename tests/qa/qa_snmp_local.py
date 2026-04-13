#!/usr/bin/env python3
import paramiko, time, re, sys
sys.stdout.reconfigure(line_buffering=True)

host = 'XEC1E3VR00008'
user = 'dnroot'
pw = 'dnroot'

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(host, username=user, password=pw, timeout=15, look_for_keys=False, allow_agent=False)

chan = ssh.invoke_shell(width=400, height=1000)
time.sleep(5)
chan.recv(65535)

ANSI_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07|\x1b[()][AB012]|\x1b\[\?[0-9;]*[hlm]|\r')

def cmd(c, wait=5):
    chan.send(c + '\n')
    time.sleep(wait)
    out = b''
    while chan.recv_ready():
        out += chan.recv(65535)
    return ANSI_RE.sub('', out.decode(errors='replace')).strip()

# Rollback the management VRF change
print('=== ROLLBACK ===', flush=True)
cmd('configure', 3)
cmd('rollback', 5)
cmd('end', 3)

# Try to find SNMP listening port via shell on routing-engine
print('\n=== SHELL INTO ROUTING-ENGINE ===', flush=True)
print(cmd('request ncc 0 routing-engine bash', 8), flush=True)
print(cmd('ss -tulnp | grep 161', 5), flush=True)
print(cmd('ip addr show | grep "inet "', 5), flush=True)
print(cmd('snmpwalk -v2c -c testacl123 127.0.0.1 1.3.6.1.4.1.49739.2.17 2>&1 | head -30', 30), flush=True)
print(cmd('exit', 3), flush=True)

ssh.close()
print('\nDONE', flush=True)
