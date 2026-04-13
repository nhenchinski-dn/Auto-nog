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
    ('IFACES', 'show interfaces summary | no-more', 10),
    ('SNMP_SUMMARY', 'show system snmp summary | no-more', 8),
    ('SNMP_TRAPS_LIST', 'show system snmp traps | include CFM | no-more', 8),
    ('RESTCONF1', 'show config system ncc ?', 5),
    ('RESTCONF2', 'show config system ncc ncc-server ?', 5),
    ('SHOW_DM_OPER', 'show services performance-monitoring cfm tests on-demand ?', 5),
]

for label, cmd, wait in cmds:
    print(f'=== {label}: {cmd} ===')
    print(run(ch, cmd, wait=wait))
    print()

ch.close()
c.close()
