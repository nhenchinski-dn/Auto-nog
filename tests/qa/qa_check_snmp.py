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

# Check management interface
print('=== MANAGEMENT INTERFACE ===', flush=True)
print(cmd('show config system management | no-more', 10), flush=True)

# Check loopback interfaces
print('\n=== LOOPBACK INTERFACES ===', flush=True)
print(cmd('show interfaces lo0 | no-more', 8), flush=True)

# Check if SNMP agent is running
print('\n=== SNMP AGENT STATUS ===', flush=True)
print(cmd('show system snmp agent | no-more', 8), flush=True)

# Get NCC management address  
print('\n=== SYSTEM MANAGEMENT ===', flush=True)
print(cmd('show system management | no-more', 8), flush=True)

# Check which process handles SNMP
print('\n=== NCC PROCESSES ===', flush=True)
print(cmd('show system ncc | no-more', 10), flush=True)

# Try a local SNMP walk  
print('\n=== LOCAL SNMP TEST ===', flush=True)
print(cmd('run snmpwalk -v2c -c testacl123 localhost 1.3.6.1.4.1.49739.2.17 | no-more', 30), flush=True)

ssh.close()
print('\nDONE', flush=True)
