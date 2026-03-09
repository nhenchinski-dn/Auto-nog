# CFM rmep-failed – Runbook (NCP3 ↔ NCPL)

Already tried:
- [x] Remove l2-cross-connect on NCPL (ge100-0/0/70.100)
- [x] Remove bridge-domain on NCP3 (ge400-0/0/33.100)
- [x] MEP direction: one up, one down

If remote MEP is **still rmep-failed**, run these in order.

---

## 1. Remove QoS from CFM interface (NCP3)

QoS on the MEP interface can drop or mis-handle CFM (EtherType 0x8902).

**On NCP3-CFM-nog:**

```
configure
interfaces ge400-0/0/33.100
no qos policy Ingress_Child_Classify_Only direction in
exit
exit
commit and-exit
```

Wait 30–60 s, then check remote MEPs on NCP3. If still failed, restore the QoS and continue.

---

## 2. Test with auto-discovery

See if CCMs are arriving at all by allowing auto-discovery (no crosscheck).

**On NCP3:** under the same MA, temporarily:

```
configure
services ethernet-oam connectivity-fault-management
maintenance-domains MD-CUST
maintenance-associations MA-CUST
remote-meps
auto-discovery enabled
exit
exit
exit
exit
commit and-exit
```

**On NCPL:** same (enable auto-discovery under MA-CUST, exit remote-meps block first if needed).

Then:

```
show services ethernet-oam connectivity-fault-management maintenance-domains MD-CUST maintenance-associations MA-CUST local-mep 2 remote-meps
```

- If a remote MEP **appears with rmep-ok** (or similar), CCMs are flowing and the issue is likely **crosscheck mep-id** or static remote-mep config.
- If **no** remote MEP appears or still failed, CCMs are not being received (path or platform issue).

Revert to `auto-discovery disabled` and `crosscheck mep-id 1` / `2` when done.

---

## 3. Try CFM on unit .1 (no VLAN 100)

Rule out something specific to VLAN 100 or that child interface.

- Create **ge400-0/0/33.1** and **ge100-0/0/70.1** with `vlan-id 1` and `l2-service enabled` if not already present.
- **Move both MEPs** to the .1 interface (change `interface ge400-0/0/33.100` → `ge400-0/0/33.1` on NCP3, and same on NCPL to ge100-0/0/70.1).
- Commit on both, wait 30–60 s, check remote MEPs.

If it works on .1, the problem is likely VLAN 100 or that specific child-interface handling.

---

## 4. Verify from NCPL side

On **NCPL**, run:

```
show services ethernet-oam connectivity-fault-management maintenance-domains MD-CUST maintenance-associations MA-CUST local-mep 1 remote-meps
```

- If **NCPL** sees remote MEP 2 as **rmep-ok** but NCP3 still sees MEP 1 as failed → problem is **one-way** (NCPL→NCP3 path or NCP3 processing).
- If **both** sides show rmep-failed → CCMs not exchanged in either direction (link/VLAN/interface or platform).

---

## 5. Capture and escalate

If all above still show rmep-failed:

1. **Both devices:**  
   `show services ethernet-oam connectivity-fault-management` (full).  
   `show interfaces ge400-0/0/33.100` (NCP3) and `show interfaces ge100-0/0/70.100` (NCPL).

2. **NCP3:**  
   `show running-config interfaces ge400-0/0/33` (and .100).  
   `show running-config services ethernet-oam` (or equivalent).

3. **NCPL:**  
   Same for ge100-0/0/70 and services ethernet-oam.

4. Open a **bug** (e.g. DNOS/DriveNets):  
   - Two-node point-to-point, CFM on child .100, same MD/MA/level, crosscheck mep-id.  
   - CCMs never reach the peer (rmep-failed both sides or one-way).  
   - Attach config snippets and show output; mention that l2-cross-connect, bridge-domain, direction, and QoS were already tried.

---

## Quick reference – current setup

| Node   | Local MEP | Interface        | Remote MEP | MD/MA    | Level |
|--------|-----------|------------------|------------|----------|-------|
| NCP3   | 2         | ge400-0/0/33.100 | 1          | MD-CUST / MA-CUST | 7 |
| NCPL   | 1         | ge100-0/0/70.100 | 2          | MD-CUST / MA-CUST | 7 |

Link: **ge400-0/0/33** (NCP3) ↔ **ge100-0/0/70** (NCPL).
