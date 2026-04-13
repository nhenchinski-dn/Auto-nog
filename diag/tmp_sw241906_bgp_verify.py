#!/usr/bin/env python3
"""Run SW-241906 BGP error-message checks on a DNOS device (one SSH session)."""
import sys
import time
import re
import paramiko

HOST = sys.argv[1]
USER = "dnroot"
PWD = "dnroot"


def clean(s):
    s = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", s)
    s = re.sub(r"\r", "", s)
    s = re.sub(r"-- More -- \(Press q to quit\)\s*", "", s)
    return s


def recv_all(shell, idle_rounds=6):
    buf = ""
    idle = 0
    while idle < idle_rounds:
        if shell.recv_ready():
            buf += shell.recv(65536).decode("utf-8", errors="replace")
            idle = 0
        else:
            time.sleep(0.35)
            idle += 1
    return buf


def send(shell, cmd, pause=4):
    shell.send(cmd + "\n")
    time.sleep(pause)
    return clean(recv_all(shell))


def reset_candidate(shell):
    send(shell, "rollback", pause=3)
    send(shell, "top", pause=2)


def main():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        HOST,
        username=USER,
        password=PWD,
        look_for_keys=False,
        allow_agent=False,
        timeout=20,
    )
    shell = client.invoke_shell(width=250, height=5000)
    time.sleep(6)
    recv_all(shell, idle_rounds=10)

    log = []

    def run_case(name, cmds, expect_substrings):
        log.append(f"\n{'='*60}\nCASE: {name}\n{'='*60}")
        out = ""
        for c in cmds:
            out += send(shell, c, pause=3)
        log.append(out)
        low = out.lower()
        ok = any(x.lower() in low for x in expect_substrings)
        log.append(
            f"\n>>> PASS (saw expected text): {ok}\n"
            f">>> Expected any of: {expect_substrings!r}\n"
        )
        reset_candidate(shell)
        return ok, out

    results = {}

    send(shell, "configure", pause=2)

    # Case 3: purge-time <= stale-path-time
    results["purge_vs_stale"] = run_case(
        "Graceful-restart purge-time vs stalepath-time",
        [
            "protocols",
            "bgp 65241",
            "graceful-restart",
            "stalepath-time 500",
            "purge-time 400",
            "commit",
        ],
        [
            "purge-time must be greater than stale-path-time",
            "graceful restart purge-time must be greater than stale-path-time",
        ],
    )

    # Case 4: confederation id == BGP AS
    results["confed_id"] = run_case(
        "Confederation identifier == BGP AS",
        [
            "protocols",
            "bgp 65241",
            "confederation identifier 65241",
            "commit",
        ],
        ["confederation identifier cannot be the same as bgp as number"],
    )

    # Case 1: local-as == global AS (local-as at neighbor level; then activate an AF)
    results["local_as_global"] = run_case(
        "local-as same as BGP global AS",
        [
            "protocols",
            "bgp 65241",
            "neighbor 10.254.254.254",
            "remote-as 8888",
            "local-as 65241",
            "address-family ipv4-unicast",
            "commit",
        ],
        [
            "cannot have local-as same as bgp as number",
            "local as cannot be the same as the global as",
        ],
    )

    # Case 2 setup: valid prefix-sid-map + neighbor with remote-as + labeled-unicast
    log.append(f"\n{'='*60}\nCASE: prefix-sid-map (valid setup)\n{'='*60}")
    out_setup = ""
    for c in [
        "routing-policy",
        "bgp-prefix-sid-map SW241906_PMAP",
        "top",
        "protocols",
        "bgp 65241",
        "neighbor 10.254.254.253",
        "remote-as 8888",
        "address-family ipv4-labeled-unicast",
        "sr-labeled-unicast",
        "prefix-sid-map SW241906_PMAP in global-block-origination-in",
        "commit",
    ]:
        out_setup += send(shell, c, pause=4)
    log.append(out_setup)

    low_setup = out_setup.lower()
    setup_ok = "error:" not in low_setup.split("commit")[-1] if "commit" in low_setup else False
    # commit line should not show ERROR for success
    last = out_setup[out_setup.rfind("commit") :].lower()
    setup_ok = "error:" not in last
    log.append(f"\n>>> Setup commit clean: {setup_ok}\n")

    log.append(
        f"\n{'='*60}\nCASE: prefix-sid-map try orphan global-block (CLI)\n{'='*60}"
    )
    out_violate = ""
    for c in [
        "top",
        "protocols",
        "bgp 65241",
        "neighbor 10.254.254.253",
        "address-family ipv4-labeled-unicast",
        "sr-labeled-unicast",
        "no prefix-sid-map SW241906_PMAP in",
        "commit",
    ]:
        out_violate += send(shell, c, pause=4)
    log.append(out_violate)
    low = out_violate.lower()
    pm_ok = "global-block-origination-in requires bgp-prefix-sid-map-policy-in" in low
    log.append(f"\n>>> PASS (prefix-sid-map must message): {pm_ok}\n")
    results["prefix_sid_map"] = (pm_ok, out_violate)

    reset_candidate(shell)

    # Cleanup
    for c in [
        "protocols",
        "no bgp 65241",
        "top",
        "routing-policy",
        "no bgp-prefix-sid-map SW241906_PMAP",
        "commit",
    ]:
        send(shell, c, pause=4)

    shell.send("exit\n")
    time.sleep(1)
    client.close()

    print("\n".join(log))
    print("\n" + "#" * 60)
    print("SUMMARY")
    print("#" * 60)
    for k, (ok, _) in results.items():
        print(f"  {k}: {'PASS' if ok else 'FAIL'}")
    if not results["prefix_sid_map"][0]:
        print(
            "\nNOTE: prefix-sid-map YANG may need NETCONF to orphan global-block; "
            "CLI often removes both leaves together."
        )


if __name__ == "__main__":
    main()
