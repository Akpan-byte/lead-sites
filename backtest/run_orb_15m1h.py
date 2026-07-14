#!/usr/bin/env python3
# CHANGE_SUMMARY
# 2026-07-12  kimi
#   - Thin wrapper over run_is.main() for the correctly-sized ORB reruns.
#     Patches driver.STRATEGIES[*]["tf_hint"] at MODULE TOP LEVEL from env vars
#     ORB_TF_HINT (e.g. "15m") and ORB_FAMS (e.g. "15m,30m") BEFORE any replay
#     code runs. Top-level placement is load-bearing: run_is uses a
#     ProcessPoolExecutor and Python 3.14 defaults to forkserver, so children
#     re-import this script as __mp_main__ and must hit the same patch (this is
#     the same trap that forced BT_IS_DIR into an env var in run_is.py).
#   - Output dir is results/$BT_IS_DIR (default is_taker_15m1h) — never touches
#     results/is_taker|is_maker|is_instant.
# WHY: signals/phase_2/btc_orb/signal.py:93 rejects every trade when
#      or_window_seconds >= market duration. The registry leaves tf_hint=None
#      so the replay defaulted duration to "5m" (300s) and the 15m/30m/1h
#      variants (OR windows 300/600/900s) produced EMPTY trade files. Live
#      evidence (Dublin VPS state files, 2026-07-12): btc_orb_15m_* and
#      btc_orb_30m_* trade 15m (900s) markets (15m also trades 4h); btc_orb_1h_*
#      trades 4h markets only because 15m rejects (900>=900) and no 1h markets
#      are currently offered — with the available datasets the faithful mapping
#      is 15m_* + 30m_* -> 15m markets (tf_hint="15m"), 1h_* -> 1h markets
#      (tf_hint="1h").
"""Usage:
  BT_IS_DIR=is_taker_15m1h ORB_TF_HINT=15m ORB_FAMS=15m,30m \
    python run_orb_15m1h.py --files is_files_15m.txt --workers 4 \
    --only phase_2.btc_orb_15m_1re,... --compact --fill taker
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("BT_IS_DIR", "is_taker_15m1h")

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "src")
for p in (HERE, SRC):
    if p not in sys.path:
        sys.path.insert(0, p)

import driver  # noqa: E402

# --- Registry patch (runs in parent AND forkserver children) ---------------
_TF = os.environ.get("ORB_TF_HINT", "")
_FAMS = [f for f in os.environ.get("ORB_FAMS", "").split(",") if f]
_PATCHED: list[str] = []
if _TF and _FAMS:
    for _n, _r in driver.STRATEGIES.items():
        if _n.startswith("phase_2.btc_orb_") and any(
            _n.startswith(f"phase_2.btc_orb_{_f}_") for _f in _FAMS
        ):
            _r["tf_hint"] = _TF
            _PATCHED.append(_n)

import run_is  # noqa: E402  (imports driver; RESULTS dir fixed at import via BT_IS_DIR)


def main() -> None:
    print(f"tf_hint patch: {_TF} on {len(_PATCHED)} strats "
          f"({', '.join(sorted(_PATCHED))})", flush=True)
    print(f"results dir: {run_is.RESULTS}", flush=True)
    run_is.main()


if __name__ == "__main__":
    main()
