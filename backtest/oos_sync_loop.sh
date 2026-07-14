#!/bin/bash
# Sync VM-produced OOS summaries to the laptop so its OOS step checkpoint-skips
# already-computed strategies. Hardened: rsync is unavailable on this VM, so use
# scp wrapped in `timeout` with ssh keepalives — a stalled tailscale session must
# die within 150s instead of hanging the loop forever (happened 2026-07-11).
SSH_KEY=/config/.ssh/akpan_device_wsl_key_20260619
SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=2 -o BatchMode=yes"
DEST=akpan@100.93.22.56:/home/akpan/backtest/results
END=$(( $(date +%s) + 21600 ))
while [ "$(date +%s)" -lt "$END" ]; do
  [ -f /tmp/stop_oos_sync ] && { echo "stop flag"; break; }
  for d in oos_maker oos_instant; do
    mkdir -p /config/backtest/results/$d
    timeout 150 scp -q -P 22 -i "$SSH_KEY" $SSH_OPTS \
      /config/backtest/results/$d/*.summary.json "$DEST/$d/" 2>/dev/null
  done
  echo "sync $(date +%H:%M:%S) maker=$(ls /config/backtest/results/oos_maker/*.summary.json 2>/dev/null|wc -l) instant=$(ls /config/backtest/results/oos_instant/*.summary.json 2>/dev/null|wc -l)"
  sleep 240
done
