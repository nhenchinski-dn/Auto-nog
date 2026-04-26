#!/usr/bin/env python3
"""Poll DUT until DNOS CLI is back, then exit 0. Else exit 1 after timeout."""
import paramiko, time, re, sys

HOST = "100.64.8.59"

def connect_once():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(HOST, username="dnroot", password="dnroot", timeout=15,
                    look_for_keys=False, allow_agent=False)
    except Exception as e:
        return None, None, str(e)
    chan = ssh.invoke_shell(width=300, height=5000)
    time.sleep(8)
    banner = chan.recv(65535).decode(errors="replace")
    banner = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", banner).replace("\r", "")
    return ssh, chan, banner


def send(chan, cmd, wait=5):
    chan.send(cmd + "\n")
    time.sleep(wait)
    o = b""
    while chan.recv_ready():
        o += chan.recv(65535); time.sleep(0.3)
    return re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", o.decode(errors="replace")).replace("\r", "")


def check():
    ssh, chan, banner = connect_once()
    if ssh is None:
        return "ssh-fail", banner
    try:
        if "NCP3-nog" in banner or "DRIVENETS CLI Loading" in banner:
            # try a show command
            try:
                o = send(chan, "show system | no-more", 8)
                return "dnos-up", o[:500]
            except Exception as e:
                return "dnos-error", str(e)
        if "GI CLI" in banner or "GI#" in banner:
            o = send(chan, "show system", 5)
            m = re.search(r"System status:\s*(\S+)", o)
            status = m.group(1) if m else "unknown"
            return f"gi-cli ({status})", o[:400]
        return "unknown-prompt", banner[-300:]
    finally:
        try:
            chan.close(); ssh.close()
        except Exception:
            pass


if __name__ == "__main__":
    state, detail = check()
    print(f"state: {state}")
    print(detail)
    sys.exit(0 if state == "dnos-up" else 2)
