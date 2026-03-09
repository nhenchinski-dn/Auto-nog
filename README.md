# Auto-nog — DNOS Test Automation

Automated test scripts for DriveNets DNOS features. Covers Y.1731/CFM, PIM/Multicast, QoS, BFD, LACP, CPRL, restart/HA, and more.

## Repository Structure

```
Auto-nog/
├── tests/               # Test scripts, organized by feature
│   ├── y1731/           # Y.1731 / CFM / Ethernet OAM (DM, SLM, on-demand, RESTCONF)
│   ├── multicast/       # PIM ASM/SSM, SPT switchover, mroute scale
│   ├── sanity/          # QoS, BFD, LACP, CPRL, interface dampening, transceiver
│   ├── sw211457/        # BGP/EVPN rollback-commit race (SW-211457, SW-241906)
│   └── restart/         # Warm/cold restart, GI-day validation
├── setup/               # Device config scripts (ISIS, PIM, sub-interfaces)
├── diag/                # Diagnostic/check scripts (check_*, diag_*, count_*)
├── lib/                 # Shared utilities (dnos_cli.py, nm_call.py)
├── docs/                # Runbooks, guides, checklists, fix summaries
├── output/              # Test logs, result JSON, CLI captures
├── scripts/             # Shell scripts (SSH helpers, wrappers)
├── reference/           # Fetched source code (CFM manager, Cheetah, YANGs)
└── README.md
```

## Quick Start

```bash
pip install paramiko
```

Run any test script directly:

```bash
python3 tests/y1731/y1731_cli_tab_test.py --host 192.168.1.10 --user dnroot --password dnroot
python3 tests/sanity/qos_sanity_test.py
python3 tests/multicast/test_asm_spt_sw242472.py
```

## Key Jira Tickets

| Ticket | Area | Scripts |
|--------|------|---------|
| SW-141523 | Y.1731 Proactive PM (epic) | `tests/y1731/verify_y1731_bugs.py`, `tests/y1731/y1731_restconf_test.py` |
| SW-237984 | Y.1731 on-demand stop | `tests/y1731/test_sw237984.py`, `tests/y1731/on_demand_stop_test.py` |
| SW-242472 | ASM SPT switchover | `tests/multicast/test_asm_spt_sw242472.py`, `tests/multicast/test_asm_spt_negative_sw242472.py` |
| SW-211457 | BGP/EVPN rollback race | `tests/sw211457/sw211457_test.py` |
| SW-246192 | RP failure at scale | `tests/multicast/test_rp_failure_scale.py` |

## Folder Details

### tests/y1731/
Y.1731 Ethernet OAM tests: CLI/TAB completion, RESTCONF, on-demand DM/SLM, proactive PM, CFM setup, bug verification.

### tests/multicast/
PIM multicast: ASM/SSM, SPT switchover, RP failure, 400G physical, mroute scale validation.

### tests/sanity/
Feature sanity checks: QoS policies, BFD over BGP, LACP, CPRL rate-limiting, interface dampening, transceiver validation.

### tests/sw211457/
Reproduction and verification scripts for the BGP/EVPN rollback-commit race condition.

### tests/restart/
Node restart tests: warm/cold NCP restart, restart suites, GI-day post-validation.

### setup/
Config-push scripts for ISIS, PIM, sub-interfaces. Used to prepare devices before test execution.

### diag/
Read-only diagnostic scripts for checking device state: PIM trees, mroute counts, interface IPs, ISIS adjacencies, core dumps.

### lib/
Shared Python utilities: SSH CLI helper (`dnos_cli.py`), network-mapper MCP caller (`nm_call.py`), connectivity checker.

### docs/
Documentation: test plans, runbooks, bug reports, fix summaries, Y.1731 guides.

### output/
Test execution artifacts: `.log` files, result `.json`, CLI captures, QoS summaries. Not intended for manual editing.

### reference/
Fetched source code for reference: CFM manager C++ sources, Cheetah YANG models, MIBs.
