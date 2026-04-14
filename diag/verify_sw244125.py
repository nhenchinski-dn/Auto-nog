import pexpect, time, re

DEVICE = 'WKY1C7VD00008P2'
child = pexpect.spawn(f'ssh -o StrictHostKeyChecking=no dnroot@{DEVICE}',
                      timeout=30, encoding='utf-8', maxread=65536)
child.expect('[Pp]assword:')
child.sendline('dnroot')
child.expect('CLI Loading', timeout=30)
child.expect(r'NCP3-nog[^\r\n]*[#>]', timeout=60)
print('Connected - device is up.')

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

# Verify system is operational
print('\n=== System version ===')
out = cmd('show sys version | no-more')
print(out[:500])

# Verify config intact
print('\n=== ge400-0/0/3.1 config ===')
out = cmd('show config interfaces ge400-0/0/3.1 | no-more')
print(out)

# Verify routes
print('\n=== Routes ===')
out = cmd('show route | no-more')
for line in out.split('\n'):
    l = line.strip()
    if l and (l.startswith('C>') or l.startswith('S>')):
        print(f'  {l}')

# Verify counters
print('\n=== Counters on ge400-0/0/3.1 ===')
out = cmd('show interfaces counters ge400-0/0/3.1 | no-more')
for line in out.split('\n'):
    l = line.strip()
    if any(w in l.lower() for w in ['urpf', 'drop', 'rx frame', 'rx octets', 'operational', 'rx packet']):
        print(f'  {l}')

# Find correct show system status
print('\n=== show sys ? ===')
out = cmd('show sys ?')
print(out[:1000])

child.sendline('exit')
print('\nVerification complete.')
