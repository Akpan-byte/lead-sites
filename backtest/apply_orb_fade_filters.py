#!/usr/bin/env python3
# CHANGE_SUMMARY
# 2026-07-15  kilo
#   - Created apply_orb_fade_filters.py for the btc_orb_feed_compare GHA workflow.
#     Reads raw daily_orb_v5 trades produced by run_is.py, applies the orb_fade
#     filter configured in coin_full_registry_btc.json (or a hardcoded fallback),
#     and writes the filtered trades back to the same results/is_*/ file so
#     quant_suite.py can run unchanged.
#   - Computes trailing-7d percentile ranks for IND_* features off the reference
#     1m klines and for PM_* features off the leg's own trade sequence, exactly
#     like filters_v2 engine.py / find_hedge_candidates.py.
# WHY: The GHA workflow was comparing unfiltered daily_orb_v5 cores; this script
#      turns each leg into the actual orb_fade filtered leg before quant metrics.
"""Apply orb_fade filters to raw daily_orb_v5 IS trades.

Usage:
  BT_IS_DIR=is_bn BT_REF_BTC_1M_DIR=/tmp/ref_btc_1m PM_BOOK_DIR=/tmp/btc5m_all \
    python3 apply_orb_fade_filters.py --leg b1m_liqpull40

The script overwrites the input trades file in-place.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from pathlib import Path

# Ensure the bundled filters_v2 package is importable when run from backtest/.
HERE = Path(__file__).resolve().parent
FILTERS = HERE / "filters_v2"
if str(FILTERS) not in sys.path:
    sys.path.insert(0, str(FILTERS))

# Hardcoded fallback configs for the 6 orb_fade legs. These match the
# `_filter` field written by the btc_orb_feed_compare workflow.
FALLBACK_FILTERS: dict[str, dict] = {
    "b1m_liqpull40": {"feat": "PM_liq_pull20", "op": "<", "thr": 40, "action": "fade"},
    "b1m_imbslope30": {"feat": "PM_imb_slope20", "op": "<", "thr": 30, "action": "fade"},
    "b1m_chand40": {"feat": "IND_chand22_1d", "op": ">", "thr": 40, "action": "fade"},
    "l5m_mom30": {"feat": "IND_mom30_1d", "op": "<", "thr": 80, "action": "fade"},
    "l5m_liqpull60": {"feat": "PM_liq_pull20", "op": "<", "thr": 60, "action": "fade"},
    "l3m_bbpct40": {"feat": "IND_bbpct20_1d", "op": ">", "thr": 40, "action": "fade"},
}


def load_trades(path: Path) -> pd.DataFrame:
    """Load a lead-sites trade jsonl.gz into a DataFrame.

    Keeps the numeric columns engine.py expects plus condition_id/direction for
    the PM book features. Empty file -> empty frame.
    """
    keep_num = ("opened_at", "closed_at", "entry_price", "shares", "pnl",
                "fee_shares", "entry_fee", "exit_fee")
    keep_str = ("condition_id", "direction")
    rows = []
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            t = json.loads(line)
            row = {k: t.get(k) for k in keep_num}
            row.update({k: t.get(k) for k in keep_str})
            rows.append(row)
    df = pd.DataFrame(rows, columns=list(keep_num) + list(keep_str))
    if not df.empty:
        df = df.sort_values("opened_at").reset_index(drop=True)
        for c in keep_num:
            df[c] = pd.to_numeric(df[c], errors="coerce").astype(np.float64)
    return df


def load_filter_config(leg: str, registry_path: Path | None) -> dict:
    """Return the filter config for `leg`, preferring registry `_filter`."""
    if registry_path and registry_path.exists():
        with open(registry_path, "r", encoding="utf-8") as fh:
            registry = json.load(fh)
        # The registry key is btc_<leg>.
        key = f"btc_{leg}"
        reg = registry.get(key, {})
        filt = reg.get("_filter")
        if filt and "feat" in filt and "op" in filt and "thr" in filt:
            cfg = dict(filt)
            cfg.setdefault("action", "fade")
            return cfg
    return FALLBACK_FILTERS[leg]


# Delayed imports: engine.py and features_pm_book.py read env vars at import time,
# so we must set KLINES_GLOB / PM_BOOK_DIR / TRADES_DIR before importing them.
np = pd = engine = indicator_features = pm_trade_features = None  # type: ignore
BRK = FADE = SKIP = ACTION_CODES = None  # type: ignore


def _import_filters() -> None:
    global np, pd, engine, indicator_features, pm_trade_features
    global BRK, FADE, SKIP, ACTION_CODES
    if engine is not None:
        return
    import numpy as _np
    import pandas as _pd
    np = _np  # type: ignore
    pd = _pd  # type: ignore
    import engine as _engine
    engine = _engine  # type: ignore
    from engine import BRK as _BRK, FADE as _FADE, SKIP as _SKIP, ACTION_CODES as _ACTION_CODES
    BRK = _BRK  # type: ignore
    FADE = _FADE  # type: ignore
    SKIP = _SKIP  # type: ignore
    ACTION_CODES = _ACTION_CODES  # type: ignore
    from features_indicators import compute_features as _indicator_features
    indicator_features = _indicator_features  # type: ignore
    from features_pm_book import compute_trade_features as _pm_trade_features
    pm_trade_features = _pm_trade_features  # type: ignore


def build_actions(trades: pd.DataFrame, cfg: dict, bar_ts: np.ndarray,
                  feat_names: list[str] | None, feat_values: np.ndarray | None,
                  pm_raw: dict[str, pd.Series] | None) -> np.ndarray:
    """Per-trade action codes: FADE when the percentile condition is met, else BRK.

    IND_* features are bar-indexed; PM_* features are per-trade. NaN percentiles
    never trigger, matching the filters_v2 convention (warmup -> breakout).
    """
    n = len(trades)
    feat = cfg["feat"]
    op = cfg["op"]
    thr = float(cfg["thr"])

    if feat.startswith("IND_"):
        if feat_names is None or feat_values is None:
            raise ValueError(f"IND feature {feat} requested but indicator matrix not computed")
        idx = engine.trade_bar_index(bar_ts, trades["opened_at"].to_numpy(np.float64))
        fi = feat_names.index(feat)
        valid = idx >= 0
        vals = np.full(n, np.nan, dtype=np.float64)
        if valid.any():
            ts = bar_ts[idx[valid]]
            union_idx = np.unique(idx[valid])
            ranks = engine.rolling_percentile_ranks(feat_values, union_idx)
            pos = {t: i for i, t in enumerate(bar_ts[union_idx])}
            rows = [pos[t] for t in ts]
            vals[valid] = ranks[rows, fi]
    elif feat.startswith("PM_"):
        if pm_raw is None or feat not in pm_raw:
            raise ValueError(f"PM feature {feat} requested but PM features not computed")
        raw = pm_raw[feat]
        vals = engine.trailing_trade_percentile_ranks(
            raw.to_numpy(np.float64)[:, None],
            trades["opened_at"].to_numpy(np.float64),
        )[:, 0]
    else:
        raise ValueError(f"unsupported feature family: {feat}")

    mask = engine._OPS[op](vals, thr)
    return np.where(mask, ACTION_CODES[cfg.get("action", "fade")], BRK).astype(np.int8)


def replay_to_trades(trades: pd.DataFrame, actions: np.ndarray) -> list[dict]:
    """Replay trades under `actions` with v1 sizing and return new trade dicts.

    Breakout trades keep their original direction/entry_price; fade trades are
    mirrored (YES<->NO, entry_price = 1 - ep). Shares are recomputed from the
    running equity. Skipped trades are dropped. The returned records are written
    back to the trades jsonl.gz file for quant_suite.py.
    """
    n = len(trades)
    ep_arr = trades["entry_price"].to_numpy(np.float64)
    ps_arr = trades["pnl"].to_numpy(np.float64) / trades["shares"].to_numpy(np.float64)
    dir_arr = trades["direction"].astype(str).to_numpy()

    eq = engine.CAP_START
    out: list[dict] = []
    rows = trades.to_dict("records")

    for k in range(n):
        act = actions[k]
        if act == SKIP:
            continue

        ep = float(ep_arr[k])
        per_share = float(ps_arr[k])
        direction = str(dir_arr[k]).upper()

        if act == FADE:
            direction = "NO" if direction == "YES" else "YES"
            ep = 1.0 - ep
            per_share = -per_share

        if ep <= 0.0:
            # Degenerate fade price; treat as skip to avoid division by zero.
            continue

        shares = engine.RISK_PCT * eq / ep
        if shares < engine.MIN_SHARES:
            shares = engine.MIN_SHARES

        pnl = shares * per_share
        eq += pnl
        if eq < 0.0:
            eq = 0.0

        rec = dict(rows[k])
        rec["direction"] = direction
        rec["entry_price"] = round(ep, 6)
        rec["shares"] = round(shares, 6)
        rec["pnl"] = round(pnl, 6)
        # Net pnl is recomputed; zero out the fee fields so they stay consistent.
        rec["fee_shares"] = 0.0
        rec["entry_fee"] = 0.0
        rec["exit_fee"] = 0.0
        out.append(rec)

    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--leg", required=True, help="orb_fade leg name, e.g. b1m_liqpull40")
    ap.add_argument("--registry", default="coin_full_registry_btc.json",
                    help="path to strategy registry (relative to backtest/)")
    args = ap.parse_args()

    leg = args.leg
    strategy = f"btc_{leg}"

    is_dir_name = os.environ.get("BT_IS_DIR", "is_taker")
    results_dir = HERE / "results" / is_dir_name
    trades_path = results_dir / f"{strategy}.trades.jsonl.gz"

    if not trades_path.exists():
        print(f"ERROR: trades file not found: {trades_path}", flush=True)
        sys.exit(1)

    # Load filter config from registry or fallback.
    registry_path = HERE / args.registry
    cfg = load_filter_config(leg, registry_path)
    print(f"[{leg}] filter config: {cfg}", flush=True)

    # Configure engine paths from env vars set by the workflow.
    ref_bn = os.environ.get("BT_REF_BTC_1M_DIR")
    ref_hl = os.environ.get("BT_REF_HL_1M_DIR")
    if ref_bn and Path(ref_bn).exists():
        os.environ.setdefault("KLINES_GLOB", f"{ref_bn}/BTCUSDT-1m-*.zip")
    elif ref_hl and Path(ref_hl).exists():
        os.environ.setdefault("KLINES_GLOB", f"{ref_hl}/BTCUSDT-1m-*.zip")
    # If neither is present, engine.py falls back to /tmp/ref_btc_1m.

    # PM book raw data directory (downloaded raw polymarket snapshots).
    pm_book_dir = os.environ.get("PM_BOOK_DIR")
    if pm_book_dir:
        os.environ["PM_BOOK_DIR"] = pm_book_dir

    # TRADES_DIR is kept for completeness in case other engine helpers need it,
    # but this script loads trades directly via load_trades() using the
    # lead-sites naming convention (no phase_2 prefix).
    os.environ["TRADES_DIR"] = str(results_dir)

    _import_filters()

    trades = load_trades(trades_path)
    print(f"[{leg}] loaded {len(trades)} raw trades from {trades_path}", flush=True)

    if trades.empty:
        # Empty input -> empty output.
        with gzip.open(trades_path, "wt", encoding="utf-8") as fh:
            pass
        print(f"[{leg}] no trades to filter; wrote empty file", flush=True)
        return

    feat = cfg["feat"]
    need_ind = feat.startswith("IND_")
    need_pm = feat.startswith("PM_")

    bars = engine.load_klines()
    print(f"[{leg}] loaded {len(bars)} 1m bars", flush=True)

    bar_ts = bars["ts"].to_numpy(np.float64)
    if need_ind:
        mat = engine.build_feature_matrix(bars, indicator_features(bars))
        feat_names = list(mat.columns)
        feat_values = mat.to_numpy(np.float32)
    else:
        feat_names = None
        feat_values = None

    if need_pm:
        pm_raw = pm_trade_features(trades)
    else:
        pm_raw = None

    actions = build_actions(trades, cfg, bar_ts, feat_names, feat_values, pm_raw)
    n_fade = int((actions == FADE).sum())
    n_skip = int((actions == SKIP).sum())
    n_brk = len(actions) - n_fade - n_skip
    print(f"[{leg}] actions: breakout={n_brk} fade={n_fade} skip={n_skip}", flush=True)

    filtered = replay_to_trades(trades, actions)
    print(f"[{leg}] wrote {len(filtered)} filtered trades", flush=True)

    # Overwrite the input file in-place.
    with gzip.open(trades_path, "wt", encoding="utf-8") as fh:
        for rec in filtered:
            fh.write(json.dumps(rec, default=str) + "\n")

    total_pnl = sum(t["pnl"] for t in filtered)
    print(f"[{leg}] filtered total_pnl={total_pnl:.4f}", flush=True)


if __name__ == "__main__":
    main()
