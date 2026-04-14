#!/usr/bin/env python3
import paramiko, time, re, sys
ANSI = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect('WKY1C7VD00008P2', username='dnroot', password='dnroot', timeout=15, banner_timeout=15, auth_timeout=15)
ch = c.invoke_shell(width=250)
ch.settimeout(30)
time.sleep(3)
while ch.recv_ready():
    ch.recv(65536)

def run(ch, cmd, wait=12):
    ch.send(cmd + '\n')
    time.sleep(wait)
    out = ''
    while ch.recv_ready():
        out += ch.recv(65536).decode(errors='ignore')
        time.sleep(0.5)
    return ANSI.sub('', out)

cmds = [
    ('DM_DETAIL', 'show services performance-monitoring cfm tests proactive two-way-delay session-name DM_CLI_TAB_mep2 detail | no-more', 15),
    ('SLM_DETAIL', 'show services performance-monitoring cfm tests proactive two-way-synthetic-loss session-name SLM_CLI_TAB_mep2 detail | no-more', 15),
    ('ALARMS', 'show system alarms | no-more', 10),
    ('MGMT0', 'show config interface mgmt0 | no-more', 8),
    ('SNMP', 'show system snmp | no-more', 8),
]

for label, cmd, wait in cmds:
    print(f'=== {label}: {cmd} ===')
    print(run(ch, cmd, wait=wait))
    print()

ch.close()
c.close()
