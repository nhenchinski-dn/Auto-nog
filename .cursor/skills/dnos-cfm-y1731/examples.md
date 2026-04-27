# DNOS CFM / Y.1731 — Worked Examples

All configurations below have been seen working in the lab (matching `WKY1C7VD00008P2` config-backup or `Auto-nog/tests/y1731` scripts).

---

## 1. Minimal Down-MEP between two DNOS devices (bridge-domain)

This is the canonical "CFM is up between two boxes" baseline. PE-A side (`mep-id 2`):

```
configure
  interfaces ge400-0/0/33.1
    admin-state enabled
    vlan-id 1
    l2-service enabled
  exit
  network-services
    bridge-domain
      instance bd-cfm
        interface ge400-0/0/33.1
      exit
    exit
  exit
  services ethernet-oam connectivity-fault-management
    maintenance-domains MD-CUST
      level 1
      md-name string md-cust
      maintenance-associations MA-CUST
        short-ma-name string ma-cust
        local-mep 2
          admin-state enabled
          direction down
          interface ge400-0/0/33.1
        exit
        remote-meps
          crosscheck mep-id 1
        exit
      exit
    exit
  exit
commit and-exit
```

PE-B side: identical except `local-mep 1` and `crosscheck mep-id 2`.

Health check from either side:

```
show services ethernet-oam connectivity-fault-management summary
show services ethernet-oam connectivity-fault-management maintenance-domains MD-CUST maintenance-associations MA-CUST meps all
```

Expected after ~3 × CCM interval: `Operational MEPs: 1/1`, remote MEP state = `rmep-ok`, no defects.

---

## 2. Down-MEP over an L2 cross-connect

If the deployment is point-to-point (not a real bridge-domain), use `l2-cross-connect`:

```
services
  ethernet-oam
    connectivity-fault-management
      maintenance-domains MD-CUST
        level 1
        md-name string md-cust
        maintenance-associations MA-CUST
          short-ma-name string ma-cust
          local-mep 2
            admin-state enabled
            direction down
            interface ge400-0/0/33.1
          remote-meps
            crosscheck mep-id 1
  l2-cross-connect cfmtest
    admin-state enabled
    interfaces ge400-0/0/33.1 ge400-0/0/22
```

The CFM sub-interface is one leg of the xc; the other leg is the user-facing port toward the CE.

---

## 3. Up-MEP on a multi-VLAN sub-interface (Q-in-Q)

Up-MEP is the PE-PE service-monitoring orientation. On a Q-in-Q sub-interface you **must** define `l2-originated-vlan-tags` or the OAMP will ship DMR/SLR/LBR untagged:

```
interfaces ge400-0/0/4.100
  admin-state enabled
  vlan-id 100
  l2-service enabled
  l2-originated-vlan-tags
    outer-tag 350 outer-tpid 0x8100
    inner-tag 200 inner-tpid 0x8100

services ethernet-oam connectivity-fault-management
  maintenance-domains PE-PE
    level 5
    md-name string pepe
    maintenance-associations SVC-100
      short-ma-name vlan-id 100
      local-mep 10
        admin-state enabled
        direction up
        interface ge400-0/0/4.100
      remote-meps
        crosscheck mep-id 11
```

Verify VLAN tag config: `show interfaces ge400-0/0/4.100` shows `L2 originated VLAN tags: outer: 350 (0x8100), inner: 200`. The `m` flag appears in `show interfaces` summary (legend: "VLAN manipulation or L2-originated-vlans configuration is configured").

---

## 4. ICC-based MEG-ID (Y.1731 interop)

Required for ITU Y.1731 MEG identification (e.g. multi-vendor carriers). MD name **must** be `null`:

```
services ethernet-oam connectivity-fault-management
  maintenance-domains MyFirstMD
    md-name null
    maintenance-associations MA1
      short-ma-name icc-based DTAG msa1234
      local-mep 1
        admin-state enabled
        direction down
        interface ge100-0/0/0
      remote-meps
        crosscheck mep-id 2
```

ICC: 1–6 alphanumeric chars; UMC: 7–12 chars. Combined ICC+UMC always padded to 13 chars internally.

---

## 5. CCM tuning + fault alarm

```
maintenance-associations MA-FAST
  short-ma-name string fast
  continuity-check
    transmit enabled
    interval 100ms
    loss-threshold 5
    fault-alarm
      lowest-priority-defect mac-remote-error-xcon
      defect-delay 2500
      clear-delay 10000
```

`lowest-priority-defect` values: `all-def, mac-remote-error-xcon, remote-error-xcon, error-xcon, xcon, no-xcon`. `defect-delay` (FNG alarm timer) and `clear-delay` (FNG reset timer) are both 2500–10000 ms.

---

## 6. Auto-discovery of remote MEPs

```
maintenance-associations MA-AUTO
  short-ma-name string auto
  remote-meps
    auto-discovery enabled
  local-mep 1
    interface ge400-0/0/4
    direction down
```

Mutually exclusive with `crosscheck mep-id` for the same MA. Global cap: `services ethernet-oam connectivity-fault-management global-config maximum-auto 2048` (default), syslog at 75% of cap (`maximum-auto-syslog-threshold`).

---

## 7. MIP (LBM/LTM responder only)

```
maintenance-associations MA-CUST
  short-ma-name string ma-cust
  mip mip-1
    interface ge400-0/0/4
    admin-state enabled
```

MIPs respond to LBM and LTM only — no CCM, no PM. CPU-based path (not OAMP), so per-MIP rates are lower than per-MEP.

---

## 8. On-demand ETH-DM (DMM → DMR), two-way

```
run ethernet-oam cfm on-demand delay-measurement two-way
    maintenance-domain MD-CUST
    maintenance-association MA-CUST
    target mep-id 1
    interval 1
    count 10
    detail
```

Output reports min/avg/max RTT, jitter (avg variation), max variation. With `detail`, per-reply delays are also displayed. Source MAC is NOT shown in detail output — BCM API limitation.

To target a peer that hasn't been discovered yet, use `target mac-address XX:XX:XX:XX:XX:XX` instead of `target mep-id`.

Valid `interval` values: `1, 10, 60, 600` (seconds). `count`: 1–65535.

PCP cannot be configured for DM (BCM OAMP limitation, CS00012355451).

---

## 9. On-demand ETH-SLM (SLM → SLR)

```
run ethernet-oam cfm on-demand synthetic-loss-measurement
    maintenance-domain MD-CUST
    maintenance-association MA-CUST
    target mep-id 1
    interval 1
    count 100
    pcp 5
```

Report contains: SLM TX, SLR RX, missing SLR, near-end loss count + %, far-end loss count + %.

Late SLR frames (>5 s after test end) are discarded per ITU-T G.8021.

Only ONE active ETH-SLM session per MEP. The system rejects a second initiator with: `Cannot initiate ETH-SL test, another test is already in progress.`

---

## 10. Proactive PM (scheduled DM and SLM)

Provision the profile + session under `services performance-monitoring`:

```
configure
  services performance-monitoring
    profiles cfm
      two-way-delay-measurement DM_PROF
        test-duration probes probe-count 10 probe-interval 1 repeat-interval 11
      exit
      two-way-synthetic-loss-measurement SLM_PROF
        test-duration probes probe-count 100 probe-interval 1 repeat-interval 11
      exit
    exit
    cfm
      two-way-delay-measurement DM_SESSION_1
        admin-state enabled
        profile DM_PROF
        source maintenance-domain MD-CUST maintenance-association MA-CUST mep-id 2
        target mep-id 1
      exit
      two-way-synthetic-loss-measurement SLM_SESSION_1
        admin-state enabled
        profile SLM_PROF
        source maintenance-domain MD-CUST maintenance-association MA-CUST mep-id 2
        target mep-id 1
      exit
    exit
  exit
commit and-exit
```

Show results:

```
show services performance-monitoring cfm tests proactive two-way-delay session-name DM_SESSION_1 detail
show services performance-monitoring cfm tests proactive two-way-synthetic-loss session-name SLM_SESSION_1 detail
```

Each session has a numeric `Session ID:` shown in detail output — useful for tests that delete + re-add and want to assert the ID changed.

---

## 11. Loopback (LBM/LBR) and Linktrace (LTM/LTR)

On-demand:

```
run ethernet-oam cfm on-demand loopback
    maintenance-domain MD-CUST maintenance-association MA-CUST
    target mep-id 1
    count 5
    pdu-size 64

run ethernet-oam cfm on-demand linktrace
    maintenance-domain MD-CUST maintenance-association MA-CUST
    target mep-id 1
    ttl 64
```

LB/LT results are stored under `performance-monitoring/cfm-tests/on-demand-tests` and visible per-MEP in counter rows (LBM/LBR/LTM/LTR sent/received).

---

## 12. Clear statistics

Scoped variants:

```
clear services ethernet-oam connectivity-fault-management statistics ge400-0/0/33.1
clear services ethernet-oam connectivity-fault-management statistics maintenance-domain MD-CUST
clear services ethernet-oam connectivity-fault-management statistics maintenance-domain MD-CUST maintenance-association MA-CUST
clear services ethernet-oam connectivity-fault-management statistics maintenance-domain MD-CUST maintenance-association MA-CUST mep-id 2
clear services ethernet-oam connectivity-fault-management statistics all
```

Minimum role: `operator`.

Known watch-out (W10 in the SW-216153 weakness inventory): on legacy code the per-MEP `ClearStats()` zeroed the redis-key fields (`hw_id/md_id/ma_id/mep_id`) — re-check that subsequent counter increments still land on the right per-MEP row after a clear.

---

## 13. Spirent Y.1731 PM emulation (responder testing)

For testing the DMR/SLR responder, emulate a Cisco-style DMM via Spirent or scapy. The PR #92176 dev test sends:

```
[CFM hdr (level=N, version=0, opcode=DMM=47, flags=0, first_tlv_offset=32)]
[tx_timestampf (8B, 1588 format) | rx_timestampf=0 (8B) | tx_timestampb=0 (8B) | rx_timestampb=0 (8B)]
[Test ID TLV (type=36, len=4, test_id=42)]
[Data TLV (type=3, len=N, value=...)]
[End TLV (type=0)]
```

Validation on the wire:
- DMR opcode = 46
- DA/SA swapped (DMR DA = original DMM SA; DMR SA = MEP unicast MAC)
- `rx_timestampf` = HW DMM RX timestamp (non-zero)
- `tx_timestampb` = HW DMR TX timestamp (`> rx_timestampf`)
- `tx_timestampf` echoed verbatim from DMM
- `res_rx_timestampb == 0`
- VLAN tag stack of DMR matches DMM exactly
- TLVs preserved bit-exact (SW responder PR #92176 only — HW responder did not preserve optional TLVs)

See `Auto-nog/tests/y1731/cfm_between_machines.py` for a working `OTG / snappi` and `LabServer REST` traffic harness that wraps this.

---

## 14. Auto-nog Python test pattern (paramiko)

Recurring idiom across `Auto-nog/tests/y1731`:

```python
import paramiko, time, re

def connect(ip, user="dnroot", pw="dnroot"):
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(ip, username=user, password=pw, timeout=30,
                look_for_keys=False, allow_agent=False)
    chan = ssh.invoke_shell(width=400)
    time.sleep(5)
    chan.recv(65535)
    return ssh, chan

def clean_ansi(text):
    return re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)

def send(chan, cmd, wait=5):
    chan.send(cmd + '\r')
    time.sleep(wait)
    out = b''
    while chan.recv_ready():
        out += chan.recv(65535)
    return clean_ansi(out.decode(errors='replace'))

def run_show(chan, cmd, wait=10):
    return send(chan, cmd + ' | no-more', wait)

def configure_commit(chan, cmds, wait_commit=20):
    send(chan, 'configure', 5)
    for c in cmds:
        send(chan, c, 2)
    out = send(chan, 'commit', wait_commit)
    send(chan, 'end', 3)
    return out
```

Key conventions in the test corpus:
- Always pipe show commands through `| no-more` to defeat pagination.
- Prepend `end` (or `top`) before each new test sequence to escape any leftover config-mode state.
- Detect commit failure with `re.search(r"commit\s+failed|error|invalid|unknown\s+command|validation\s+failed|TRANSACTION_COMMIT|commit\s+check\s+failed", out, re.IGNORECASE)`.
- For 100ms-class CCM intervals, sleep ≥ 3 × interval before sampling counters.
- The "out-of-sync" commit prompt (`This is current configuration which is out-of-sync … (yes,no,diff,clear,abort) [no]:`) often appears on shared lab devices — answer `yes` to commit on top of background drift, or `clear` to discard.

---

## 15. Two-device CFM bring-up via the `cfm_between_machines.py` helper

`Auto-nog/tests/y1731/cfm_between_machines.py` automates the whole two-device flow: enables LLDP if needed, finds the link via LLDP neighbors, creates the L2 sub-interface + bridge-domain, applies symmetric CFM config, and (optionally) validates with Spirent OTG/snappi or LabServer REST.

```bash
python3 cfm_between_machines.py --host-a 100.64.X.A --host-b 100.64.X.B
# or skip discovery:
python3 cfm_between_machines.py --host-a A --host-b B \
    --iface-a ge400-0/0/33 --iface-b ge100-0/0/70 \
    --md-name CFM-MD --ma-name CFM-MA --level 7 \
    --mep-a 1 --mep-b 2 --bridge-domain bd-cfm
```

Defaults: `--user dnroot --password dnroot --md-name CFM-MD --ma-name CFM-MA --level 7 --mep-a 1 --mep-b 2 --vlan-id 1 --bridge-domain bd-cfm`. Pass `--bridge-domain ''` to skip bridge-domain attachment if the topology uses an l2-cross-connect or VPN service instead.
