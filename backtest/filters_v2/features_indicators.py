# CHANGE_SUMMARY
# 2026-07-15  kilo
#   - Ported into /config/repos/lead-sites/backtest/filters_v2/ for the GHA
#     btc_orb_feed_compare workflow. No code changes.
# WHY: Reuse the proven filters_v2 indicator feature library inside the repo that
#      the workflow checks out.
#
# 2026-07-13  regime-gate-phase1 (kimi subagent)
#   - Extended TF_FREQS with six longer timeframes (2h,3h,4h,6h,8h,12h), taking the
#     library from 7 -> 13 timeframes and from 16x7=112 to 16x13=208 IND_* features.
#     Same publish-timing/no-lookahead convention applies (resample origin="start_day",
#     value published at the 1m bar where the tf bar closes, then ffilled).
# WHY: Phase-1 of the regime-gate study needs higher-timeframe trend indicators so a
#      Phase-2 swarm can test "skip the 5m fade when a higher tf is strongly trending".
#      Feature-matrix recompute is done OFF the RAM-constrained VM (on the laptop) and
#      the resulting 208-col matrix is shipped back into cache/ as a hash-keyed pkl.
# 2026-07-12  filters-v2-agent
#   - Created features_indicators.py: compute_features(bars_1m) -> dict[str, pd.Series]
#     with 16 indicators x 7 timeframes (1m,3m,5m,15m,30m,1h,1d) = 112 IND_* features.
#   - __main__ harness loads /tmp/ref_btc_1m zips, runs compute_features, prints
#     feature count, NaN stats, sample values, runtime, and sanity checks.
# WHY: Indicator feature library for the trading-strategy filter sweep engine
#      (filters_v2). Engine consumes this exact interface; do not change names/index.
"""
features_indicators.py — indicator feature library for the filters_v2 sweep.

Contract
--------
compute_features(bars_1m: pd.DataFrame) -> dict[str, pd.Series]

bars_1m columns: ts (float seconds, bar OPEN time, ascending, no gaps), open, high,
low, close, volume, quote_volume, trade_count, taker_buy_base_vol.

Each returned Series is indexed by 1m bar OPEN ts (float seconds, same grid as the
input) and holds one value per 1m bar: the feature value known at the CLOSE of that
1m bar. NaN during indicator warmup (min_periods == period) — the engine treats NaN
as "no signal".

PUBLISH-TIMING CONVENTION (no lookahead)
----------------------------------------
- 1m tf: computed directly on 1m bars; the value of bar with open ts t is published
  at index t (it uses only that bar and earlier ones, so it is known at t's close).
- Higher tfs (3m..1d): bars are resampled with closed="left", label="left",
  origin="start_day" (daily bars anchored at 00:00 UTC). A tf bar covering
  [S, S+F) CLOSES at S+F, i.e. at the close of the 1m bar whose open ts is
  S+F-60s. The tf bar's indicator value is therefore PUBLISHED at 1m index
  S+F-60s, then forward-filled on the 1m grid until the next tf bar closes.
  Example: the 15m bar covering 12:00-12:15 publishes at the 1m bar with open
  ts 12:14. Values are never published before the tf bar's close, so no lookahead.
- A trailing partial tf bar at the end of the data would publish past the last 1m
  ts and is simply dropped (its predecessor's value stays forward-filled).

Indicator definitions (all standard; warmup = the indicator's own period):
- RSI(14): Wilder smoothing (ewm alpha=1/14). Range 0..100.
- ADX(14): Wilder-smoothed TR/+DM/-DM -> +DI/-DI -> DX -> Wilder-smoothed ADX.
- Bollinger (20, 2 sigma, population std ddof=0): bbw20 = (upper-lower)/mid;
  bbpct20 = (close-lower)/(upper-lower).
- Keltner (EMA20, 1.5xATR20 Wilder): kcw20 = (upper-lower)/mid = 3*ATR20/EMA20.
- donch20 = (close - min(low,20)) / (max(high,20) - min(low,20)).
- chand22 = (close - (max(high,22) - 3*ATR22)) / ATR22.
- atr14pct = ATR14(Wilder)/close.
- rv30/rv60: std (ddof=0) of 1-bar log returns over trailing 30/60 bars of that tf,
  raw (not annualized).
- mom15/mom30/mom60: close/close.shift(N) - 1 for N bars of that tf.
- z50 = (close - mean50)/std50 (population std).
- vwapd = (close - vwap)/vwap, vwapside = sign(close - vwap) as +1/-1 (ties -> +1).
  VWAP is cumulative sum(tp*volume)/sum(volume), tp=(h+l+c)/3 of the tf's bars,
  reset at each 00:00 UTC (grouped by UTC day of the tf bar's START). For the 1d tf
  the VWAP is instead the intraday 1m-computed daily VWAP (its final value of the
  day), published at day close, per spec.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# (tf label, pandas resample frequency or None for native 1m)
# 13 timeframes: 1m..1h plus the six longer tfs (2h..12h) added for the regime-gate
# study, then 1d. 16 indicators x 13 tfs = 208 IND_* features.
TF_FREQS = [
    ("1m", None),
    ("3m", "3min"),
    ("5m", "5min"),
    ("15m", "15min"),
    ("30m", "30min"),
    ("1h", "1h"),
    ("2h", "2h"),
    ("3h", "3h"),
    ("4h", "4h"),
    ("6h", "6h"),
    ("8h", "8h"),
    ("12h", "12h"),
    ("1d", "1D"),
]

_OHLCV_AGG = {
    "open": "first",
    "high": "max",
    "low": "min",
    "close": "last",
    "volume": "sum",
    "quote_volume": "sum",
    "trade_count": "sum",
    "taker_buy_base_vol": "sum",
}


def _wilder(s: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing == ewm with alpha = 1/period (adjust=False)."""
    return s.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()


def _true_range(df: pd.DataFrame) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    return pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)


def _atr(df: pd.DataFrame, period: int) -> pd.Series:
    return _wilder(_true_range(df), period)


def _indicators(df: pd.DataFrame) -> dict[str, pd.Series]:
    """Compute the 16 indicators on one timeframe's OHLCV frame (DatetimeIndex, UTC)."""
    out: dict[str, pd.Series] = {}
    c, h, l, v = df["close"], df["high"], df["low"], df["volume"]

    # 1. RSI(14), Wilder
    delta = c.diff()
    avg_gain = _wilder(delta.clip(lower=0.0), 14)
    avg_loss = _wilder((-delta).clip(lower=0.0), 14)
    rs = avg_gain / avg_loss
    out["rsi14"] = 100.0 - 100.0 / (1.0 + rs)

    # 2. ADX(14), Wilder
    atr14 = _atr(df, 14)
    up_move = h - h.shift(1)
    dn_move = l.shift(1) - l
    plus_dm = up_move.where((up_move > dn_move) & (up_move > 0.0), 0.0)
    minus_dm = dn_move.where((dn_move > up_move) & (dn_move > 0.0), 0.0)
    plus_di = 100.0 * _wilder(plus_dm, 14) / atr14
    minus_di = 100.0 * _wilder(minus_dm, 14) / atr14
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    out["adx14"] = _wilder(dx, 14)

    # 3/4. Bollinger Bands (20, 2 sigma), width-% and %b
    mid = c.rolling(20, min_periods=20).mean()
    sd = c.rolling(20, min_periods=20).std(ddof=0)
    upper, lower = mid + 2.0 * sd, mid - 2.0 * sd
    out["bbw20"] = (upper - lower) / mid
    out["bbpct20"] = (c - lower) / (upper - lower)

    # 5. Keltner Channel (EMA20, 1.5xATR20) width as % of mid
    ema20 = c.ewm(span=20, min_periods=20, adjust=False).mean()
    atr20 = _atr(df, 20)
    out["kcw20"] = (2.0 * 1.5 * atr20) / ema20

    # 6. Donchian position (20)
    hi20 = h.rolling(20, min_periods=20).max()
    lo20 = l.rolling(20, min_periods=20).min()
    out["donch20"] = (c - lo20) / (hi20 - lo20)

    # 7. Chandelier distance (22, 3xATR22)
    hi22 = h.rolling(22, min_periods=22).max()
    atr22 = _atr(df, 22)
    out["chand22"] = (c - (hi22 - 3.0 * atr22)) / atr22

    # 8. ATR(14) as % of close
    out["atr14pct"] = atr14 / c

    # 9/10. Realized vol of 1-bar log returns, trailing 30/60 bars, raw
    log_ret = np.log(c / c.shift(1))
    out["rv30"] = log_ret.rolling(30, min_periods=30).std(ddof=0)
    out["rv60"] = log_ret.rolling(60, min_periods=60).std(ddof=0)

    # 11/12/13. Momentum over N bars of this tf
    out["mom15"] = c / c.shift(15) - 1.0
    out["mom30"] = c / c.shift(30) - 1.0
    out["mom60"] = c / c.shift(60) - 1.0

    # 14. Close z-score vs trailing 50 bars
    m50 = c.rolling(50, min_periods=50).mean()
    s50 = c.rolling(50, min_periods=50).std(ddof=0)
    out["z50"] = (c - m50) / s50

    # 15/16. Daily-anchored VWAP distance / side (reset each 00:00 UTC).
    # For the 1d tf this per-bar version is degenerate and gets overwritten in
    # compute_features with the 1m-computed daily VWAP.
    tp = (h + l + c) / 3.0
    day = df.index.floor("D")
    cum_pv = (tp * v).groupby(day).cumsum()
    cum_v = v.groupby(day).cumsum().replace(0.0, np.nan)
    vwap = cum_pv / cum_v
    diff = c - vwap
    out["vwapd"] = diff / vwap
    # +1/-1, ties -> +1; NaN while VWAP is in warmup
    out["vwapside"] = pd.Series(
        np.where(diff >= 0.0, 1.0, -1.0), index=df.index
    ).where(vwap.notna())

    return out


def _publish(
    values: np.ndarray, publish_dt: pd.DatetimeIndex, grid: pd.Index
) -> pd.Series:
    """Map tf-bar values onto the 1m grid: a value appears at the 1m bar where the
    tf bar closes (publish_dt), then forward-fills until the next tf bar closes.

    Matching is done on INTEGER seconds to avoid float-epsilon mismatches between
    the resampled timestamps and the input grid; the returned Series keeps the
    original float-seconds 1m grid as its index. (Note: datetime64[s] conversion is
    used instead of .astype("int64"), whose unit semantics changed in pandas 3.0.)
    """
    grid_sec = pd.Index(np.round(grid.to_numpy()).astype(np.int64))
    pub_sec = publish_dt.to_numpy(dtype="datetime64[s]").astype(np.int64)
    s = pd.Series(values, index=pub_sec)
    out = s.reindex(grid_sec).ffill()
    out.index = grid
    return out


def compute_features(bars_1m: pd.DataFrame) -> dict[str, pd.Series]:
    """Compute 16 indicators x 13 timeframes = 208 features on the 1m grid.

    See module docstring for the exact publish-timing convention (no lookahead).
    """
    grid = pd.Index(bars_1m["ts"].to_numpy(), name="ts")  # float seconds, 1m opens
    dt_index = pd.to_datetime(bars_1m["ts"].to_numpy(), unit="s", utc=True)
    df1m = bars_1m.drop(columns=["ts"]).copy()
    df1m.index = dt_index

    features: dict[str, pd.Series] = {}

    for tf, freq in TF_FREQS:
        if freq is None:
            tdf = df1m
            publish_dt = tdf.index  # 1m bar publishes at itself (known at its close)
        else:
            tdf = (
                df1m.resample(freq, closed="left", label="left", origin="start_day")
                .agg(_OHLCV_AGG)
                .dropna(subset=["close"])
            )
            # tf bar [S, S+F) closes at S+F == close of the 1m bar opened at S+F-60s
            publish_dt = tdf.index + pd.Timedelta(freq) - pd.Timedelta(minutes=1)
        for name, s in _indicators(tdf).items():
            features[f"IND_{name}_{tf}"] = _publish(s.to_numpy(), publish_dt, grid)

    # 1d VWAP features: use the intraday 1m-computed daily VWAP (final value of the
    # day), published at day close (1m bar with open ts 23:59 of that day).
    tp1 = (df1m["high"] + df1m["low"] + df1m["close"]) / 3.0
    day1 = df1m.index.floor("D")
    cum_pv1 = (tp1 * df1m["volume"]).groupby(day1).cumsum()
    cum_v1 = df1m["volume"].groupby(day1).cumsum().replace(0.0, np.nan)
    vwap1m = cum_pv1 / cum_v1
    day_close = df1m["close"].groupby(day1).last()
    day_vwap = vwap1m.groupby(day1).last()
    day_publish = day_close.index + pd.Timedelta("1D") - pd.Timedelta(minutes=1)
    d_diff = (day_close - day_vwap).to_numpy()
    features["IND_vwapd_1d"] = _publish(
        d_diff / day_vwap.to_numpy(), day_publish, grid
    )
    features["IND_vwapside_1d"] = _publish(
        np.where(d_diff >= 0.0, 1.0, -1.0), day_publish, grid
    )

    return features


if __name__ == "__main__":
    import glob
    import time
    import zipfile

    t0 = time.time()
    frames = []
    for zpath in sorted(glob.glob("/tmp/ref_btc_1m/BTCUSDT-1m-*.zip")):
        with zipfile.ZipFile(zpath) as zf:
            with zf.open(zf.namelist()[0]) as f:
                raw = pd.read_csv(f, header=None)
        frames.append(
            pd.DataFrame(
                {
                    "ts": raw[0].astype(np.float64) / 1e6,  # open_time, microseconds -> s
                    "open": raw[1].astype(np.float64),
                    "high": raw[2].astype(np.float64),
                    "low": raw[3].astype(np.float64),
                    "close": raw[4].astype(np.float64),
                    "volume": raw[5].astype(np.float64),
                    "quote_volume": raw[7].astype(np.float64),
                    "trade_count": raw[8].astype(np.float64),
                    "taker_buy_base_vol": raw[9].astype(np.float64),
                }
            )
        )
    bars_1m = (
        pd.concat(frames, ignore_index=True).sort_values("ts").reset_index(drop=True)
    )
    print(f"loaded {len(bars_1m)} 1m bars in {time.time() - t0:.1f}s")

    t1 = time.time()
    feats = compute_features(bars_1m)
    runtime = time.time() - t1

    print(f"feature count: {len(feats)} (expected 16x13 = 208)")
    print(f"compute_features runtime: {runtime:.2f}s")

    samples = [
        "IND_rsi14_1m",
        "IND_adx14_15m",
        "IND_bbw20_1h",
        "IND_vwapd_5m",
        "IND_vwapside_1d",
    ]
    for name in samples:
        s = feats[name]
        print(
            f"{name}: NaNs={int(s.isna().sum())}/{len(s)}  "
            f"tail3={np.round(s.dropna().to_numpy()[-3:], 6).tolist()}"
        )

    # sanity checks
    all_nan = [k for k, s in feats.items() if s.notna().sum() == 0]
    print("all-NaN columns:", all_nan if all_nan else "none")
    for tf, _ in TF_FREQS:
        r = feats[f"IND_rsi14_{tf}"].dropna()
        lo, hi = float(r.min()), float(r.max())
        assert 0.0 <= lo and hi <= 100.0, f"rsi14_{tf} out of range: {lo}..{hi}"
        vs = feats[f"IND_vwapside_{tf}"].dropna().unique()
        assert set(vs) <= {-1.0, 1.0}, f"vwapside_{tf} bad values: {vs}"
    print("sanity: RSI within [0,100] on all tfs, vwapside in {-1,+1} on all tfs — OK")
