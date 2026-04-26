#!/usr/bin/env python3
"""Cleanup any lingering Spirent sessions for this user."""
from stcrestclient import stchttp
stc = stchttp.StcHttp("il-auto-containers", port=80)
sessions = stc.sessions()
print("All sessions:", sessions)
for s in sessions:
    if 'sw244107' in s:
        print(f"Ending session {s}")
        try:
            stc.join_session(s)
            stc.end_session(s)
            print("  ended OK")
        except Exception as e:
            print(f"  error: {e}")
print("Remaining sessions:", stc.sessions())
