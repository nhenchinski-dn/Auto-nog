#!/usr/bin/env python3
"""Test MEP discovery logic with actual device config"""

import re
from typing import Dict, Set, Tuple, Optional, List

# Actual config from device
config_output = """
services
  ethernet-oam
    connectivity-fault-management
      maintenance-domains MD-CUST
        level 7
        md-name string customer-md
        maintenance-associations MA-CUST
          short-ma-name string customer-ma
          local-mep 2
            direction up
            interface ge400-0/0/24.100
          !
          remote-meps
            auto-discovery disabled
            crosscheck mep-id 1
          !
        !
      !
      maintenance-domains MD-CUST1
        level 3
        md-name string customer-md1
        maintenance-associations MA-CUST1
          short-ma-name string customer-ma1
          local-mep 4
            admin-state enabled
            direction down
            interface ge400-0/0/33.1
          !
          remote-meps
            crosscheck mep-id 3
          !
        !
      !
    !
  !
!
"""

# Discovery logic from y1731_cli_tab_test.py
direction_re = re.compile(r"\bdirection\s+(down|up)\b", flags=re.IGNORECASE)
md_re = re.compile(r"\bmaintenance[-_]domain(?:s)?(?:[-_]name)?\s+(\S+)", flags=re.IGNORECASE)
ma_re = re.compile(r"\bmaintenance[-_]association(?:s)?(?:[-_]name)?\s+(\S+)", flags=re.IGNORECASE)
mep_id_re = re.compile(r"\bmep[-_]id\s+(\d+)\b", flags=re.IGNORECASE)
local_mep_re = re.compile(r"\blocal[-_]mep\s+(\d+)\b", flags=re.IGNORECASE)
mep_re = re.compile(r"\bmep\s+(\d+)\b", flags=re.IGNORECASE)
remote_mep_re = re.compile(r"\bremote[-_]mep(?:s)?(?:[-_]id)?\s+(\d+)\b", flags=re.IGNORECASE)

candidates: Dict[Tuple[str, str], Dict[str, Set[int]]] = {}
current_md: Optional[str] = None
current_ma: Optional[str] = None

for line_num, line in enumerate(config_output.splitlines(), 1):
    md_m = md_re.search(line)
    if md_m:
        current_md = md_m.group(1)
        current_ma = None
        print(f"Line {line_num}: Found MD = {current_md}")
    
    ma_m = ma_re.search(line)
    if ma_m:
        current_ma = ma_m.group(1)
        print(f"Line {line_num}: Found MA = {current_ma}")
    
    line_md = md_m.group(1) if md_m else current_md
    line_ma = ma_m.group(1) if ma_m else current_ma
    
    if not (line_md and line_ma):
        continue
    
    key = (line_md, line_ma)
    if key not in candidates:
        candidates[key] = {"meps": set(), "remote_meps": set(), "direction": None}
    
    dir_m = direction_re.search(line)
    if dir_m:
        candidates[key]["direction"] = dir_m.group(1).lower()
        print(f"Line {line_num}: Direction = {dir_m.group(1)} for {key}")
    
    is_remote_line = (
        bool(remote_mep_re.search(line))
        or ("remote-mep" in line.lower())
        or ("remote_mep" in line.lower())
        or ("crosscheck" in line.lower())
    )
    
    for m in remote_mep_re.finditer(line):
        candidates[key]["remote_meps"].add(int(m.group(1)))
    
    if "crosscheck" in line.lower():
        for m in mep_id_re.finditer(line):
            candidates[key]["remote_meps"].add(int(m.group(1)))
            print(f"Line {line_num}: Remote MEP {m.group(1)} for {key}")
    
    if is_remote_line:
        continue
    
    for m in local_mep_re.finditer(line):
        candidates[key]["meps"].add(int(m.group(1)))
        print(f"Line {line_num}: LOCAL MEP {m.group(1)} for {key}")
    
    for m in mep_id_re.finditer(line):
        candidates[key]["meps"].add(int(m.group(1)))
    
    for m in mep_re.finditer(line):
        if "local-mep" not in line.lower() and "remote-mep" not in line.lower():
            candidates[key]["meps"].add(int(m.group(1)))

print("\n=== DISCOVERY RESULTS ===")
print(f"Found {len(candidates)} MD/MA pairs:")
for key in sorted(candidates.keys()):
    md, ma = key
    c = candidates[key]
    meps = sorted(c["meps"])
    remote_meps = sorted(c["remote_meps"])
    print(f"\n{md}/{ma}:")
    print(f"  Local MEPs: {meps}")
    print(f"  Remote MEPs: {remote_meps}")
    print(f"  Direction: {c.get('direction')}")

result_list: List[Tuple[str, str, str, Optional[str], Optional[str]]] = []
for key in sorted(candidates.keys()):
    md, ma = key
    c = candidates[key]
    meps = sorted(c["meps"])
    remote_meps = sorted(c["remote_meps"])
    target_str: Optional[str] = f"mep-id {remote_meps[0]}" if remote_meps else None
    direction = c.get("direction")
    for mep_id in meps:
        result_list.append((md, ma, str(mep_id), direction, target_str))

print(f"\n=== FINAL RESULT LIST ===")
print(f"Discovered {len(result_list)} local MEP(s):")
for md, ma, mep_id, direction, target in result_list:
    print(f"  MEP {mep_id}: {md}/{ma}, direction={direction}, target={target}")
