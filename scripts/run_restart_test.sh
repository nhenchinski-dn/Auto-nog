#!/bin/bash
# Quick script to run restart tests on YE41F7VK00003B1

# Option 1: If your username is the same on both machines
# python3 /home/dn/restart_test.py --host YE41F7VK00003B1

# Option 2: If you need to specify a different username
# python3 /home/dn/restart_test.py --host YE41F7VK00003B1 --username <your-username>

# Option 3: If you need to use NCP commands
# python3 /home/dn/restart_test.py --host YE41F7VK00003B1 --use-ncp --ncp-id 0

# Option 4: If you need password-based SSH (requires sshpass)
# python3 /home/dn/restart_test.py --host YE41F7VK00003B1 --username dnroot --password dnroot

# Option 5: Apply config profiles before each test scenario
# python3 /home/dn/restart_test.py --host YE41F7VK00003B1 --username dnroot \
#   --empty-config-cmd "<command to set empty config>" \
#   --normal-config-cmd "<command to set normal config>" \
#   --scale-config-cmd "<command to set scale config>"

# Option 6: Use MCP server (network-mapper) instead of SSH
# The script will auto-discover the tool/args, or you can override them.
# python3 /home/dn/restart_test.py --host YE41F7VK00003B1 --use-mcp \
#   --mcp-server network-mapper
#
# python3 /home/dn/restart_test.py --host YE41F7VK00003B1 --use-mcp \
#   --mcp-server network-mapper --mcp-tool <tool-name> \
#   --mcp-command-arg <command-arg> --mcp-target-arg <target-arg>

# Default: Use MCP server (auto-discover tool/args)
python3 /home/dn/restart_test.py --host YE41F7VK00003B1
