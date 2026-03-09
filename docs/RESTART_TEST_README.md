# Node Restart Test Script

This script tests warm and cold restart scenarios for a node with different configurations.

## Test Scenarios

1. **Warm restart with empty config** (10 iterations) - Baseline N
2. **Warm restart with normal config** (3 iterations) - Compare with N
3. **Warm restart with scale config** (3 iterations) - Compare with N
4. **Cold restart with empty config** (10 iterations) - Baseline M
5. **Cold restart with normal config** (3 iterations) - Compare with M
6. **Cold restart with scale config** (3 iterations) - Compare with M

## Pass Criteria

- ✓ OS reboot occurred (verified via console)
- ✓ Configuration remains unchanged
- ✓ Restart time doesn't change drastically (within 30% threshold)
- ✓ Node completes full restart cycle

## Usage

### Local Execution
```bash
./restart_test.py
```

### Remote Execution via SSH
```bash
./restart_test.py --host <node-ip> --username <username>
```

### Password-based SSH (requires sshpass)
```bash
./restart_test.py --host <node-ip> --username <username> --password <password>
```

### Execution via MCP Server
```bash
./restart_test.py --host <node-id> --use-mcp \
  --mcp-server network-mapper --mcp-tool <tool-name> \
  --mcp-command-arg command --mcp-target-arg target
```

Notes:
- MCP is enabled by default. Use `--no-mcp` to disable it.
- `--mcp-server` looks up the URL in `mcp.json`.
- `--mcp-tool` must match the MCP tool that runs CLI commands.
- `--mcp-target-arg` is the tool argument for the target identifier.
- If you omit `--mcp-tool`, the script will try to auto-discover a tool
  that accepts a command argument.

### Using NCP Commands
```bash
./restart_test.py --use-ncp --ncp-id 0
```

### Combined Options
```bash
./restart_test.py --host 192.168.1.100 --username admin --use-ncp --ncp-id 0
```

### Apply Config Profiles Before Tests
```bash
./restart_test.py --host 192.168.1.100 --username admin \
  --empty-config-cmd "<command to set empty config>" \
  --normal-config-cmd "<command to set normal config>" \
  --scale-config-cmd "<command to set scale config>"
```

## Command Mapping

The script uses the following commands based on options:

- **Warm restart (standard)**: `request system restart warm`
- **Warm restart (NCP)**: `request system restart ncp 0 warm`
- **Cold restart (standard)**: `request system restart`
- **Cold restart (NCP)**: `request system restart ncp 0`

## Output

The script will:
- Print real-time progress and results to console
- Generate a JSON report file: `restart_test_report_YYYYMMDD_HHMMSS.json`

## Requirements

- Python 3.6+
- SSH access (if testing remote node)
- Appropriate permissions to execute restart commands

## Notes

- The script waits up to 5 minutes for the node to come back online after each restart
- There's a 5-second pause between iterations
- Config snapshots are taken before each test scenario
- Times are measured from restart command execution until node is back online

## Example Output

```
================================================================
STARTING RESTART TEST SUITE
================================================================
Timestamp: 2024-01-15T10:30:00
Host: local
Use NCP: False, NCP ID: 0

================================================================
TEST 1: Warm restart with empty config
================================================================
✓ Saved config snapshot: warm_empty
--- Iteration 1/10 ---
Performing warm restart: request system restart warm
Waiting for node to come online (max 300s)...
✓ Node is online after 45.23 seconds
✓ Reboot verified
✓ Restart completed in 45.23 seconds
✓ Config unchanged for warm_empty
...
```
