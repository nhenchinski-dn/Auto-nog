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

# Check system ncc with args
print('=== SYSTEM NCC ? ===', flush=True)
print(cmd('show system ncc ?', 5), flush=True)

# Check system ncf
print('\n=== SYSTEM NCF ===', flush=True)
print(cmd('show system ncf | no-more', 8), flush=True)

# Try to enter linux shell and find SNMP process
print('\n=== LINUX SHELL - SNMP PROCESSES ===', flush=True)
print(cmd('run bash', 3), flush=True)
print(cmd('ps aux | grep -i snmp', 5), flush=True)
print(cmd('ss -tulnp | grep 161', 3), flush=True)
print(cmd('ip addr show | grep "inet "', 5), flush=True)
print(cmd('exit', 3), flush=True)

# Try with NCP shell
print('\n=== NCP DATAPATH SHELL ===', flush=True)
print(cmd('run ncp 0 bash', 5), flush=True)
print(cmd('ps aux | grep -i snmp', 5), flush=True)
print(cmd('ss -tulnp | grep 161', 3), flush=True)
print(cmd('exit', 3), flush=True)

ssh.close()
print('\nDONE', flush=True)
