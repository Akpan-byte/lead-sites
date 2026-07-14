#!/bin/bash
# Background sync loop: pull full result dirs from Modal bt-results volume every 10 min.
set -euo pipefail
cd /config/backtest
source venv/bin/activate
while true; do
  for dir in is_taker_eth oos_taker_eth is_taker_sol oos_taker_sol; do
    modal volume get --force bt-results "$dir" results >/dev/null 2>&1 || true
  done
  echo "$(date -Iseconds) sync done" >> /tmp/modal_sync_loop.log
  sleep 600
done
