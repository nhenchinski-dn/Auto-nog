#!/bin/bash
set -a
source ~/.bug_monitor.env
set +a
exec python3 /home/dn/bug_dashboard.py
