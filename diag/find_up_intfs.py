import pexpect, time, re

DEVICE = 'WKY1C7VD00008P2'
child = pexpect.spawn(f'ssh -o StrictHostKeyChecking=no dnroot@{DEVICE}',
                      timeout=30, encoding='utf-8', maxread=65536)
child.expect('[Pp]assword:')
child.sendline('dnroot')
child.expect('CLI Loading', timeout=30)
child.expect(r'NCP3-nog[^\r\n]*[#>]', timeout=60)
print('Connected.')

def cmd(c, timeout=60):
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

# Check specific interfaces we know about
for intf in ['ge400-0/0/3', 'ge400-0/0/4', 'ge400-0/0/33', 'ge400-0/0/34',
             'ge100-0/0/1', 'ge100-0/0/2', 'ge100-0/0/3', 'ge100-0/0/4',
             'ge10-0/0/1', 'ge10-0/0/2']:
    out = cmd(f'show interfaces {intf} | no-more')
    for line in out.split('\n'):
        l = line.strip()
        if 'Operational state' in l or 'Physical link' in l:
            print(f'{intf}: {l}')
            break
    else:
        if 'ERROR' in out:
            pass  # skip non-existent

# Also check what interface names exist
print('\n=== show interfaces ? ===')
out = cmd('show interfaces ?')
print(out[:2000])

child.sendline('exit')
