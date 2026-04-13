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

    results = {}

    # Enter config mode
    out = send(chan, 'configure', 5)
    print(f"=== configure ===\n{out}\n")

    # Explore test-duration options under a profile
    out = send(chan, 'services performance-monitoring profiles cfm two-way-delay-measurement EXPLORE_PROFILE test-duration ?', 5)
    print(f"=== test-duration ? ===\n{out}\n")
    results['test-duration-help'] = out

    # Explore count options
    out = send(chan, 'services performance-monitoring profiles cfm two-way-delay-measurement EXPLORE_PROFILE test-duration count ?', 5)
    print(f"=== test-duration count ? ===\n{out}\n")
    results['count-help'] = out

    # Explore time-frame options
    out = send(chan, 'services performance-monitoring profiles cfm two-way-delay-measurement EXPLORE_PROFILE test-duration time-frame ?', 5)
    print(f"=== test-duration time-frame ? ===\n{out}\n")
    results['time-frame-help'] = out

    # Explore non-stop options
    out = send(chan, 'services performance-monitoring profiles cfm two-way-delay-measurement EXPLORE_PROFILE test-duration non-stop ?', 5)
    print(f"=== test-duration non-stop ? ===\n{out}\n")
    results['non-stop-help'] = out

    # Also check the overall profile options
    out = send(chan, 'services performance-monitoring profiles cfm two-way-delay-measurement EXPLORE_PROFILE ?', 5)
    print(f"=== profile options ? ===\n{out}\n")
    results['profile-options'] = out

    # Check if there are SLM profiles too (for completeness)
    out = send(chan, 'services performance-monitoring profiles cfm two-way-synthetic-loss-measurement EXPLORE_SLM test-duration ?', 5)
    print(f"=== SLM test-duration ? ===\n{out}\n")
    results['slm-test-duration'] = out

    # Check existing profiles
    send(chan, 'end', 3)
    out = send(chan, 'show services performance-monitoring cfm profiles | no-more', 8)
    print(f"=== show profiles ===\n{out}\n")
    results['show-profiles'] = out

    # Try the show command from the ticket
    out = send(chan, 'show services performance-monitoring cfm profiles two-way-delay-measurement | no-more', 8)
    print(f"=== show DM profiles ===\n{out}\n")
    results['show-dm-profiles'] = out

    # Exit
    send(chan, 'end', 3)
    chan.close()
    ssh.close()

    with open('/home/dn/sw236668_explore.json', 'w') as f:
        json.dump(results, f, indent=2)
    print("Done. Results saved to sw236668_explore.json")

if __name__ == '__main__':
    main()
