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

# Check current state
print('=== Current ge400-0/0/3.1 config ===')
print(cmd('show config interfaces ge400-0/0/3.1 | no-more'))

print('\n=== Current ge400-0/0/33 config ===')
print(cmd('show config interfaces ge400-0/0/33 | no-more'))

print('\n=== Current routes ===')
out = cmd('show route | no-more')
for line in out.split('\n'):
    l = line.strip()
    if l and (l.startswith('C>') or l.startswith('S>')):
        print(f'  {l}')

# Check if config survived the restart
needs_config = False
out = cmd('show config interfaces ge400-0/0/3.1 | no-more')
if 'urpf' not in out:
    needs_config = True
    print('\n>>> Config lost after restart, re-applying...')

if needs_config:
    cmd('configure')
    cmd('interfaces ge400-0/0/3.1 l2-service disabled')
    cmd('interfaces ge400-0/0/3.1 ipv4-address 10.1.1.1/24')
    cmd('interfaces ge400-0/0/3.1 ipv6-address 2001:db8:1::1/64')
    cmd('interfaces ge400-0/0/3.1 vlan-id 1')
    cmd('interfaces ge400-0/0/3.1 urpf admin-state enabled')
    cmd('interfaces ge400-0/0/3.1 urpf mode strict')
    cmd('interfaces ge400-0/0/3.1 urpf address-family ipv4 admin-state enabled')
    cmd('interfaces ge400-0/0/3.1 urpf address-family ipv4 mode strict')
    cmd('interfaces ge400-0/0/3.1 urpf address-family ipv6 admin-state enabled')
    cmd('interfaces ge400-0/0/3.1 urpf address-family ipv6 mode strict')
    cmd('interfaces ge400-0/0/33 ipv4-address 10.33.0.1/24')
    cmd('interfaces ge400-0/0/33 ipv6-address 2001:db8:33::1/64')
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

# Final verification
print('\n========== FINAL CONFIG ==========')
print('\n--- ge400-0/0/3.1 ---')
print(cmd('show config interfaces ge400-0/0/3.1 | no-more'))

print('\n--- ge400-0/0/33 ---')
print(cmd('show config interfaces ge400-0/0/33 | no-more'))

print('\n--- Routes ---')
out = cmd('show route | no-more')
for line in out.split('\n'):
    l = line.strip()
    if l and (l.startswith('C>') or l.startswith('S>')):
        print(f'  {l}')

print('\n--- Counters on ge400-0/0/3.1 ---')
out = cmd('show interfaces counters ge400-0/0/3.1 | no-more')
for line in out.split('\n'):
    l = line.strip()
    if any(w in l.lower() for w in ['urpf', 'drop', 'operational']):
        print(f'  {l}')

# Find warm restart command
print('\n--- Warm restart command ---')
out = cmd('request ncc warm ?')
print(out)

child.sendline('exit')
print('\nDone.')
