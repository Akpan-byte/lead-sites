#!/usr/bin/env python3
# CHANGE_SUMMARY
# 2026-07-15  kilo
#   - Ported into /config/repos/lead-sites/backtest/filters_v2/ for the GHA
#     btc_orb_feed_compare workflow. Made BOOK_DIR and CACHE_DIR configurable
#     via PM_BOOK_DIR / PM_BOOK_CACHE_DIR env vars so the GitHub Actions runner
#     can point at the downloaded raw Polymarket book data.
# WHY: The workflow runs on ubuntu-latest without access to /config/bt_data/5m/.
#
# 2026-07-12  kimi (filters_v2 PM book features)
#   - Created features_pm_book.py: per-trade Polymarket orderbook features.
#     Parses /config/bt_data/5m/<condition_id>.json.gz snapshot series, caches
#     per-snapshot derived metrics in gzipped batch pkls (16 stable batches by
#     market-id hash), and as-of joins each trade to the LAST snapshot with
#     time <= opened_at (strict no-lookahead).
#   - Expanded from family (a) book-snapshot metrics to the full spec:
#     (b) PM contract-price indicators (momentum/RSI/VWAP-dev/z-score/session
#         high-low distance off the price_up series),
#     (c) orderflow over time (size deltas, tick flow, imbalance slope, wall
#         persistence, liquidity pull),
#     (d) Bookmap-style (depth shape, largest-wall side, spread regime).
#   - Cache moved to /tmp/pmbook_cache (disk tight on /config).
# WHY: PM book state is per-market, not a continuous market-wide series, so it
#      cannot be a 1m-bar-indexed feature. It must be computed per trade from
#      that trade's own market file. NOTE: /tmp/btc5m_compact/ files hold only
#      top-1 prices (no sizes/depth), so the full-depth raw files under
#      /config/bt_data/5m/ are the single source for every family.
"""
features_pm_book.py - per-trade features from Polymarket orderbook snapshots.

Data source: /config/bt_data/5m/<market_id>.json.gz -- one file per 5-min BTC
up/down market, ~2,081 snapshots at ~0.1s cadence, each with full-depth
orderbook_up/orderbook_down (bids/asks lists of {size, price}, sorted
best-first; verified on a sample). ~0.01% of snapshots have a None book and
~6.9% are one-sided -- both handled (None -> NaN metrics, missing side -> 0).
(The /tmp/btc5m_compact/*.pkl.gz files carry only t/btc/pu/pd/top-1 prices --
no sizes -- so they cannot support the depth-based families; they are a strict
subset of the raw files used here.)

For each trade (condition_id, opened_at, direction) the LAST snapshot with
time <= opened_at is used (as-of join via searchsorted -- no lookahead).
Windowed features use only snapshots up to and including that as-of snapshot.
Missing market file / no snapshot before entry / undefined window -> NaN
(-> breakout in the sweep).

Entry side: direction YES -> orderbook_up, NO -> orderbook_down.

Feature families (windows in snapshots, ~0.1s each):
  (a) book snapshot at entry:
      PM_imb_top5        (sum bid5 - sum ask5)/(sum bid5 + sum ask5)
      PM_imb_top1        same for best bid/ask sizes
      PM_imb_opp_top5    PM_imb_top5 on the OPPOSITE side's book
      PM_depth_ratio_top5  sum bid5 / sum ask5
      PM_spread          (best_ask - best_bid) * 100  (cents)
      PM_wall_dist       price distance best ask -> largest ask level in top 10
      PM_slope_bid       # top-10 bid levels with size >= 2x mean top-10 bid size
  (b) PM contract-price indicators (price_up series):
      PM_mom20           pu[i]/pu[i-20] - 1
      PM_rsi14           RSI over 14 snapshot-to-snapshot pu changes (flat -> 50)
      PM_vwap_dev        pu[i] - cumulative mean of pu (no volume data -> TWAP proxy)
      PM_z50             z-score of pu over trailing 50 snapshots
      PM_dist_high       session max(pu) - pu[i]
      PM_dist_low        pu[i] - session min(pu)
  (c) orderflow over time (entry side):
      PM_bid_delta5      sum(bid5)[i] - sum(bid5)[i-1]
      PM_ask_delta5      sum(ask5)[i] - sum(ask5)[i-1]
      PM_tick_flow20     (#up-ticks - #down-ticks)/20 over last 20 pu changes
      PM_imb_slope20     (imb5[i] - imb5[i-20]) / 20
      PM_wall_persist30  fraction of last 30 snapshots whose largest ask wall
                         price equals the current one
      PM_liq_pull20      (depth10[i-20] - depth10[i]) / depth10[i-20]
                         (positive = liquidity pulled, negative = stacked)
  (d) Bookmap-style (entry side):
      PM_depth_shape     sum sizes top3 (bids+asks) / sum sizes top10
      PM_wall_side       (max bid10 size - max ask10 size) / (sum); +1 bid wall
      PM_spread_regime   spread[i] / median(valid spread[0..i]); >1 = wide

Interface (matches the sweep's per-trade feature contract):
  compute_trade_features(trades_df) -> dict[str, pd.Series]
  where trades_df has columns condition_id, opened_at, direction and each
  returned Series is aligned to trades_df.index (one value per trade). Raw
  values are returned; the sweep applies trailing-7d trade-sequence
  percentile ranks when generating configs.
"""
from __future__ import annotations

import gzip
import json
import os
import pickle
import time
import warnings
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

BOOK_DIR = Path(os.environ.get("PM_BOOK_DIR", "/config/bt_data/5m"))
CACHE_DIR = Path(os.environ.get("PM_BOOK_CACHE_DIR", "/tmp/pmbook_cache"))  # /config disk is nearly full
N_BATCHES = 16  # batch = int(condition_id) % N_BATCHES -- stable as the market set grows

TOP1, TOP5, TOP10 = 1, 5, 10
MOM_N, RSI_N, Z_N = 20, 14, 50
FLOW_N, WALL_PERSIST_N, LIQ_N = 20, 30, 20
SPREAD_MED_MIN = 5  # min valid spreads before PM_spread_regime is defined

# per-side columns inside the cached metrics matrix (up side: 0..12, down: 13..25)
SIDE_COLS = ("imb1", "imb5", "dr", "spread", "wall_dist", "slope_bid",
             "bid5", "ask5", "depth3", "depth10", "wall_px",
             "maxbid10", "maxask10")
_C = {name: i for i, name in enumerate(SIDE_COLS)}
DOWN_OFF = len(SIDE_COLS)  # 13

FEATURE_NAMES = (
    # (a) book snapshot
    "PM_imb_top5", "PM_imb_top1", "PM_imb_opp_top5", "PM_depth_ratio_top5",
    "PM_spread", "PM_wall_dist", "PM_slope_bid",
    # (b) contract-price indicators
    "PM_mom20", "PM_rsi14", "PM_vwap_dev", "PM_z50", "PM_dist_high",
    "PM_dist_low",
    # (c) orderflow over time
    "PM_bid_delta5", "PM_ask_delta5", "PM_tick_flow20", "PM_imb_slope20",
    "PM_wall_persist30", "PM_liq_pull20",
    # (d) Bookmap-style
    "PM_depth_shape", "PM_wall_side", "PM_spread_regime",
)

_MEMO: dict[str, tuple | None] = {}   # cid -> (t_f64, m_f32[n,26], pu_f32)


# ----------------------------- parsing ---------------------------------------
def _side_metrics(ob) -> tuple:
    """13 per-snapshot metrics for one side book (orderbook_up/_down or None)."""
    nan = np.nan
    if ob is None:
        return (nan,) * len(SIDE_COLS)
    bids = ob.get("bids") or ()
    asks = ob.get("asks") or ()

    b5 = sum(l["size"] for l in bids[:TOP5])
    a5 = sum(l["size"] for l in asks[:TOP5])
    imb5 = (b5 - a5) / (b5 + a5) if b5 + a5 > 0 else nan
    b1 = bids[0]["size"] if bids else 0.0
    a1 = asks[0]["size"] if asks else 0.0
    imb1 = (b1 - a1) / (b1 + a1) if b1 + a1 > 0 else nan
    dr = b5 / a5 if a5 > 0 else nan
    spread = (asks[0]["price"] - bids[0]["price"]) * 100.0 if bids and asks else nan
    if asks:
        wall = max(asks[:TOP10], key=lambda l: l["size"])  # first max on ties
        wall_px = wall["price"]
        wall_dist = wall_px - asks[0]["price"]
        maxask10 = wall["size"]
    else:
        wall_px = nan
        wall_dist = 0.0
        maxask10 = 0.0
    if bids:
        sizes = [l["size"] for l in bids[:TOP10]]
        thr = 2.0 * (sum(sizes) / len(sizes))
        slope_bid = float(sum(1 for s in sizes if s >= thr))
        maxbid10 = max(sizes)
    else:
        slope_bid = 0.0
        maxbid10 = 0.0
    depth3 = sum(l["size"] for l in bids[:3]) + sum(l["size"] for l in asks[:3])
    depth10 = sum(l["size"] for l in bids[:TOP10]) + sum(l["size"] for l in asks[:TOP10])
    return (imb1, imb5, dr, spread, wall_dist, slope_bid,
            b5, a5, depth3, depth10, wall_px, maxbid10, maxask10)


def _parse_market(cid: str):
    """Parse one raw market file -> (cid, (t_f64, m_f32[n,26], pu_f32));
    (cid, None) if missing/unreadable."""
    path = BOOK_DIR / f"{cid}.json.gz"
    if not path.exists():
        return cid, None
    try:
        with gzip.open(path, "rt") as f:
            snaps = json.load(f)
    except Exception:
        return cid, None
    if not snaps:
        return cid, None
    with warnings.catch_warnings():  # numpy warns about the 'Z' suffix; all UTC
        warnings.simplefilter("ignore")
        t = (np.array([s["time"] for s in snaps], dtype="datetime64[us]")
             .astype("int64").astype(np.float64) / 1_000_000.0)
    n = len(snaps)
    m = np.empty((n, 2 * len(SIDE_COLS)), dtype=np.float32)
    pu = np.empty(n, dtype=np.float32)
    for i, s in enumerate(snaps):
        m[i, :DOWN_OFF] = _side_metrics(s.get("orderbook_up"))
        m[i, DOWN_OFF:] = _side_metrics(s.get("orderbook_down"))
        pu[i] = s.get("price_up", np.nan)
    order = np.argsort(t, kind="stable")  # defensive; data is already ascending
    return cid, (t[order], m[order], pu[order])


# ------------------------------ cache ----------------------------------------
def _batch_key(cid: str) -> int:
    return int(cid) % N_BATCHES


def _batch_path(k: int) -> Path:
    return CACHE_DIR / f"batch_{k:02d}.pkl.gz"


def _load_memo(cids) -> None:
    need = [c for c in cids if c not in _MEMO]
    if not need:
        return
    for k in {_batch_key(c) for c in need}:
        p = _batch_path(k)
        if p.exists():
            with gzip.open(p, "rb") as f:
                _MEMO.update(pickle.load(f))


def ensure_parsed(cids, verbose: bool = True) -> None:
    """Make sure every cid has a cached parse; parse missing ones in parallel
    and append them to their (stable) batch files under /tmp."""
    _load_memo(cids)
    missing = [c for c in dict.fromkeys(cids) if c not in _MEMO]
    if not missing:
        return
    t0 = time.perf_counter()
    workers = min(4, os.cpu_count() or 1)
    parsed = {}
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for cid, rec in ex.map(_parse_market, missing, chunksize=32):
            parsed[cid] = rec
    _MEMO.update(parsed)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    by_batch: dict[int, dict] = {}
    for cid, rec in parsed.items():
        by_batch.setdefault(_batch_key(cid), {})[cid] = rec
    for k, new in by_batch.items():
        p = _batch_path(k)
        merged = {}
        if p.exists():
            with gzip.open(p, "rb") as f:
                merged = pickle.load(f)
        merged.update(new)
        tmp = p.with_suffix(".tmp")
        with gzip.open(tmp, "wb") as f:
            pickle.dump(merged, f, protocol=4)
        os.replace(tmp, p)
    if verbose:
        print(f"  pm book: parsed {len(missing)} markets in "
              f"{time.perf_counter() - t0:.1f}s ({workers} workers)")


# ----------------------- per-trade feature math ------------------------------
def _rsi(prices: np.ndarray) -> float:
    """RSI over the given snapshot-to-snapshot changes (len = RSI_N changes)."""
    d = np.diff(prices)
    gains = d[d > 0].sum()
    losses = -d[d < 0].sum()
    if losses == 0.0:
        return 50.0 if gains == 0.0 else 100.0
    return 100.0 - 100.0 / (1.0 + gains / losses)


def _fill_trade(vals: dict, p: int, i: int, ms: np.ndarray, mo: np.ndarray,
                pu: np.ndarray, spread_valid_med) -> None:
    """Compute all 22 features for one trade at as-of snapshot index i.

    ms/mo: per-snapshot metric columns for the entry / opposite side
    (views of the cached matrix). pu: price_up series. NaN-safe throughout.
    """
    nan = np.nan
    row_s = ms[i]
    # (a) book snapshot -----------------------------------------------------
    vals["PM_imb_top1"][p] = row_s[_C["imb1"]]
    vals["PM_imb_top5"][p] = row_s[_C["imb5"]]
    vals["PM_imb_opp_top5"][p] = mo[i, _C["imb5"]]
    vals["PM_depth_ratio_top5"][p] = row_s[_C["dr"]]
    vals["PM_spread"][p] = row_s[_C["spread"]]
    vals["PM_wall_dist"][p] = row_s[_C["wall_dist"]]
    vals["PM_slope_bid"][p] = row_s[_C["slope_bid"]]
    # (b) contract-price indicators (NaN-robust: ~2.7% of snapshots lack pu) --
    pu_i = pu[i]
    if i >= MOM_N and not (np.isnan(pu_i) or np.isnan(pu[i - MOM_N])) and pu[i - MOM_N] > 0:
        vals["PM_mom20"][p] = pu_i / pu[i - MOM_N] - 1.0
    if i >= RSI_N:
        w = pu[i - RSI_N:i + 1]
        if not np.isnan(w).any():  # RSI needs a complete change series
            vals["PM_rsi14"][p] = _rsi(w)
    if not np.isnan(pu_i):
        hist = pu[:i + 1]
        hist = hist[~np.isnan(hist)]
        if len(hist):
            vals["PM_vwap_dev"][p] = pu_i - hist.mean()  # TWAP proxy (no volume)
            vals["PM_dist_high"][p] = hist.max() - pu_i
            vals["PM_dist_low"][p] = pu_i - hist.min()
    if i >= Z_N - 1 and not np.isnan(pu_i):
        w = pu[i - Z_N + 1:i + 1]
        w = w[~np.isnan(w)]
        if len(w) >= Z_N // 2:
            sd = w.std()
            if sd > 0:
                vals["PM_z50"][p] = (pu_i - w.mean()) / sd
    # (c) orderflow over time -----------------------------------------------
    if i >= 1:
        vals["PM_bid_delta5"][p] = ms[i, _C["bid5"]] - ms[i - 1, _C["bid5"]]
        vals["PM_ask_delta5"][p] = ms[i, _C["ask5"]] - ms[i - 1, _C["ask5"]]
    if i >= FLOW_N:
        d = np.diff(pu[i - FLOW_N:i + 1])
        d = d[~np.isnan(d)]
        vals["PM_tick_flow20"][p] = ((d > 0).sum() - (d < 0).sum()) / FLOW_N
        a, b = ms[i, _C["imb5"]], ms[i - FLOW_N, _C["imb5"]]
        if not (np.isnan(a) or np.isnan(b)):
            vals["PM_imb_slope20"][p] = (a - b) / FLOW_N
        d0, d1 = ms[i, _C["depth10"]], ms[i - FLOW_N, _C["depth10"]]
        if d1 > 0:
            vals["PM_liq_pull20"][p] = (d1 - d0) / d1
    if i >= WALL_PERSIST_N - 1:
        cur = ms[i, _C["wall_px"]]
        if not np.isnan(cur):
            w = ms[i - WALL_PERSIST_N + 1:i + 1, _C["wall_px"]]
            vals["PM_wall_persist30"][p] = np.count_nonzero(w == cur) / WALL_PERSIST_N
    # (d) Bookmap-style -------------------------------------------------------
    d3, d10 = row_s[_C["depth3"]], row_s[_C["depth10"]]
    if d10 > 0:
        vals["PM_depth_shape"][p] = d3 / d10
    mb, ma = row_s[_C["maxbid10"]], row_s[_C["maxask10"]]
    if mb + ma > 0:
        vals["PM_wall_side"][p] = (mb - ma) / (mb + ma)
    sp = row_s[_C["spread"]]
    if not np.isnan(sp):
        med = spread_valid_med(i)
        if med is not None and med > 0:
            vals["PM_spread_regime"][p] = sp / med


# --------------------------- public interface --------------------------------
def compute_trade_features(trades_df: pd.DataFrame) -> dict[str, pd.Series]:
    """Per-trade PM book features, aligned to trades_df.index.

    trades_df must have columns: condition_id, opened_at, direction.
    Trades with a missing market file or no snapshot before entry get NaN.
    """
    cid = trades_df["condition_id"].astype(str).to_numpy()
    oa = trades_df["opened_at"].to_numpy(np.float64)
    is_up = trades_df["direction"].astype(str).str.upper().to_numpy() == "YES"
    n = len(trades_df)

    ensure_parsed(np.unique(cid))

    vals = {name: np.full(n, np.nan, dtype=np.float64) for name in FEATURE_NAMES}
    # group trade positions by market for one as-of join per market
    order = np.argsort(cid, kind="stable")
    cid_s = cid[order]
    bounds = np.searchsorted(cid_s, np.unique(cid_s))
    for lo, hi in zip(bounds, np.append(bounds[1:], len(cid_s))):
        c = cid_s[lo]
        rec = _MEMO.get(c)
        if rec is None:
            continue
        t, m, pu = rec
        pos = order[lo:hi]
        idx = np.searchsorted(t, oa[pos], side="right") - 1  # last snap <= entry
        ok = idx >= 0
        if not ok.any():
            continue
        p_all = pos[ok]
        i_all = idx[ok]
        # valid (non-NaN) spreads per side for the spread-regime median
        sp_up = m[:, _C["spread"]]
        sp_dn = m[:, DOWN_OFF + _C["spread"]]

        def _med(side_up: bool, i: int):
            sp = (sp_up if side_up else sp_dn)[:i + 1]
            sp = sp[~np.isnan(sp)]
            return float(np.median(sp)) if len(sp) >= SPREAD_MED_MIN else None

        for p, i in zip(p_all, i_all):
            up = is_up[p]
            ms = m[:, :DOWN_OFF] if up else m[:, DOWN_OFF:]
            mo = m[:, DOWN_OFF:] if up else m[:, :DOWN_OFF]
            _fill_trade(vals, p, i, ms, mo, pu,
                        lambda i, up=up: _med(up, i))

    return {name: pd.Series(v, index=trades_df.index) for name, v in vals.items()}
