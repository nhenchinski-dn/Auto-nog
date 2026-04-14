#!/bin/bash
# Verify SW-248225: PM Profile allows multiple test-duration types to coexist
# Device: ncpl-cfm-nog (XEC1E3VR00008) @ 100.64.4.93

DEVICE_IP="100.64.4.93"
PROFILE_NAME="SW248225_TEST"

ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 "$DEVICE_IP" <<'SSHEOF'

echo "=== STEP 0: Check current version ==="
show system version | no-more

echo ""
echo "=== STEP 1: Clean up any previous test profile ==="
config
no services performance-monitoring profiles cfm two-way-delay-measurement SW248225_TEST
commit
exit

echo ""
echo "=== STEP 2: Configure test-duration probes and commit ==="
config
services performance-monitoring profiles cfm two-way-delay-measurement SW248225_TEST test-duration probes probe-count 10 probe-interval 1 repeat-interval 60
commit
exit

echo ""
echo "=== STEP 2 VERIFY: Show config after probes commit ==="
show config services performance-monitoring profiles cfm two-way-delay-measurement SW248225_TEST | no-more

echo ""
echo "=== STEP 3: In single transaction, remove probes and add time-frame ==="
config
no services performance-monitoring profiles cfm two-way-delay-measurement SW248225_TEST test-duration probes
services performance-monitoring profiles cfm two-way-delay-measurement SW248225_TEST test-duration time-frame minutes 5 probe-interval 2 repeat-interval 600
commit
exit

echo ""
echo "=== STEP 3 VERIFY: Show config after switching to time-frame ==="
echo "=== EXPECTED (fixed): Only time-frame should appear ==="
echo "=== BUG (unfixed): Both probes AND time-frame would appear ==="
show config services performance-monitoring profiles cfm two-way-delay-measurement SW248225_TEST | no-more

echo ""
echo "=== STEP 4: Clean up test profile ==="
config
no services performance-monitoring profiles cfm two-way-delay-measurement SW248225_TEST
commit
exit

echo ""
echo "=== VERIFICATION COMPLETE ==="
SSHEOF
