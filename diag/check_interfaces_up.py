import pexpect, time, re

DEVICE = 'WKY1C7VD00008P2'
child = pexpect.spawn(f'ssh -o StrictHostKeyChecking=no dnroot@{DEVICE}',
                      timeout=30, encoding='utf-8', maxread=65536)
child.expect('[Pp]assword:')
child.sendline('dnroot')
child.expect('CLI Loading', timeout=30)
child.expect(r'NCP3-nog[^\r\n]*[#>]', timeout=60)
print('Connected.')

def cmd(c, timeout=30):
    child.sendline(c)
    time.sleep(0.5)
    parts = []
    while True:
        idx = child.expect([r'NCP3-nog[^\r\n]*[#>]', r'-- More --',
                           r'Are you sure.*\?'], timeout=timeout)
        raw = child.before.split('\r\n')
        for line in raw[1:]:
            line = re.sub(r'\x1b\[[0-9;]*[mKHJ]', '', line)
            parts.append(line)
        if idx == 0: break
        elif idx == 1: child.sendline(' ')
        elif idx == 2: child.sendline('no')
        time.sleep(0.3)
    return '\n'.join(parts).strip()

print('=== Interfaces that are UP ===')
out = cmd('show interfaces summary | no-more')
for line in out.split('\n'):
    l = line.strip()
    if 'up' in l.lower() and ('ge' in l.lower() or 'bundle' in l.lower()):
        print(f'  {l}')

print('\n=== All interfaces summary ===')
print(cmd('show interfaces summary | no-more'))

child.sendline('exit')
