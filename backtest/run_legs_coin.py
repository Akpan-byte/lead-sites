#!/usr/bin/env python3
# CHANGE_SUMMARY
# 2026-07-14  kimi
#   - Per-coin, per-window leg runner for the 6 ORB-fade legs. Reads the raw
#     taker backtest trades for the ETH/SOL orb base strategies, applies the
#     same filter configs that make up the BTC full_stack, and writes one
#     <leg>.summary.json + <leg>.trades.jsonl.gz per leg.
# WHY: Translate the BTC full_stack (3 base timeframes + 6 filter configs) to
#      ETH/SOL with identical filter semantics.
"""Run the 6 full_stack ORB-fade legs for one coin and window.

Example (IS, ETH):
  TRADES_DIR=/config/backtest/results/is_taker_eth \
  KLINES_SYMBOL=ETH \
  /config/backtest/venv/bin/python run_legs_coin.py --coin eth --window is
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from datetime import datetime, date
from pathlib import Path

import numpy as np
import pandas as pd

FV2 = Path(__file__).resolve().parent / "filters_v2"
if str(FV2) not in sys.path:
    sys.path.insert(0, str(FV2))

# Must import engine after path insert; we will patch constants before use.
import engine
from engine import BRK, FADE, SKIP, ACTION_CODES
from features_indicators import compute_features as indicator_features
from features_pm_book import compute_trade_features as pm_trade_features
from find_hedge_candidates import build_actions, per_trade_pnl
from eval_tf_filters import parse_cfg, daily_pnl
from combined_eval import equity_stats

ET = __import__("zoneinfo").ZoneInfo("America/New_York")

# Base strategy (raw result file = phase_2.<name>.trades.jsonl.gz) -> config -> leg name
LEGS = {
    "eth": [
        ("eth_orb_1m_5re", "IND_chand22_1d_pct>40->fade", "eth_b1m_chand40"),
        ("eth_orb_1m_5re", "PM_imb_slope20_pct<30->fade", "eth_b1m_imbslope30"),
        ("eth_orb_1m_5re", "PM_liq_pull20_pct<40->fade", "eth_b1m_liqpull40"),
        ("eth_orb_3m_5re", "IND_bbpct20_1d_pct>40->fade", "eth_l3m_bbpct40"),
        ("eth_orb_5m_5re", "IND_mom30_5m_pct>30->fade", "eth_l5m_mom30"),
        ("eth_orb_5m_5re", "PM_liq_pull20_pct<60->fade", "eth_l5m_liqpull60"),
    ],
    "sol": [
        ("sol_orb_1m_5re", "IND_chand22_1d_pct>40->fade", "sol_b1m_chand40"),
        ("sol_orb_1m_5re", "PM_imb_slope20_pct<30->fade", "sol_b1m_imbslope30"),
        ("sol_orb_1m_5re", "PM_liq_pull20_pct<40->fade", "sol_b1m_liqpull40"),
        ("sol_orb_3m_5re", "IND_bbpct20_1d_pct>40->fade", "sol_l3m_bbpct40"),
        ("sol_orb_5m_5re", "IND_mom30_5m_pct>30->fade", "sol_l5m_mom30"),
        ("sol_orb_5m_5re", "PM_liq_pull20_pct<60->fade", "sol_l5m_liqpull60"),
    ],
}

WINDOWS = {
    "is": {
        "eth": (date(2026, 5, 11), date(2026, 6, 30)),
        "sol": (date(2026, 5, 13), date(2026, 6, 30)),
    },
    "oos": {
        "eth": (date(2026, 7, 1), date(2026, 7, 13)),
        "sol": (date(2026, 7, 1), date(2026, 7, 13)),
    },
}

RAW_DATA_DIR = {
    "eth": "/tmp/eth5m_all",
    "sol": "/tmp/sol5m_all",
}


def _ts_of_date(d: date, end_of_day: bool = False) -> float:
    """Return UTC epoch seconds for start or end (ET) of a date."""
    if end_of_day:
        dt = datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=ET)
    else:
        dt = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=ET)
    return dt.astimezone(__import__("zoneinfo").ZoneInfo("UTC")).timestamp()


def filter_window(trades: pd.DataFrame, coin: str, window: str) -> pd.DataFrame:
    start_d, end_d = WINDOWS[window][coin]
    lo = _ts_of_date(start_d, False)
    hi = _ts_of_date(end_d, True)
    return trades[(trades["opened_at"] >= lo) & (trades["opened_at"] <= hi)].copy()


def leg_trades(trades: pd.DataFrame, actions: np.ndarray, base: str, cfg_name: str) -> list[dict]:
    """Convert actions + raw trades into the leg's taken/faded trade tape."""
    ep = trades["entry_price"].to_numpy(np.float64)
    ps = trades["pnl"].to_numpy(np.float64) / trades["shares"].to_numpy(np.float64)
    opened = trades["opened_at"].to_numpy(np.float64)
    closed = trades["closed_at"].to_numpy(np.float64)
    conds = trades["condition_id"].to_numpy()
    dirs = trades["direction"].to_numpy()
    out = []
    for k in range(len(trades)):
        act = actions[k]
        if act == SKIP:
            continue
        e = ep[k]
        p = ps[k]
        d = str(dirs[k])
        if act == FADE:
            e = 1.0 - e
            p = -p
            d = "NO" if d == "YES" else "YES"
        # Fixed $200 / 0.5% risk sizing, min 5 shares (matches filters_v2).
        shares = max(5.0, np.floor(0.005 * 200.0 / e)) if e > 0 else 0.0
        out.append({
            "opened_at": float(opened[k]),
            "closed_at": float(closed[k]),
            "condition_id": str(conds[k]),
            "direction": d,
            "entry_price": round(float(e), 6),
            "shares": float(shares),
            "pnl": round(float(shares * p), 6),
            "base_strategy": base,
            "config": cfg_name,
            "action": "fade" if act == FADE else "breakout",
        })
    return out


def run_leg(coin: str, window: str, raw_dir: str, out_dir: str,
            base: str, cfg_name: str, leg_name: str,
            bar_ts: np.ndarray, feat_names: list, feat_values: np.ndarray) -> dict:
    print(f"[{coin}/{window}] {leg_name} ...", flush=True)
    cfg = parse_cfg(cfg_name)
    if cfg is None:
        raise ValueError(f"cannot parse config {cfg_name}")
    # Load raw trades for the base orb strategy.
    trades_path = Path(raw_dir) / f"phase_2.{base}.trades.jsonl.gz"
    if not trades_path.exists():
        raise FileNotFoundError(f"missing raw trades: {trades_path}")
    trades = engine.load_trades(base)
    trades = filter_window(trades, coin, window)
    if trades.empty:
        return {"strategy": leg_name, "coin": coin, "window": window,
                "base_strategy": base, "config": cfg_name, "n_closed": 0,
                "n_fades": 0, "n_skips": 0, "total_pnl": 0.0, "win_rate": 0.0,
                "max_dd_usd": 0.0, "max_dd_pct": 0.0, "equity": 200.0}
    # PM book ranks must be computed on exactly this trade set (alignment matters).
    pmr = pm_trade_features(trades)
    pm_ranks = {n: pmr[n].to_numpy(np.float64) for n in pmr.keys()}
    acts = build_actions(trades, cfg, bar_ts, feat_names, feat_values, pm_ranks)
    leg = leg_trades(trades, acts, base, cfg_name)
    total_pnl = sum(t["pnl"] for t in leg)
    wins = sum(1 for t in leg if t["pnl"] > 0)
    n = len(leg)
    # Daily pnl for drawdown.
    df = pd.DataFrame({
        "day": np.floor(trades["opened_at"].to_numpy(np.float64) / 86400).astype(int),
        "pnl": np.where(acts == SKIP, 0.0, per_trade_pnl(trades, acts)),
    })
    daily = df.groupby("day")["pnl"].sum()
    stats = equity_stats({int(k): float(v) for k, v in daily.items()}, 200.0)
    out = {
        "strategy": leg_name,
        "coin": coin,
        "window": window,
        "base_strategy": base,
        "config": cfg_name,
        "n_closed": n,
        "n_fades": int((acts == FADE).sum()),
        "n_skips": int((acts == SKIP).sum()),
        "total_pnl": round(total_pnl, 4),
        "win_rate": round(100.0 * wins / n, 2) if n else 0.0,
        "max_dd_usd": stats.get("max_dd_usd", 0.0),
        "max_dd_pct": stats.get("max_dd_pct", 0.0),
        "start_capital": 200.0,
        "equity": round(200.0 + total_pnl, 4),
    }
    os.makedirs(out_dir, exist_ok=True)
    with gzip.open(Path(out_dir) / f"{leg_name}.trades.jsonl.gz", "wt", encoding="utf-8") as fh:
        for t in leg:
            fh.write(json.dumps(t, default=str) + "\n")
    with open(Path(out_dir) / f"{leg_name}.summary.json", "w") as fh:
        json.dump(out, fh, indent=1, default=str)
    print(f"[{coin}/{window}] {leg_name} done: n={n} pnl={out['total_pnl']} wr={out['win_rate']}% dd={out['max_dd_pct']}%", flush=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coin", choices=["eth", "sol"], required=True)
    ap.add_argument("--window", choices=["is", "oos"], required=True)
    ap.add_argument("--raw-dir", default=None, help="raw orb results dir (default results/<window>_taker_<coin>)")
    ap.add_argument("--out-dir", default=None, help="output dir (default same as raw-dir)")
    ap.add_argument("--only", default="", help="comma-separated leg names to run (default all)")
    args = ap.parse_args()

    raw_dir = args.raw_dir or f"/config/backtest/results/{args.window}_taker_{args.coin}"
    out_dir = args.out_dir or raw_dir
    only = {n.strip() for n in args.only.split(",") if n.strip()}

    # Point engine + PM book helpers at the right coin data.
    engine.TRADES_DIR = Path(raw_dir)
    import features_pm_book
    features_pm_book.BOOK_DIR = Path(RAW_DATA_DIR[args.coin])
    features_pm_book.CACHE_DIR = Path(f"/tmp/pmbook_cache_{args.coin}")

    # Load full klines (IS+OOS) so features are causal for both windows.
    engine.IS_END = float("inf")
    bars = engine.load_klines()
    print(f"[{args.coin}] loaded {len(bars)} 1m bars ({bars['ts'].min():.0f} .. {bars['ts'].max():.0f})", flush=True)
    bar_ts = bars["ts"].to_numpy(np.float64)
    mat = engine.build_feature_matrix(bars, indicator_features(bars))
    feat_names = list(mat.columns)
    feat_values = mat.to_numpy(np.float32)

    summaries = []
    for base, cfg_name, leg_name in LEGS[args.coin]:
        if only and leg_name not in only:
            continue
        summaries.append(run_leg(
            args.coin, args.window, raw_dir, out_dir,
            base, cfg_name, leg_name,
            bar_ts, feat_names, feat_values
        ))

    print(f"\n[{args.coin}/{args.window}] all legs done:")
    for s in summaries:
        print(f"  {s['strategy']:22s} n={s['n_closed']:4d} pnl={s['total_pnl']:8.2f} "
              f"wr={s['win_rate']:5.1f}% dd={s['max_dd_pct']:5.1f}%")


if __name__ == "__main__":
    main()
