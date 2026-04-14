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

# Check current state first
print('=== Current ge400-0/0/3.1 config ===')
out = cmd('show config interfaces ge400-0/0/3.1 | no-more')
print(out)

print('\n=== Current ge400-0/0/33 config ===')
out = cmd('show config interfaces ge400-0/0/33 | no-more')
print(out)

print('\n=== Current routes ===')
out = cmd('show route | no-more')
print(out)

# Configure the test setup
print('\n=== Configuring for SW-244125 ===')
cmd('configure')

# Ingress sub-interface: ge400-0/0/3.1 with uRPF strict + per-AFI
# Already has l2-service disabled and vlan-id 1
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

# Egress interface: ge400-0/0/33 (different interface for spoofed route)
cmd('interfaces ge400-0/0/33 ipv4-address 10.33.0.1/24')
cmd('interfaces ge400-0/0/33 ipv6-address 2001:db8:33::1/64')

# Static routes:
# Customer prefix — via ingress sub-interface ge400-0/0/3.1 (RPF PASS)
cmd('protocols static address-family ipv4-unicast route 198.51.100.0/24 next-hop 10.1.1.2')
cmd('protocols static address-family ipv6-unicast route 2001:db8:100::/48 next-hop 2001:db8:1::2')

# Spoofed prefix — via different interface ge400-0/0/33 (RPF DROP)
cmd('protocols static address-family ipv4-unicast route 203.0.113.0/24 next-hop 10.33.0.2')
cmd('protocols static address-family ipv6-unicast route 2001:db8:200::/48 next-hop 2001:db8:33::2')

# Destination route (for forwarding valid traffic)
cmd('protocols static address-family ipv4-unicast route 99.0.0.0/24 next-hop 10.33.0.2')
cmd('protocols static address-family ipv6-unicast route cafe::/64 next-hop 2001:db8:33::2')

# Commit
print('\n--- Committing ---')
out = cmd('commit')
print(out)
cmd('top')
cmd('end')

# Verify
print('\n=== Verify: ge400-0/0/3.1 config ===')
out = cmd('show config interfaces ge400-0/0/3.1 | no-more')
print(out)

print('\n=== Verify: ge400-0/0/33 config ===')
out = cmd('show config interfaces ge400-0/0/33 | no-more')
print(out)

print('\n=== Verify: routes ===')
out = cmd('show route | no-more')
print(out)

# Clear counters for baseline
cmd('clear int counters')

print('\n=== Baseline counters on ge400-0/0/3.1 ===')
out = cmd('show interfaces counters ge400-0/0/3.1 | no-more')
for line in out.split('\n'):
    l = line.strip()
    if any(w in l.lower() for w in ['urpf', 'drop', 'rx frames', 'rx broadcast', 'operational']):
        print(f'  {l}')

# Check warm restart command exists
print('\n=== Check warm restart command ===')
out = cmd('request system warm-restart ?')
print(out)

print('\n=== Check system status ===')
out = cmd('show system status | no-more')
print(out[:1000])

child.sendline('exit')
print('\nConfiguration complete.')
