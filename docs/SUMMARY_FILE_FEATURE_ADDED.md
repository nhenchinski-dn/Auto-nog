# Summary File Generation Feature - Added

## Overview
Both test scripts now automatically generate markdown summary files after test completion.

## QoS Sanity Test Script
- File: qos_sanity_test.py
- Commit: 6b28465
- Summary file format: qos_test_summary_<device>_<timestamp>.md
- Generated automatically after every test run
- Includes: statistics, test results by phase, config details

## Y.1731 CFM Test Script  
- File: y1731_cli_tab_test.py
- Commit: 3281baf
- Summary file format: <output-file-base>_summary.md
- Generated when --output-file is specified
- Includes: statistics, results by category (DM/SLM/TAB), config details

## Usage

### QoS Test
python3 qos_sanity_test.py
# Creates: qos_test_summary_<device>_<timestamp>.md

### Y.1731 Test
python3 y1731_cli_tab_test.py --output-file results.txt
# Creates: results.txt and results_summary.md

## Git Status
Both changes committed locally. Ready to push.
