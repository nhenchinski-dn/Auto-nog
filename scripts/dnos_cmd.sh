#!/bin/bash
# Run DNOS CLI commands on a remote device
# Usage: ./dnos_cmd.sh <hostname> <command1> [command2] ...
HOST=$1
shift

{
    sleep 6
    for cmd in "$@"; do
        echo "$cmd"
        sleep 2
    done
    sleep 1
    echo "exit"
} | sshpass -p 'dnroot' ssh -tt -o StrictHostKeyChecking=no -o PubkeyAuthentication=no dnroot@${HOST} 2>&1 | sed 's/\x1b\[[0-9;]*m//g' | grep -v "^stty:" | grep -v "DRIVENETS CLI Loading"
