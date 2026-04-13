#!/usr/bin/env python3
"""Capture SNMP traps on port 19162 for 60 seconds and decode them."""
import socket, time, struct

PORT = 19162
DURATION = 60

print(f"Listening for SNMP traps on UDP port {PORT} for {DURATION}s...")
print(f"(Trap server on device sends to 10.10.72.148:9162 - checking if we")
print(f" can also receive on a different port. If not, will read terminal.)")
print()

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    sock.bind(('0.0.0.0', PORT))
except OSError as e:
    print(f"Cannot bind to port {PORT}: {e}")
    print("Will read existing trap data from terminal instead.")
    sock.close()
    exit(1)

sock.settimeout(3)
count = 0
end_time = time.time() + DURATION

while time.time() < end_time:
    try:
        data, addr = sock.recvfrom(65536)
        count += 1
        print(f"[{time.strftime('%H:%M:%S')}] Trap #{count} from {addr[0]}:{addr[1]} ({len(data)} bytes)")
    except socket.timeout:
        remaining = int(end_time - time.time())
        if remaining > 0 and remaining % 10 == 0:
            print(f"  ... waiting ({remaining}s remaining, {count} traps so far)")

sock.close()
print(f"\nTotal traps received: {count}")
