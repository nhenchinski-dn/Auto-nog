# SW-236664 ETH-DM Initiator – Manual Test Runbook

Replace `<MD>`, `<MA>`, `<IF_DUT>`, `<IF_PEER>`, `<PROFILE>`, `<SESSION_UC>`, `<SESSION_MC>` with your values (e.g. MD-CUST, MA-CUST, eth0, DM_PROF, DM_UC, DM_MC).

---

## Commands in order (run top to bottom)

**Configure (DUT):**

1. `configure`
2. `services ethernet-oam connectivity-fault-management maintenance-domains <MD> maintenance-associations <MA>`
3. `services ethernet-oam connectivity-fault-management maintenance-domains <MD> maintenance-associations <MA> local-mep 1 interface <IF_DUT>`
4. *(On peer device, if different:)* `services ethernet-oam connectivity-fault-management maintenance-domains <MD> maintenance-associations <MA> local-mep 2 interface <IF_PEER>`
5. `services performance-monitoring profiles cfm two-way-delay-measurement <PROFILE>`
6. `inform-test-results enabled`
7. `test-duration probes probe-count 5 probe-interval 1 repeat-interval 10`
8. `thresholds delay-rtt-min 100`
9. `thresholds delay-rtt-avg 1000`
10. `thresholds delay-rtt-max 2000`
11. `thresholds jitter-rtt-avg 500`
12. `thresholds jitter-rtt-max 1000`
13. `thresholds success-rate 90`
14. `exit`
15. `exit`
16. `exit`
17. `exit`
18. `exit`
19. `services performance-monitoring cfm two-way-delay-measurement <SESSION_UC>`
20. `profile <PROFILE>`
21. `admin-state enabled`
22. `description DM_UC_mep_target`
23. `source maintenance-domain <MD> maintenance-association <MA> mep-id 1`
24. `target mep-id 2`
25. `exit`
26. `exit`
27. `exit`
28. `exit`
29. `exit`
30. `services performance-monitoring cfm two-way-delay-measurement <SESSION_MC>`
31. `profile <PROFILE>`
32. `admin-state enabled`
33. `description DM_MC_multicast`
34. `source maintenance-domain <MD> maintenance-association <MA> mep-id 1`
35. `target mac-address 01:80:C2:00:00:3F`
36. `exit`
37. `exit`
38. `exit`
39. `exit`
40. `exit`
41. `commit check`
42. `commit`
43. `exit`

**Operational (run on-demand + verify):**

44. `run ethernet-oam cfm on-demand delay-measurement two-way maintenance-domain <MD> maintenance-association <MA> target mep-id 2`
45. `show services performance-monitoring cfm tests proactive two-way-delay-measurement session-name <SESSION_UC> detail`
46. `run ethernet-oam cfm on-demand delay-measurement two-way maintenance-domain <MD> maintenance-association <MA> target mac-address 01:80:C2:00:00:3F`
47. `show services performance-monitoring cfm tests proactive two-way-delay-measurement session-name <SESSION_MC> detail`

**Cleanup (when done):**

48. `configure`
49. `no services performance-monitoring cfm two-way-delay-measurement <SESSION_UC>`
50. `no services performance-monitoring cfm two-way-delay-measurement <SESSION_MC>`
51. `no services performance-monitoring profiles cfm two-way-delay-measurement <PROFILE>`
52. `commit`
53. `exit`

---

## Test Steps Overview

1. Configure MEPs and DM profile.
2. Configure DM_UC (target mep-id 2) and DM_MC (target multicast MAC).
3. Run DM_UC and verify DMM/DMR and all seven metrics.
4. Run DM_MC and verify valid results.
5. (Optional) Variants: one-way 1DM, PCP 0/7.
6. (Optional) Negative: unreachable MAC → no fake DMRs.

---

## Step 1 – Configure MEPs and DM profile

### 1.1 Enter configure mode

```
configure
```

### 1.2 CFM: Maintenance domain and association (if not already present)

```
services ethernet-oam connectivity-fault-management maintenance-domains <MD> maintenance-associations <MA>
```

(Exit to config if you enter submode; adjust to your CLI hierarchy.)

### 1.3 MEP on DUT (local-mep 1)

```
services ethernet-oam connectivity-fault-management maintenance-domains <MD> maintenance-associations <MA> local-mep 1 interface <IF_DUT>
```

### 1.4 MEP on peer (local-mep 2)

```
services ethernet-oam connectivity-fault-management maintenance-domains <MD> maintenance-associations <MA> local-mep 2 interface <IF_PEER>
```

*(If DUT and peer are different devices, run the local-mep 2 command on the peer device.)*

### 1.5 DM profile (two-way delay measurement)

```
services performance-monitoring profiles cfm two-way-delay-measurement <PROFILE>
inform-test-results enabled
test-duration probes probe-count 5 probe-interval 1 repeat-interval 10
thresholds delay-rtt-min 100
thresholds delay-rtt-avg 1000
thresholds delay-rtt-max 2000
thresholds jitter-rtt-avg 500
thresholds jitter-rtt-max 1000
thresholds success-rate 90
exit
```

Then exit back to top-level configure (multiple `exit` until you see `(config)#`):

```
exit
exit
exit
exit
```

---

## Step 2 – Configure DM_UC and DM_MC sessions

### 2.1 DM_UC (unicast – target mep-id 2)

```
services performance-monitoring cfm two-way-delay-measurement <SESSION_UC>
profile <PROFILE>
admin-state enabled
description DM_UC_mep_target
source maintenance-domain <MD> maintenance-association <MA> mep-id 1
target mep-id 2
exit
```

Then exit to top-level configure:

```
exit
exit
exit
exit
```

### 2.2 DM_MC (multicast – target multicast MAC)

```
services performance-monitoring cfm two-way-delay-measurement <SESSION_MC>
profile <PROFILE>
admin-state enabled
description DM_MC_multicast
source maintenance-domain <MD> maintenance-association <MA> mep-id 1
target mac-address 01:80:C2:00:00:3F
exit
```

Then exit to top-level configure:

```
exit
exit
exit
exit
```

### 2.3 Commit

```
commit check
commit
exit
```

---

## Step 3 – Run DM_UC and verify DMM/DMR and all seven metrics

### 3.1 Run on-demand delay measurement (trigger DMM/DMR) – DM_UC

From **operational** mode (not configure). This triggers the DMM/DMR exchange for target mep-id 2:

```
run ethernet-oam cfm on-demand delay-measurement two-way maintenance-domain <MD> maintenance-association <MA> target mep-id 2
```

*(If you are in configure mode, type `exit` until you see the operational prompt, then run the command above.)*

### 3.2 Show proactive DM session (detail) – DM_UC

```
show services performance-monitoring cfm tests proactive two-way-delay-measurement session-name <SESSION_UC> detail
```

*(For on-demand results, check if your image has e.g. `show services performance-monitoring cfm tests on-demand ...` and use that after the run command.)*

### 3.3 Pass criteria for DM_UC

- DM_UC present with correct **MD / MA / MEP / PCP**.
- **All seven metrics** present and **non-zero**:
  - Frame Delay two-way
  - Frame Delay one-way
  - Frame Delay one-way_far
  - Frame Delay one-way_near
  - Jitter two-way
  - Jitter one-way_far
  - Jitter one-way_near
- DMM/DMR exchange successful (no errors in show/output).

---

## Step 4 – Run DM_MC and verify valid results

### 4.1 Run on-demand delay measurement (multicast target) – DM_MC

From **operational** mode:

```
run ethernet-oam cfm on-demand delay-measurement two-way maintenance-domain <MD> maintenance-association <MA> target mac-address 01:80:C2:00:00:3F
```

### 4.2 Show proactive DM session (detail) – DM_MC

```
show services performance-monitoring cfm tests proactive two-way-delay-measurement session-name <SESSION_MC> detail
```

### 4.3 Pass criteria for DM_MC

- DM_MC present with correct **MD / MA / MEP / PCP**.
- Valid results (metrics populated as expected for multicast target `01:80:C2:00:00:3F`).

---

## Run command (on-demand DMM/DMR)

From **operational** mode (not inside `configure`):

**Unicast (mep-id 2):**
```
run ethernet-oam cfm on-demand delay-measurement two-way maintenance-domain <MD> maintenance-association <MA> target mep-id 2
```

**Multicast (01:80:C2:00:00:3F):**
```
run ethernet-oam cfm on-demand delay-measurement two-way maintenance-domain <MD> maintenance-association <MA> target mac-address 01:80:C2:00:00:3F
```

Replace `<MD>` and `<MA>` with your names (e.g. MD-CUST, MA-CUST).

---

## Show commands legend

**Proactive two-way delay – detail (per session):**

```
show services performance-monitoring cfm tests proactive two-way-delay-measurement session-name <SESSION> detail
```

Use `<SESSION_UC>` or `<SESSION_MC>` for the session name.

**Other useful shows:**

```
show config services performance-monitoring
show services performance-monitoring cfm tests proactive
```

---

## Variants (optional)

- **One-way 1DM:** Configure/run one-way delay measurement if supported (CLI may differ; use TAB/completion).
- **PCP 0 and PCP 7:** Create profiles or sessions with `pcp 0` and `pcp 7` (if supported under profile or session) and repeat verification.

---

## Negative testing – Unreachable MAC

- Configure a DM session with **target mac-address** set to an **unreachable MAC**.
- Run/show proactive and on-demand results.
- **Pass criteria:** No fake “DMR received” lines; stats should reflect no responses (e.g. zero or N/A), not fabricated values.

---

## Cleanup

```
configure
no services performance-monitoring cfm two-way-delay-measurement <SESSION_UC>
no services performance-monitoring cfm two-way-delay-measurement <SESSION_MC>
no services performance-monitoring profiles cfm two-way-delay-measurement <PROFILE>
commit
exit
```

---

## Command reference (legends)

| Item | Command / value |
|------|------------------|
| MEP (DUT) | `services ethernet-oam connectivity-fault-management maintenance-domains <MD> maintenance-associations <MA> local-mep 1 interface <IF_DUT>` |
| MEP (peer) | `services ethernet-oam connectivity-fault-management maintenance-domains <MD> maintenance-associations <MA> local-mep 2 interface <IF_PEER>` |
| DM profile | `services performance-monitoring profiles cfm two-way-delay-measurement <PROFILE> ...` |
| DM_UC | `services performance-monitoring cfm two-way-delay-measurement <SESSION_UC> ... target mep-id 2 ...` |
| DM_MC | `services performance-monitoring cfm two-way-delay-measurement <SESSION_MC> ... target mac-address 01:80:C2:00:00:3F ...` |
| **Run on-demand DM (UC)** | `run ethernet-oam cfm on-demand delay-measurement two-way maintenance-domain <MD> maintenance-association <MA> target mep-id 2` |
| **Run on-demand DM (MC)** | `run ethernet-oam cfm on-demand delay-measurement two-way maintenance-domain <MD> maintenance-association <MA> target mac-address 01:80:C2:00:00:3F` |

Example names: `<PROFILE>` = DM_PROF, `<SESSION_UC>` = DM_UC, `<SESSION_MC>` = DM_MC. For run: use your MD/MA (e.g. MD-CUST, MA-CUST).
