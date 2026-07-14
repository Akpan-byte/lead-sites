#!/bin/bash
# End-gate verification for the backtest pipeline.
# Runs AFTER the orchestrator prints PIPELINE COMPLETE and results are pulled
# to the VM. Verifies every strategy has a summary in each results dir
# (quant dirs use *.quant.json, others *.summary.json), patches missing
# quant-suite entries LOCALLY on the VM (quant_suite checkpoint-skip makes
# existing entries cache hits), re-uploads, and regenerates the leaderboard
# if anything was patched.
#
# Background: the 13:06 ssh flap killed orchestrator step-1 mid-wait; the
# instant pools survived as orphans but the orchestrator advanced early, so
# quant_instant can be short a few strats. The 15:25+ tailscale outage made
# the laptop unreachable, so patching moved from laptop to VM. This gate
# catches exactly that.
set -u
cd /config/backtest
SSH="ssh -p 22 -i /config/.ssh/akpan_device_wsl_key_20260619 -o StrictHostKeyChecking=no -o ConnectTimeout=10 -o BatchMode=yes akpan@100.93.22.56"
BT=/home/akpan/backtest
FAIL=0

# Canonical strategy list from the registry (source of truth)
python3 - <<'PY' > /tmp/endgate_strats.txt
import sys
sys.path.insert(0, "src")
from engine.strategy_registry import STRATEGIES
for n in STRATEGIES:
    print(n)
PY
TOTAL=$(wc -l < /tmp/endgate_strats.txt)
echo "registry strategies: $TOTAL"

for d in is_maker is_instant quant_maker quant_instant oos_maker oos_instant; do
  # quant dirs store *.quant.json; everything else stores *.summary.json
  case $d in
    quant_maker|quant_instant) GLOB="*.quant.json"; SUFFIX=".quant.json" ;;
    *) GLOB="*.summary.json"; SUFFIX=".summary.json" ;;
  esac
  ls results/$d/$GLOB 2>/dev/null | sed "s|.*/||; s|$SUFFIX||" | sort > /tmp/endgate_have.txt
  MISSING=$(comm -23 <(sort /tmp/endgate_strats.txt) /tmp/endgate_have.txt)
  N=$(echo "$MISSING" | grep -c .)
  echo "$d: $(wc -l < /tmp/endgate_have.txt)/$TOTAL  missing=$N"
  if [ "$N" -gt 0 ]; then
    echo "  MISSING: $(echo $MISSING | tr '\n' ' ')"
    FAIL=1
    # Patch quant dirs LOCALLY on the VM (laptop was flapping; VM is source of
    # truth). Checkpoint-skip in quant_suite.py makes existing entries cache
    # hits, so this only computes the genuinely missing strats + DSR pass.
    case $d in
      quant_maker|quant_instant)
        FILL=${d#quant_}
        LIST=$(printf '%s' "$MISSING" | tr '\n' ',' | sed 's/,$//')
        echo "  patching $d locally for: $LIST"
        ( cd /config/backtest && BT_IS_DIR=is_$FILL BT_Q_DIR=$d python3 -u quant_suite.py --workers 4 --fill $FILL --only "$LIST" >> logs/endgate_$d.log 2>&1 )
        echo "PATCH_RC=$?"
        rclone copy results/$d gdrive:trading_backtest/results/$d
        ;;
    esac
  fi
done

# is_scale holds only the 4 daily_orb scale variants — informational
echo "is_scale: $(ls results/is_scale/*.summary.json 2>/dev/null | wc -l)/4 (scale variants only)"

if [ "$FAIL" -gt 0 ]; then
  echo "=== ENDGATE: patched gaps, regenerating leaderboard ==="
  python3 -u report_generator.py --upload >> results/report_generator.log 2>&1
  echo "REPORT_REGEN_RC=$?"
else
  echo "=== ENDGATE: all $TOTAL strategies present in every dir ==="
fi
