#!/bin/bash
# Pull all backtest result dirs from the laptop (source of truth post-flap).
# Merges into /config/backtest/results/ — VM-local files (e.g. five_min when
# it lands) are preserved; laptop versions overwrite duplicates (deterministic
# backtest => identical content anyway).
set -u
SSH="ssh -p 22 -i /config/.ssh/akpan_device_wsl_key_20260619 -o StrictHostKeyChecking=no -o ConnectTimeout=10 -o BatchMode=yes akpan@100.93.22.56"
BT=/home/akpan/backtest
cd /config/backtest
for d in is_maker is_instant is_scale oos_maker oos_instant quant_maker quant_instant; do
  echo "=== pulling $d ==="
  mkdir -p results/$d
  $SSH "cd $BT/results && tar -cf - $d" | tar -xf - -C results/ && echo "$d OK: $(ls results/$d | wc -l) files" || echo "$d FAILED"
done
echo "=== spot-check quant dsr ==="
python3 - <<'PY'
import json, glob
p = sorted(glob.glob("results/quant_maker/*.quant.json"))
print("quant_maker files:", len(p))
if p:
    d = json.load(open(p[0]))
    print("sample:", d.get("strategy"), "dsr=", d.get("dsr"), "psr=", d.get("psr"))
q = sorted(glob.glob("results/quant_instant/*.quant.json"))
print("quant_instant files:", len(q))
PY
echo PULL_COMPLETE
