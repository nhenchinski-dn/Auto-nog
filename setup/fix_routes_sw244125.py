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
                           r'\(commit, merge-only, abort\)',
                           r'Are you sure.*\?'], timeout=timeout)
        raw = child.before.split('\r\n')
        for line in raw[1:]:
            line = re.sub(r'\x1b\[[0-9;]*[mKHJ]', '', line)
            parts.append(line)
        if idx == 0: break
        elif idx == 1: child.sendline(' ')
        elif idx == 2: child.sendline('commit')
        elif idx == 3: child.sendline('no')
        time.sleep(0.3)
    return '\n'.join(parts).strip()

# Check full route table
print('=== Full route output ===')
print(cmd('show route | no-more'))

# Check static route config
print('\n=== Static route config ===')
print(cmd('show config protocols static | no-more'))

# Check ge400-0/0/3.1 operational state
print('\n=== ge400-0/0/3.1 status ===')
print(cmd('show interfaces ge400-0/0/3.1 | no-more'))

# Check parent interface
print('\n=== ge400-0/0/3 status ===')
out = cmd('show interfaces ge400-0/0/3 | no-more')
for line in out.split('\n'):
    l = line.strip()
    if any(w in l.lower() for w in ['operational', 'admin', 'speed', 'link']):
        print(f'  {l}')

# Add routes if missing
print('\n=== Adding static routes ===')
cmd('configure')
cmd('protocols static address-family ipv4-unicast route 198.51.100.0/24 next-hop 10.1.1.2')
cmd('protocols static address-family ipv6-unicast route 2001:db8:100::/48 next-hop 2001:db8:1::2')
cmd('protocols static address-family ipv4-unicast route 203.0.113.0/24 next-hop 10.33.0.2')
cmd('protocols static address-family ipv6-unicast route 2001:db8:200::/48 next-hop 2001:db8:33::2')
cmd('protocols static address-family ipv4-unicast route 99.0.0.0/24 next-hop 10.33.0.2')
cmd('protocols static address-family ipv6-unicast route cafe::/64 next-hop 2001:db8:33::2')
out = cmd('commit')
print(f'Commit: {out}')
cmd('top')
cmd('end')

# Verify routes
print('\n=== Routes after fix ===')
out = cmd('show route | no-more')
for line in out.split('\n'):
    l = line.strip()
    if l and (l.startswith('C>') or l.startswith('S>') or l.startswith('IPv')):
        print(f'  {l}')

# Final sub-interface state
print('\n=== ge400-0/0/3.1 final state ===')
print(cmd('show interfaces ge400-0/0/3.1 | no-more'))

# Clear counters for clean baseline
cmd('clear int counters')
print('\nBaseline counters cleared.')

child.sendline('exit')
print('\nDone.')
