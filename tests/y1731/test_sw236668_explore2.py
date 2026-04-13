import paramiko
import time
import re
import json

DEVICE_IP = "100.64.3.184"
USERNAME = "dnroot"
PASSWORD = "dnroot"

def connect():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(DEVICE_IP, username=USERNAME, password=PASSWORD,
                timeout=30, look_for_keys=False, allow_agent=False)
    chan = ssh.invoke_shell(width=300)
    time.sleep(5)
    chan.recv(65535)
    return ssh, chan

def clean_ansi(text):
    return re.sub(r'\x1b\[[0-9;]*[a-zA-Z]|\r', '', text)

def send(chan, cmd, wait=5):
    chan.send(cmd + '\r')
    time.sleep(wait)
    out = b''
    while chan.recv_ready():
        out += chan.recv(65535)
    decoded = clean_ansi(out.decode(errors='replace'))
    if 'uncommitted changes' in decoded.lower():
        chan.send('no\r')
        time.sleep(3)
        extra = b''
        while chan.recv_ready():
            extra += chan.recv(65535)
        decoded += clean_ansi(extra.decode(errors='replace'))
    return decoded

def main():
    ssh, chan = connect()
    print("Connected.\n")

    # First clean up any leftover EXPLORE_PROFILE
    out = send(chan, 'configure', 5)
    out = send(chan, 'no services performance-monitoring profiles cfm two-way-delay-measurement EXPLORE_PROFILE', 5)
    print(f"Cleanup EXPLORE_PROFILE: {out}\n")
    out = send(chan, 'commit', 10)
    print(f"Commit cleanup: {out}\n")

    # Explore probes sub-options
    out = send(chan, 'services performance-monitoring profiles cfm two-way-delay-measurement TEST_PROFILE test-duration probes ?', 5)
    print(f"=== probes ? ===\n{out}\n")

    # Explore probes probe-count range
    out = send(chan, 'services performance-monitoring profiles cfm two-way-delay-measurement TEST_PROFILE test-duration probes probe-count ?', 5)
    print(f"=== probes probe-count ? ===\n{out}\n")

    # Explore probes probe-interval range
    out = send(chan, 'services performance-monitoring profiles cfm two-way-delay-measurement TEST_PROFILE test-duration probes probe-interval ?', 5)
    print(f"=== probes probe-interval ? ===\n{out}\n")

    # Explore probes repeat-interval range
    out = send(chan, 'services performance-monitoring profiles cfm two-way-delay-measurement TEST_PROFILE test-duration probes repeat-interval ?', 5)
    print(f"=== probes repeat-interval ? ===\n{out}\n")

    # Explore time-frame minutes range
    out = send(chan, 'services performance-monitoring profiles cfm two-way-delay-measurement TEST_PROFILE test-duration time-frame minutes ?', 5)
    print(f"=== time-frame minutes ? ===\n{out}\n")

    # Explore time-frame probe-interval range
    out = send(chan, 'services performance-monitoring profiles cfm two-way-delay-measurement TEST_PROFILE test-duration time-frame probe-interval ?', 5)
    print(f"=== time-frame probe-interval ? ===\n{out}\n")

    # Explore time-frame repeat-interval range
    out = send(chan, 'services performance-monitoring profiles cfm two-way-delay-measurement TEST_PROFILE test-duration time-frame repeat-interval ?', 5)
    print(f"=== time-frame repeat-interval ? ===\n{out}\n")

    # Explore non-stop computation-interval range
    out = send(chan, 'services performance-monitoring profiles cfm two-way-delay-measurement TEST_PROFILE test-duration non-stop computation-interval ?', 5)
    print(f"=== non-stop computation-interval ? ===\n{out}\n")

    # Explore non-stop probe-interval range
    out = send(chan, 'services performance-monitoring profiles cfm two-way-delay-measurement TEST_PROFILE test-duration non-stop probe-interval ?', 5)
    print(f"=== non-stop probe-interval ? ===\n{out}\n")

    # Try to configure probes with min value 1
    out = send(chan, 'services performance-monitoring profiles cfm two-way-delay-measurement TEST_PROFILE test-duration probes probe-count 1', 5)
    print(f"=== probes probe-count 1 ===\n{out}\n")

    # Try time-frame minutes 1
    out = send(chan, 'services performance-monitoring profiles cfm two-way-delay-measurement TEST_PROFILE test-duration time-frame minutes 1', 5)
    print(f"=== time-frame minutes 1 ===\n{out}\n")

    # Check the show command for profiles
    send(chan, 'end', 3)
    
    # Try various show commands
    for cmd in [
        'show services performance-monitoring cfm two-way-delay-measurement | no-more',
        'show services performance-monitoring | no-more',
    ]:
        out = send(chan, cmd, 8)
        print(f"=== {cmd} ===\n{out}\n")

    # Go back to config to rollback and clean up
    out = send(chan, 'configure', 5)
    out = send(chan, 'rollback', 5)
    print(f"Rollback: {out}\n")
    out = send(chan, 'no services performance-monitoring profiles cfm two-way-delay-measurement TEST_PROFILE', 5)
    out = send(chan, 'commit', 10)
    print(f"Cleanup commit: {out}\n")

    send(chan, 'end', 3)
    chan.close()
    ssh.close()
    print("Done.")

if __name__ == '__main__':
    main()
