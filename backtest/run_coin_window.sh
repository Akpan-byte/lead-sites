#!/bin/bash
# Launcher for one coin/window coarse taker screen.
# Usage: ./run_coin_window.sh <coin> <window> <workers> <files> <index> <results_env>
set -euo pipefail
coin="${1}"
window="${2}"
workers="${3}"
files="${4}"
index="${5}"
res_env="${6}"   # e.g. is_taker_eth

export BT_ASSET="${coin^^}"
export BT_EXTRA_STRATEGIES="/config/backtest/coin_full_registry_${coin}.json"
export BT_ONLY_EXTRA_REGISTRY=1
export BT_REF_BTC_1M_DIR="/tmp/ref_${coin}_1m"
export BT_IS_INDEX="${index}"

py="/config/backtest/venv/bin/python"
root="/config/backtest"

mkdir -p "${root}/logs"
log="${root}/logs/${res_env}.log"

if [[ "$window" == "is" ]]; then
  export BT_IS_DIR="${res_env}"
  exec nice -n 10 "$py" "${root}/run_is.py" \
    --files "$files" --workers "$workers" --compact --fill taker \
    > "$log" 2>&1
else
  export BT_OOS_DIR="${res_env}"
  exec nice -n 10 "$py" "${root}/run_oos.py" \
    --files "$files" --workers "$workers" --fill taker \
    > "$log" 2>&1
fi
