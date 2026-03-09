#!/bin/bash
# SW-211457 Bug Reproduction Script
# Tests the race condition between two CLI sessions doing rollback + commit

DEVICE_IP="100.64.3.239"
USER="dnroot"
PASS="dnroot"
LOGDIR="/home/dn/sw211457_logs"
mkdir -p "$LOGDIR"

SESSION_X_LOG="$LOGDIR/session_x.log"
SESSION_Y_LOG="$LOGDIR/session_y.log"
SYNC_FILE="$LOGDIR/sync_signal"

rm -f "$SYNC_FILE" "$SESSION_X_LOG" "$SESSION_Y_LOG"

echo "=== SW-211457 Bug Reproduction Test ==="
echo "Device: $DEVICE_IP"
echo "Date: $(date)"
echo ""

run_session_x() {
    echo "[Session X] Starting..." | tee -a "$SESSION_X_LOG"
    
    sshpass -p "$PASS" ssh -o StrictHostKeyChecking=no -o PubkeyAuthentication=no \
        -o ServerAliveInterval=30 "$USER@$DEVICE_IP" << 'EOF' 2>&1 | tee -a "$SESSION_X_LOG"
configure

interfaces irb66 admin-state enabled ipv4-address 101.1.0.254/24
network-services evpn instance kfkfkf protocols bgp 1 export-l2vpn-evpn route-target 100:1
network-services evpn instance kfkfkf protocols bgp 1 import-l2vpn-evpn route-target 100:1
network-services evpn instance kfkfkf protocols bgp 1 route-distinguisher 65145:1
network-services evpn instance kfkfkf counters service-counters enabled
network-services evpn instance kfkfkf router-interface irb66
network-services vrf instance alpha interface irb66

show config compare

commit

rollback 1

show config compare

commit

exit
exit
EOF

    echo "[Session X] Done" | tee -a "$SESSION_X_LOG"
    touch "$SYNC_FILE"
}

run_session_y() {
    echo "[Session Y] Waiting for Session X to start commit..." | tee -a "$SESSION_Y_LOG"
    sleep 8
    
    echo "[Session Y] Starting - attempting rollback during commit window..." | tee -a "$SESSION_Y_LOG"
    
    sshpass -p "$PASS" ssh -o StrictHostKeyChecking=no -o PubkeyAuthentication=no \
        -o ServerAliveInterval=30 "$USER@$DEVICE_IP" << 'EOF' 2>&1 | tee -a "$SESSION_Y_LOG"
configure

rollback 1

show config compare

commit

exit
exit
EOF

    echo "[Session Y] Done" | tee -a "$SESSION_Y_LOG"
}

echo "--- Starting Session X (configure + commit + rollback + commit) ---"
run_session_x &
SESSION_X_PID=$!

echo "--- Starting Session Y (rollback during X's commit) ---"
run_session_y &
SESSION_Y_PID=$!

echo "Waiting for both sessions to complete..."
wait $SESSION_X_PID
wait $SESSION_Y_PID

echo ""
echo "=== Test Complete ==="
echo ""
echo "--- Session X Output ---"
cat "$SESSION_X_LOG"
echo ""
echo "--- Session Y Output ---"
cat "$SESSION_Y_LOG"
echo ""

if grep -q "Internal error" "$SESSION_Y_LOG" 2>/dev/null; then
    echo "*** BUG REPRODUCED: Internal error found in Session Y ***"
elif grep -q "Internal error" "$SESSION_X_LOG" 2>/dev/null; then
    echo "*** BUG REPRODUCED: Internal error found in Session X ***"
elif grep -q "RECOVERY" "$SESSION_Y_LOG" 2>/dev/null || grep -q "RECOVERY" "$SESSION_X_LOG" 2>/dev/null; then
    echo "*** BUG REPRODUCED: Device entered RECOVERY ***"
elif grep -q "Commit succeeded" "$SESSION_Y_LOG" 2>/dev/null; then
    echo "*** BUG NOT REPRODUCED: Session Y commit succeeded ***"
elif grep -q "another commit is in progress" "$SESSION_Y_LOG" 2>/dev/null; then
    echo "*** TIMING ISSUE: Session Y hit concurrent commit guard ***"
else
    echo "*** INCONCLUSIVE: Check logs for details ***"
fi
