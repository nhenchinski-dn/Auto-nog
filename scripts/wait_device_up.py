import subprocess, time

DEVICE = 'WKY1C7VD00008P2'
print(f'Waiting for {DEVICE} to come back online...')

for i in range(30):
    result = subprocess.run(
        ['sshpass', '-p', 'dnroot', 'ssh', '-o', 'StrictHostKeyChecking=no',
         '-o', 'ConnectTimeout=5', f'dnroot@{DEVICE}', 'show sys version | no-more'],
        capture_output=True, text=True, timeout=15
    )
    if result.returncode == 0 and 'DNOS' in result.stdout:
        print(f'\n[{i*15}s] Device is UP!')
        print(result.stdout[:500])
        break
    else:
        status = 'connecting...' if 'Connection refused' in result.stderr else 'unreachable'
        print(f'  [{i*15}s] {status}')
        time.sleep(15)
else:
    print('Device did not come back within 7.5 minutes.')
