# Y.1731 DM/SLM CLI and TAB Test

Automated CLI and TAB-completion tests for Y.1731 **Delay Measurement (DM)** and **Synthetic Loss Measurement (SLM)** on DriveNets devices. Covers profile/session configuration, commit validation, negative cases, and SW-235372 CLI coverage.

**Jira:** SW-235373 (DM CLI), SW-235927 (SLM CLI), SW-235372 (CLI coverage).

---

## Important: Your Config Is Preserved

This script **does not use `rollback 0`**. Discovery, validation, cleanup, and commit-check steps only remove the PM sessions/profiles they create. Your candidate config (e.g. `services ethernet-oam connectivity-fault-management`) is left intact.

**Device rollback:** On the device, `rollback 0` only rolls back the candidate (config you are about to commit). To revert older committed configs use `rollback 1`, `rollback 2`, etc. Use `show config committed` (sh con com) to see commit history.

---

## Prerequisites

- **Python 3** with `paramiko`:
  ```bash
  pip install paramiko
  ```
- **Device:** `services ethernet-oam connectivity-fault-management` configured with at least one Maintenance Domain, Maintenance Association, and local MEP (so the script can auto-discover MD/MA/MEP/target).
- **SSH** access from your machine to the device (port 22).

---

## Quick Start

Run with prompts for host, user, password, and cleanup:

```bash
python3 y1731_cli_tab_test.py
```

Or pass host and credentials:

```bash
python3 y1731_cli_tab_test.py --host 192.168.1.10 --user dnroot --password YOUR_PASSWORD
```

Keep the created config (no cleanup):

```bash
python3 y1731_cli_tab_test.py --host 192.168.1.10 --no-cleanup
```

---

## Options

| Option | Description |
|--------|-------------|
| `--host` | Device hostname or IP (prompted if omitted) |
| `--user` | SSH username (default: `dnroot`) |
| `--password` | SSH password (default: `dnroot`) |
| `--timeout` | Command timeout in seconds (default: 30) |
| `--session` | DM session name to create (default: `DM_CLI_TAB`) |
| `--profile` | DM profile name (default: `DM_PROF_CLI`) |
| `--slm-session` | SLM session name (default: `SLM_CLI_TAB`) |
| `--slm-profile` | SLM profile name (default: `SLM_PROF_CLI`) |
| `--slm-target` | SLM target, e.g. `mep-id 2` (default: same as DM target) |
| `--slm-pcp` | SLM PCP (default: 5) |
| `--auto-from-cfm` | Auto-discover MD/MA/MEP/target from CFM config (default: true) |
| `--no-auto-from-cfm` | Disable discovery; you will be prompted or use overrides |
| `--md` | Override maintenance-domain name |
| `--ma` | Override maintenance-association name |
| `--mep-id` | Override local MEP ID |
| `--target` | Override DM target, e.g. `mep-id 2` |
| `--description` | DM session description (default: `cli_tab_test`) |
| `--slm-description` | SLM session description |
| `--cleanup` | Remove created DM/SLM config at end |
| `--no-cleanup` | Do not remove created config (prompted if both omitted) |
| `--show-details` | Print per-step details (errors/notes) |
| `--show-progress` | Print `RUNNING: <test>` before each test |
| `--show-cli-output` | Print raw CLI output from device |
| `--output-file FILE` | Write raw CLI output to a file |
| `--output-format table\|lines` | Summary format: table (default) or line-by-line |

---

## What It Tests

- **Discovery:** Reads MD/MA/MEP/target from `show config services ethernet-oam connectivity-fault-management` (or prompts).
- **DM:** Profile and session create/commit, TAB completion, thresholds, test-duration (probes / time-frame / non-stop), admin-state, description, source/target (mep-id and mac-address).
- **SLM:** Same style for SLM profiles and sessions.
- **SW-235372:** CLI coverage for profile knobs and session variants (commit check + teardown, no rollback).
- **Negative:** Long names/descriptions, invalid MD/MA, non-numeric mep-id/target; expects command or commit-check failure.
- **Cleanup:** Removes only the script-created DM/SLM sessions and profiles, then commits (no `rollback 0`).

---

## Output

- Default: PASS/FAIL summary table (DM, SLM, Other).
- `--show-details`: per-step details and errors.
- `--output-format lines`: line-by-line results instead of tables.
- `--output-file`: full raw CLI output saved to the given file.

---

## License

Use according to your project’s policy.
