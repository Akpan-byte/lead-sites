#!/bin/bash
# VM-side OOS scoring, restructured per user request:
#   Phase A/B: all 109 NON-indicator strats first (maker then instant bound)
#   Phase C/D: the 4 slow indicator strats LAST in background (memory-hog class)
# 3 workers on a 4-core VM keeps 1 core free; indicators capped at 2 workers
# (they are the OOM-prone class). checkpoint-skip makes restarts safe.
set -x
cd /config/backtest
source /tmp/oos_lists.env
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1

BT_OOS_DIR=oos_maker   python3 -u run_oos.py --files is_files_compact.txt --workers 3 --fill maker   --only "$NON" > logs/oos_maker_vm.log 2>&1
echo "PHASE_A_MAKER_NONIND_RC=$?"
BT_OOS_DIR=oos_instant python3 -u run_oos.py --files is_files_compact.txt --workers 3 --fill instant --only "$NON" > logs/oos_instant_vm.log 2>&1
echo "PHASE_B_INSTANT_NONIND_RC=$?"
BT_OOS_DIR=oos_maker   python3 -u run_oos.py --files is_files_compact.txt --workers 2 --fill maker   --only "$IND" > logs/oos_maker_ind_vm.log 2>&1
echo "PHASE_C_MAKER_IND_RC=$?"
BT_OOS_DIR=oos_instant python3 -u run_oos.py --files is_files_compact.txt --workers 2 --fill instant --only "$IND" > logs/oos_instant_ind_vm.log 2>&1
echo "PHASE_D_INSTANT_IND_RC=$?"
echo "VM_OOS_CHAIN_COMPLETE"
