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
                           r'\(commit, merge-only, abort\)', r'\(yes/no\)'], timeout=timeout)
        raw = child.before.split('\r\n')
        for line in raw[1:]:
            line = re.sub(r'\x1b\[[0-9;]*[mKHJ]', '', line)
            parts.append(line)
        if idx == 0: break
        elif idx == 1: child.sendline(' ')
        elif idx == 2: child.sendline('commit')
        elif idx == 3: child.sendline('yes')
        time.sleep(0.3)
    out = '\n'.join(parts).strip()
    return out

# Check IPv6 config
print('=== ge400-0/0/3 config ===')
out = cmd('show config interfaces ge400-0/0/3 | no-more')
print(out)

# Current NDP table
print('\n=== NDP table ===')
out = cmd('show ndp interface ge400-0/0/3 | no-more')
print(out[:2000])

print('\n=== NDP count ===')
out = cmd('show ndp interface ge400-0/0/3 | count')
print(out)

# Check ndp CLI options
print('\n=== ndp ? (in config mode) ===')
cmd('configure')
out = cmd('interfaces ge400-0/0/3 ndp ?')
print(out)
cmd('top')
cmd('end')

# Check show ndp options
print('\n=== show ndp ? ===')
out = cmd('show ndp ?')
print(out)

child.sendline('exit')
print('\nDone.')
