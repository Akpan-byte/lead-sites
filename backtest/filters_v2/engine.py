#!/usr/bin/env python3
# CHANGE_SUMMARY
# 2026-07-15  kilo
#   - Ported into /config/repos/lead-sites/backtest/filters_v2/ for the GHA
#     btc_orb_feed_compare workflow. No functional changes; existing env vars
#     (TRADES_DIR, KLINES_SYMBOL, KLINES_GLOB) are used by apply_orb_fade_filters.py.
# WHY: Reuse the proven filters_v2 replay math inside the repo that the workflow
#      checks out, without requiring access to /config/backtest/filters_v2/.
#
# 2026-07-12  kimi (filters_v2 core engine)
#   - Created engine.py: kline loading (cached), trade loading, feature-matrix
#     assembly, strictly no-lookahead trade->bar mapping, trailing-window
#     percentile ranks, v1-compatible 3-action replay, vectorized config eval.
#   - load_trades() now also keeps condition_id + direction (string columns)
#     for the per-trade PM book features.
#   - Added trailing_trade_percentile_ranks(): trailing-7d percentile ranks
#     over the strategy's own TRADE sequence (for per-trade features such as
#     the PM book metrics), warmup < 20 valid trades -> NaN -> breakout.
# 2026-07-14  kilo
#   - Made kline source configurable via KLINES_SYMBOL (default BTC) and
#     KLINES_GLOB env vars so ETH/SOL reference zips can be loaded.
#   - Made the kline cache filename symbol-specific (bars_1m_<symbol>.pkl)
#     so switching symbols no longer clobbers the previously cached bars.
# WHY: ETH and SOL 1m reference kline zips were built for filters_v2; the
#      engine must load them without overwriting the existing BTC cache.
"""
engine.py - core replay engine for the filters_v2 sweep.

Replays REAL in-sample taker trades and, per trade, applies one of three
actions decided by a filter config:
  - "breakout" (code 0): take the trade exactly as recorded
  - "fade"     (code 1): mirror the trade (per-share pnl negated, entry price
                         mirrored as 1 - entry_price for sizing) -- identical to
                         v1 sweep_filters.py
  - "skip"     (code 2): no trade

Sizing (identical to v1):
  start equity $200, RISK_PCT = 0.005, shares = max(5, 0.005 * equity / ep),
  recomputed per trade from running equity. `pnl` from the trade record is net
  (fees already embedded), exactly as v1 treats it -- no separate fee handling.

No-lookahead rule: the feature row used for a trade at opened_at T comes from
the LAST 1m bar whose close (ts + 60s) is <= T, i.e. ts <= T - 60. Trades with
no such bar, or with NaN features (warmup), are taken as breakout.
"""
from __future__ import annotations

import glob
import gzip
import os
import json
import pickle
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

# ----------------------------- constants ------------------------------------
CAP_START = 200.0
RISK_PCT = 0.005          # risk 0.5% of running equity per trade (v1)
MIN_SHARES = 5.0          # minimum position size in shares (v1)
BAR_SECS = 60.0           # 1m bar length; bar close time = ts + BAR_SECS
IS_END = datetime(2026, 7, 2, tzinfo=timezone.utc).timestamp()  # IS data window ends Jul 1 (bars with ts < this)

# trailing 7-day window (in 1m bars) for percentile ranks, and the minimum
# number of valid values required in that window (same threshold as v1)
PCT_WINDOW = 10080
PCT_MIN_COUNT = 50

# action codes
BRK, FADE, SKIP = 0, 1, 2
ACTION_CODES = {"breakout": BRK, "brk": BRK, "fade": FADE, "skip": SKIP}

HERE = Path(__file__).resolve().parent
CACHE_DIR = HERE / "cache"
RESULTS_DIR = HERE / "results"
TRADES_DIR = Path(os.environ.get("TRADES_DIR", "/config/backtest/results/is_taker"))
# Default to BTC; set KLINES_SYMBOL=ETH or KLINES_SYMBOL=SOL (or a full KLINES_GLOB)
# to load the corresponding reference 1m zips produced by build_ref_klines.py.
KLINES_SYMBOL = os.environ.get("KLINES_SYMBOL", "BTC").upper()
KLINES_GLOB = os.environ.get(
    "KLINES_GLOB",
    f"/tmp/ref_{KLINES_SYMBOL.lower()}_1m/{KLINES_SYMBOL}USDT-1m-*.zip",
)

STRATEGIES = [f"btc_orb_{tf}_5re" for tf in ("1m", "3m", "5m", "15m", "30m", "1h")]

# kline CSV columns (no header) inside the Binance daily zips
_KLINE_USECOLS = [0, 1, 2, 3, 4, 5, 7, 8, 9]
_KLINE_NAMES = ["open_time", "open", "high", "low", "close", "volume",
                "quote_volume", "trade_count", "taker_buy_base_vol"]


# ----------------------------- data loading ---------------------------------
def load_klines(force: bool = False) -> pd.DataFrame:
    """Load 1m klines from the daily zips into a DataFrame.

    The symbol is controlled by KLINES_SYMBOL (default BTC) or a full
    KLINES_GLOB can be supplied; see the module-level KLINES_GLOB.

    Columns: ts (float seconds, bar OPEN time), open, high, low, close,
    volume, quote_volume, trade_count, taker_buy_base_vol -- sorted ascending.
    Cached to cache/bars_1m_<symbol>.pkl after the first load (pyarrow is not
    available in the project venv, so pickle instead of parquet).
    """
    cache_path = CACHE_DIR / f"bars_1m_{KLINES_SYMBOL.lower()}.pkl"
    zips = sorted(glob.glob(KLINES_GLOB))
    if not zips:
        # Fallback: if the daily zip source was wiped (e.g. /tmp on WSL restart)
        # but the cached bars remain, use the cache. Log a warning so it's visible.
        if cache_path.exists():
            print(f"WARN: no kline zips at {KLINES_GLOB}; using cached {cache_path}", flush=True)
            with open(cache_path, "rb") as f:
                cached = pickle.load(f)
            return cached["df"]
        raise FileNotFoundError(f"no kline zips matched {KLINES_GLOB}")
    file_sig = [Path(z).name for z in zips]

    if not force and cache_path.exists():
        with open(cache_path, "rb") as f:
            cached = pickle.load(f)
        if cached.get("files") == file_sig:
            return cached["df"]

    frames = []
    for zp in zips:
        with zipfile.ZipFile(zp) as z:
            for name in z.namelist():
                with z.open(name) as f:
                    df = pd.read_csv(f, header=None, usecols=_KLINE_USECOLS,
                                     names=_KLINE_NAMES)
                    frames.append(df)
    raw = pd.concat(frames, ignore_index=True)
    # Binance kline open_time here is MICROseconds -> float seconds
    raw["ts"] = raw["open_time"].astype(np.float64) / 1_000_000.0
    raw = raw[raw["ts"] < IS_END]
    bars = raw[["ts", "open", "high", "low", "close", "volume",
                "quote_volume", "trade_count", "taker_buy_base_vol"]].copy()
    bars = bars.sort_values("ts").reset_index(drop=True)
    for c in bars.columns:
        if c != "ts":
            bars[c] = bars[c].astype(np.float64)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(cache_path, "wb") as f:
        pickle.dump({"files": file_sig, "df": bars}, f, protocol=4)
    return bars


def load_trades(strategy_name: str) -> pd.DataFrame:
    """Load the recorded IS taker trades for one strategy, sorted by opened_at.

    Numeric columns: opened_at, closed_at, entry_price, shares, pnl,
    fee_shares, entry_fee, exit_fee. String columns: condition_id, direction
    (needed by the per-trade PM book features). Empty file -> empty frame.
    """
    path = TRADES_DIR / f"phase_2.{strategy_name}.trades.jsonl.gz"
    keep_num = ("opened_at", "closed_at", "entry_price", "shares", "pnl",
                "fee_shares", "entry_fee", "exit_fee")
    keep_str = ("condition_id", "direction")
    rows = []
    with gzip.open(path, "rt") as f:
        for line in f:
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


# ------------------------- feature plumbing ----------------------------------
def build_feature_matrix(bars_1m: pd.DataFrame, *feature_dicts: dict) -> pd.DataFrame:
    """Assemble one column per feature, indexed by 1m bar open ts.

    Each feature_dict is {name: pd.Series indexed by bar ts} as returned by
    the feature modules' compute_features(bars_1m). Values are aligned to the
    bars' ts; bars not covered by a feature get NaN (warmup -> breakout).
    """
    ts = bars_1m["ts"].to_numpy(np.float64)
    merged: dict[str, pd.Series] = {}
    for fd in feature_dicts:
        if not fd:
            continue
        for name, series in fd.items():
            if name in merged:
                print(f"WARNING: duplicate feature name {name!r}; keeping last")
            merged[name] = series
    if not merged:
        return pd.DataFrame(index=pd.Index(ts, name="ts"))
    mat = pd.DataFrame(merged)
    mat.index = mat.index.astype(np.float64)
    mat = mat.reindex(ts)
    mat.index.name = "ts"
    return mat.astype(np.float32)


def trade_bar_index(bar_ts: np.ndarray, opened_at: np.ndarray) -> np.ndarray:
    """For each trade, the index of the LAST 1m bar with close_time <= opened_at.

    bar close = ts + 60s, so we need ts <= opened_at - 60. Uses searchsorted;
    trades before the first closed bar get -1 (treated as warmup -> breakout).
    """
    bar_ts = np.asarray(bar_ts, dtype=np.float64)
    opened_at = np.asarray(opened_at, dtype=np.float64)
    return np.searchsorted(bar_ts, opened_at - BAR_SECS, side="right") - 1


def rolling_percentile_ranks(mat: np.ndarray, bar_idx: np.ndarray,
                             window: int = PCT_WINDOW,
                             min_count: int = PCT_MIN_COUNT) -> np.ndarray:
    """Trailing percentile rank of each queried bar's feature values.

    For each bar index i in bar_idx and each feature column: rank of the value
    at i among all valid values in the trailing `window` bars ENDING AT i
    (inclusive), in [0, 100): 100 * (# window values strictly < v) / (# valid).
    NaN when the value itself is NaN or fewer than min_count valid values exist
    in the window. Fully no-lookahead: only bars <= i are used.

    mat: (n_bars, n_features) float array; bar_idx: array of bar indices.
    Returns (len(bar_idx), n_features) float32.
    """
    mat = np.asarray(mat, dtype=np.float32)
    n_feat = mat.shape[1]
    out = np.full((len(bar_idx), n_feat), np.nan, dtype=np.float32)
    isnan = np.isnan(mat)  # precompute once for the whole matrix
    for j, i in enumerate(bar_idx):
        lo = max(0, i - window + 1)
        w = mat[lo:i + 1]              # (<=window, n_feat)
        v = mat[i]                     # (n_feat,)
        cnt = np.count_nonzero(~isnan[lo:i + 1], axis=0)
        # NaN < v is False, so NaNs are automatically excluded from `less`
        less = np.count_nonzero(w < v, axis=0)
        ok = (cnt >= min_count) & ~np.isnan(v)
        out[j] = np.where(ok, less / np.maximum(cnt, 1) * 100.0, np.nan)
    return out


# trailing window (seconds) and warmup for per-trade percentile ranks
TRADE_PCT_WINDOW_SECS = 7 * 86400.0   # trailing 7 days of the strategy's trades
TRADE_PCT_MIN_COUNT = 20              # warmup: fewer valid trades -> NaN -> breakout


def trailing_trade_percentile_ranks(values: np.ndarray, opened_at: np.ndarray,
                                    window_secs: float = TRADE_PCT_WINDOW_SECS,
                                    min_count: int = TRADE_PCT_MIN_COUNT) -> np.ndarray:
    """Percentile ranks for PER-TRADE features (e.g. PM book metrics).

    For trade i: rank of its feature values among all of the strategy's trades
    with opened_at in [t_i - window_secs, t_i] (inclusive), in [0, 100):
    100 * (# window values strictly < v) / (# valid). NaN when the value is NaN
    or fewer than min_count valid trades exist in the window (warmup -> the
    sweep takes those trades as breakout). Trades must be sorted by opened_at.

    values: (n_trades, n_features); opened_at: (n_trades,) float seconds.
    Returns float32 (n_trades, n_features).
    """
    v = np.asarray(values, dtype=np.float64)
    oa = np.asarray(opened_at, dtype=np.float64)
    lo = np.searchsorted(oa, oa - window_secs, side="left")
    out = np.full(v.shape, np.nan, dtype=np.float32)
    isn = np.isnan(v)
    for i in range(len(v)):
        w = v[lo[i]:i + 1]
        cnt = np.count_nonzero(~isn[lo[i]:i + 1], axis=0)
        less = np.count_nonzero(w < v[i], axis=0)  # NaN < v is False
        ok = (cnt >= min_count) & ~isn[i]
        out[i] = np.where(ok, less / np.maximum(cnt, 1) * 100.0, np.nan)
    return out


# ------------------------------ replay ---------------------------------------
def normalize_actions(actions, n: int) -> np.ndarray:
    """Accept int codes (0/1/2) or strings ('breakout'/'brk'/'fade'/'skip')."""
    if isinstance(actions, str):
        return np.full(n, ACTION_CODES[actions], dtype=np.int8)
    a = np.asarray(actions)
    if a.dtype.kind in "iu" and len(a) and a.max() <= 2:
        return a.astype(np.int8)
    mapped = [ACTION_CODES[x] for x in a]
    return np.array(mapped, dtype=np.int8)


def replay(trades: pd.DataFrame, actions) -> dict:
    """Replay trades under per-trade actions with the exact v1 sizing/fade math.

    trades: DataFrame with entry_price, shares, pnl (as recorded).
    actions: per-trade int codes or strings; length must match trades.
    Returns dict(pnl, maxDD, maxDD_pct, n_trades, n_fades, n_skips) where:
      pnl      = final equity - CAP_START
      maxDD    = peak-to-trough max equity drawdown in dollars
      maxDD_pct= peak-to-trough max drawdown as % of running peak equity
      n_trades = trades actually taken (skips excluded)
    """
    n = len(trades)
    a = normalize_actions(actions, n)
    ep_arr = trades["entry_price"].to_numpy(np.float64)
    ps_arr = trades["pnl"].to_numpy(np.float64) / trades["shares"].to_numpy(np.float64)

    eq = CAP_START
    peak = CAP_START
    dd = 0.0
    dd_pct = 0.0
    n_taken = n_fade = n_skip = 0
    risk = RISK_PCT
    minc = MIN_SHARES
    for k in range(n):
        act = a[k]
        if act == SKIP:
            n_skip += 1
            continue
        ep = ep_arr[k]
        ps = ps_arr[k]
        if act == FADE:
            ep = 1.0 - ep          # mirrored entry price, as in v1
            ps = -ps               # per-share pnl negated, as in v1
            n_fade += 1
        shares = risk * eq / ep
        if shares < minc:
            shares = minc
        eq += shares * ps
        if eq < 0.0:
            eq = 0.0
        if eq > peak:
            peak = eq
        d = peak - eq
        if d > dd:
            dd = d
        dp = d / peak              # relative drawdown vs running peak equity
        if dp > dd_pct:
            dd_pct = dp
        n_taken += 1
    return {"pnl": eq - CAP_START, "maxDD": dd, "maxDD_pct": dd_pct * 100.0,
            "n_trades": n_taken, "n_fades": n_fade, "n_skips": n_skip}


_OPS = {"<": np.less, ">": np.greater, "<=": np.less_equal, ">=": np.greater_equal}


def evaluate_config(feature_values_for_trades, op: str, threshold: float,
                    action: str, trades: pd.DataFrame) -> dict:
    """Vectorized single-threshold config: `action` when value <op> threshold.

    NaN feature values (warmup / insufficient history) never trigger, so those
    trades are taken as breakout. Returns the replay() result dict.
    """
    vals = np.asarray(feature_values_for_trades, dtype=np.float64)
    mask = _OPS[op](vals, threshold)   # comparisons with NaN are False
    actions = np.where(mask, ACTION_CODES[action], BRK).astype(np.int8)
    return replay(trades, actions)
