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

# Remove old duplicate routes from previous test
print('=== Cleaning up old routes ===')
cmd('configure')

# Remove old next-hops that go via ge400-0/0/3 (parent) — keep only sub-interface ones
cmd('no protocols static address-family ipv4-unicast route 198.51.100.0/24 next-hop 10.0.3.2')
cmd('no protocols static address-family ipv4-unicast route 203.0.113.0/24 next-hop 10.0.33.2')
cmd('no protocols static address-family ipv4-unicast route 99.0.0.0/24 next-hop 10.0.33.2')
cmd('no protocols static address-family ipv4-unicast route 192.0.2.0/24')
cmd('no protocols static address-family ipv6-unicast route 2001:db8:100::/48 next-hop 2001:db8:3::2')
cmd('no protocols static address-family ipv6-unicast route 2001:db8:200::/48 next-hop 2001:db8:33::2')
cmd('no protocols static address-family ipv6-unicast route 2001:db8:dead::/48')

out = cmd('commit')
print(f'Commit: {out}')
cmd('top')
cmd('end')

# Verify clean routes
print('\n=== Routes after cleanup ===')
out = cmd('show route | no-more')
for line in out.split('\n'):
    l = line.strip()
    if l and (l.startswith('C>') or l.startswith('S>') or l.startswith(' ')):
        print(f'  {l}')

# Find warm restart command
print('\n=== Finding warm restart command ===')
out = cmd('request ?')
print(out)

print('\n=== request system ? ===')
out = cmd('request system ?')
print(out)

# Try different restart/reboot commands
for c in ['request system restart ?', 'request system reboot ?', 'request ncc ?', 'request ncc restart ?']:
    print(f'\n--- {c} ---')
    out = cmd(c)
    if 'ERROR' not in out:
        print(out[:500])
    else:
        print(out[:200])

# Check show system status
print('\n=== show system ? ===')
out = cmd('show system ?')
print(out)

print('\n=== show sys status ===')
out = cmd('show sys status | no-more')
print(out[:1000])

child.sendline('exit')
print('\nDone.')
