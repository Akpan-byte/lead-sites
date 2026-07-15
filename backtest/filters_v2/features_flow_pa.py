# CHANGE_SUMMARY
# 2026-07-15  kilo
#   - Ported into /config/repos/lead-sites/backtest/filters_v2/ for the GHA
#     btc_orb_feed_compare workflow. No code changes.
# WHY: Reuse the proven filters_v2 flow/PA feature library inside the repo that
#      the workflow checks out.
#
# 2026-07-12  kimi (subagent)
#   - Initial implementation: order-flow + price-action feature library for the
#     filters_v2 sweep. Exposes compute_features(bars_1m) -> dict[str, pd.Series]
#     with 64 features (42 FLOW_ across 7 TFs, 8 PA_ singles, 14 PA_ swing).
# WHY: Another agent builds the filter-sweep engine against this exact contract;
#      feature semantics, publish timing, and naming must be fixed and documented.
#
# ============================================================================
# CONVENTIONS (contract-critical — do not change without syncing the engine)
# ============================================================================
#
# INDEX / TIMING
#   - Every returned Series is indexed by the 1m bar OPEN time (float seconds),
#     identical to bars_1m["ts"]. One value per 1m bar.
#   - The value at 1m bar t is information known at the CLOSE of bar t.
#
# HIGHER-TF RESAMPLE PUBLISH TIMING (no lookahead)
#   - A tf bar covering [T, T+tf) is built from the 1m bars whose opens fall in
#     that interval. It is COMPLETE at the close of the 1m bar opened at
#     T+tf-60s (its last minute).
#   - Its value is therefore PUBLISHED at the 1m bar whose open == T+tf-60s,
#     then forward-filled until the next tf bar closes.
#   - Implementation: tf aggregates are indexed by bucket open T, re-stamped to
#     publish_ts = T + tf_seconds - 60, and reindexed onto the 1m index with
#     ffill (as-of join). For tf=1m publish_ts == T (identity).
#
# CVD CONVENTION
#   - 1m delta = (2*tbr - 1) * volume = 2*taker_buy_base_vol - volume.
#   - CVD = cumsum of 1m delta from the start of the input series.
#   - Higher-TF CVD is computed as the cumsum of per-tf-bucket deltas. Because
#     tf buckets partition the 1m series, this is EXACTLY the 1m CVD sampled at
#     each tf bar close — both definitions coincide, no approximation.
#
# SIGN CONVENTION for FLOW_cvd_div30_{tf}
#   - sign2(x) = +1 if x >= 0 else -1 (zero slope counts as agreement/up).
#   - cvd_div30 = sign2(slope(CVD,30)) * sign2(slope(close,30)) ∈ {-1, +1}.
#
# Z-SCORE CONVENTION
#   - (x - rolling_mean(w)) / rolling_std(w, ddof=0) over trailing w tf bars.
#
# PA TIME-OF-DAY CONVENTIONS
#   - PA_min_open: minutes since 00:00 UTC at bar OPEN (0..1439).
#   - PA_min_hour: minutes from bar CLOSE until the next :00 of the hour
#     (0..59; the bar opening at :59 closes exactly on the round hour -> 0).
"""Order-flow + price-action features for the filters_v2 sweep.

Contract: compute_features(bars_1m: pd.DataFrame) -> dict[str, pd.Series]
bars_1m columns: ts (float seconds, bar OPEN), open, high, low, close,
volume, quote_volume, trade_count, taker_buy_base_vol — ascending, no gaps.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# Timeframe fan-out for FLOW_ features and PA_ swing features.
TF_SECONDS: dict[str, int] = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "1d": 86400,
}

SECONDS_PER_DAY = 86400


# ---------------------------------------------------------------------------
# Vectorized helpers
# ---------------------------------------------------------------------------

def _rolling_slope(y: np.ndarray, w: int) -> np.ndarray:
    """OLS slope of y on x=0..w-1 over each trailing window of length w.

    Fully vectorized via sliding_window_view; NaN for the first w-1 points.
    Assumes y has no NaNs inside windows (true for CVD and close here).
    """
    y = np.asarray(y, dtype=np.float64)
    n = len(y)
    out = np.full(n, np.nan, dtype=np.float64)
    if n < w:
        return out
    x = np.arange(w, dtype=np.float64)
    xc = x - x.mean()
    denom = float((xc * xc).sum())
    win = np.lib.stride_tricks.sliding_window_view(y, w)  # (n-w+1, w)
    ym = win.mean(axis=1, keepdims=True)
    out[w - 1:] = ((win - ym) * xc).sum(axis=1) / denom
    return out


def _sign2(x: np.ndarray) -> np.ndarray:
    """Sign that never returns 0: +1 for x >= 0, else -1 (keeps cvd_div30 in ±1)."""
    return np.where(np.asarray(x, dtype=np.float64) >= 0.0, 1.0, -1.0)


def _publish(tf_values: pd.Series, tf_sec: int, out_index: pd.Index) -> pd.Series:
    """Re-stamp a per-tf-bar series to its publish time and ffill onto 1m index.

    tf_values index: bucket open times T (float seconds). The tf bar [T, T+tf)
    closes at the 1m bar opened at T+tf-60s, so its value appears there.
    """
    pub = tf_values.copy()
    pub.index = pub.index + (tf_sec - 60)
    return pub.reindex(out_index, method="ffill")


def _tf_bars(ts_sec: np.ndarray, df: pd.DataFrame, tf_sec: int) -> pd.DataFrame:
    """Aggregate 1m bars into tf buckets aligned to epoch multiples of tf_sec.

    Returns a frame indexed by bucket open T (float seconds) with OHLC +
    summed volume/taker_buy_base_vol/trade_count. Computed once per tf and
    reused by all features of that tf.
    """
    bucket = (ts_sec // tf_sec) * tf_sec
    g = df.groupby(bucket, sort=True)
    out = pd.DataFrame({
        "open": g["open"].first(),
        "high": g["high"].max(),
        "low": g["low"].min(),
        "close": g["close"].last(),
        "volume": g["volume"].sum(),
        "tbv": g["taker_buy_base_vol"].sum(),
        "trade_count": g["trade_count"].sum(),
    })
    out.index = out.index.astype(np.float64)
    return out


# ---------------------------------------------------------------------------
# Feature groups
# ---------------------------------------------------------------------------

def _flow_features(df: pd.DataFrame, ts_sec: np.ndarray, out_index: pd.Index) -> dict[str, pd.Series]:
    """FLOW_ features: 7 per tf × 7 tfs = 49."""
    feats: dict[str, pd.Series] = {}
    for tf, tf_sec in TF_SECONDS.items():
        b = _tf_bars(ts_sec, df, tf_sec)

        # 1. taker buy ratio ∈ [0,1]
        tbr = b["tbv"] / b["volume"]

        # 2. signed aggressive volume + CVD (cumsum of per-tf deltas ==
        #    1m CVD sampled at tf bar close; see header convention).
        delta = 2.0 * b["tbv"] - b["volume"]
        cvd = delta.cumsum()

        # 3. CVD slope over trailing 20 tf bars
        cvd_slope20 = pd.Series(_rolling_slope(cvd.to_numpy(), 20), index=b.index)

        # 4. delta z-score vs trailing 100 tf bars (population std)
        d_mean = delta.rolling(100).mean()
        d_std = delta.rolling(100).std(ddof=0)
        deltaz100 = (delta - d_mean) / d_std

        # 5. trade_count z-score vs trailing 100 tf bars
        tc = b["trade_count"]
        tcz100 = (tc - tc.rolling(100).mean()) / tc.rolling(100).std(ddof=0)

        # 6. CVD/price divergence over trailing 30 tf bars, ±1 only
        s_cvd = _rolling_slope(cvd.to_numpy(), 30)
        s_px = _rolling_slope(b["close"].to_numpy(), 30)
        cvd_div30 = pd.Series(_sign2(s_cvd) * _sign2(s_px), index=b.index)
        cvd_div30[np.isnan(s_cvd) | np.isnan(s_px)] = np.nan

        # 7. CONTINUOUS divergence: normalized-slope difference.
        #    Motivation: cvd_div30 is a ±1 flag that degenerated to a constant
        #    on the 1d tf (CVD and price trended together for the whole IS
        #    window -> bit-identical threshold ties -> selection-bias artifact,
        #    audit 2026-07-12). This variant keeps the same idea but carries
        #    magnitude: each 30-bar slope is normalized by the rolling 30-bar
        #    std of its own series (unit-free trend strength), then differenced.
        #    >0: volume flow outpacing price; <0: price outpacing flow.
        std_cvd = cvd.rolling(30).std(ddof=0).to_numpy()
        std_px = b["close"].rolling(30).std(ddof=0).to_numpy()
        with np.errstate(divide="ignore", invalid="ignore"):
            s_cvd_n = np.where(std_cvd > 0, s_cvd / std_cvd, np.nan)
            s_px_n = np.where(std_px > 0, s_px / std_px, np.nan)
        cvd_divz = pd.Series(s_cvd_n - s_px_n, index=b.index)
        cvd_divz[np.isnan(s_cvd) | np.isnan(s_px)] = np.nan

        for name, ser in (
            ("tbr", tbr),
            ("cvd", cvd),
            ("cvd_slope20", cvd_slope20),
            ("deltaz100", deltaz100),
            ("tcz100", tcz100),
            ("cvd_div30", cvd_div30),
            ("cvd_divz", cvd_divz),
        ):
            feats[f"FLOW_{name}_{tf}"] = _publish(ser, tf_sec, out_index)
    return feats


def _pa_features(df: pd.DataFrame, ts_sec: np.ndarray, out_index: pd.Index) -> dict[str, pd.Series]:
    """PA_ features: 8 daily/time singles + 2 swing features × 7 tfs = 22."""
    feats: dict[str, pd.Series] = {}
    close = df["close"].to_numpy()

    # --- Daily-anchored structure -------------------------------------------
    day_id = ts_sec // SECONDS_PER_DAY
    g = df.groupby(day_id, sort=True)
    daily_high = g["high"].max()
    daily_low = g["low"].min()
    # Prior FULLY COMPLETED UTC day: day d maps to daily stats of day d-1.
    day_ser = pd.Series(day_id)
    pdh = pd.Series(day_ser.map(daily_high.shift(1)).to_numpy(), index=out_index)
    pdl = pd.Series(day_ser.map(daily_low.shift(1)).to_numpy(), index=out_index)
    # 7/8. distance to prior-day high/low, relative to close
    # Build from numpy to avoid index-alignment surprises (pdh/pdl carry the
    # ts index, df["close"] carries RangeIndex).
    feats["PA_dist_pdhigh"] = pd.Series((close - pdh.to_numpy()) / close, index=out_index)
    feats["PA_dist_pdlow"] = pd.Series((close - pdl.to_numpy()) / close, index=out_index)
    # Raw prior-day levels (added so PA singles total 8 per the engine contract;
    # useful as absolute-price filters alongside the relative distances).
    feats["PA_pdhigh"] = pdh
    feats["PA_pdlow"] = pdl

    # 9. position within today's running range, 0..1 (today = current UTC day)
    day_hi_so_far = g["high"].cummax().to_numpy()
    day_lo_so_far = g["low"].cummin().to_numpy()
    rng = day_hi_so_far - day_lo_so_far
    day_pos = np.where(rng > 0.0, (close - day_lo_so_far) / rng, np.nan)
    feats["PA_day_pos"] = pd.Series(day_pos, index=out_index)

    # 10. signed distance to nearest $100 multiple, relative to close
    feats["PA_round100"] = pd.Series(
        (close - np.round(close / 100.0) * 100.0) / close, index=out_index
    )

    # 13/14. time-of-day (see header conventions)
    feats["PA_min_open"] = pd.Series((ts_sec % SECONDS_PER_DAY) / 60.0, index=out_index)
    feats["PA_min_hour"] = pd.Series(59.0 - ((ts_sec // 60) % 60), index=out_index)

    # --- 11/12. swing high/low distance, per tf -------------------------------
    for tf, tf_sec in TF_SECONDS.items():
        b = _tf_bars(ts_sec, df, tf_sec)
        swing_hi = b["high"].rolling(20).max()
        swing_lo = b["low"].rolling(20).min()
        feats[f"PA_swinghi20_{tf}"] = _publish((b["close"] - swing_hi) / b["close"], tf_sec, out_index)
        feats[f"PA_swinglo20_{tf}"] = _publish((b["close"] - swing_lo) / b["close"], tf_sec, out_index)
    return feats


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_features(bars_1m: pd.DataFrame) -> dict[str, pd.Series]:
    """Compute all order-flow + price-action features from 1m bars.

    Returns 64 Series (42 FLOW_ + 8 PA_ singles + 14 PA_ swing), each indexed
    by 1m bar open ts (float seconds), values known at that bar's close.
    NaN during warmup; no lookahead (see module header conventions).
    """
    out_index = pd.Index(bars_1m["ts"].to_numpy(dtype=np.float64))
    # Whole-second int timestamps for bucketing (1m opens are exact multiples
    # of 60s, so float->int64 is lossless and avoids float dust in alignment).
    ts_sec = bars_1m["ts"].to_numpy(dtype=np.int64)

    feats: dict[str, pd.Series] = {}
    feats.update(_flow_features(bars_1m, ts_sec, out_index))
    feats.update(_pa_features(bars_1m, ts_sec, out_index))
    return feats


# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import glob
    import time
    import zipfile

    t0 = time.perf_counter()

    # --- Load reference 1m zips (headerless CSV, ts in MICROseconds) ---------
    COLS = {
        0: "ts", 1: "open", 2: "high", 3: "low", 4: "close",
        5: "volume", 7: "quote_volume", 8: "trade_count", 9: "taker_buy_base_vol",
    }
    parts = []
    for zp in sorted(glob.glob("/tmp/ref_btc_1m/BTCUSDT-1m-*.zip")):
        with zipfile.ZipFile(zp) as zf:
            with zf.open(zf.namelist()[0]) as fh:
                d = pd.read_csv(fh, header=None, usecols=list(COLS))
        d.columns = list(COLS.values())
        parts.append(d)
    bars = pd.concat(parts, ignore_index=True)
    bars["ts"] = bars["ts"] / 1e6  # microseconds -> float seconds
    bars = bars.sort_values("ts").reset_index(drop=True)
    t_load = time.perf_counter()
    print(f"loaded {len(bars):,} 1m bars "
          f"({pd.to_datetime(bars['ts'].iloc[0], unit='s')} -> "
          f"{pd.to_datetime(bars['ts'].iloc[-1], unit='s')}) in {t_load - t0:.1f}s")

    # --- Compute -------------------------------------------------------------
    feats = compute_features(bars)
    t_feat = time.perf_counter()
    runtime = t_feat - t_load

    n = len(feats)
    print(f"feature count: {n} (expected 64)")
    assert n == 64, f"expected 64 features, got {n}"

    # --- NaN counts -----------------------------------------------------------
    print("NaN counts per feature:")
    for name in sorted(feats):
        s = feats[name]
        assert len(s) == len(bars), f"{name}: wrong length {len(s)}"
        assert s.index.equals(pd.Index(bars["ts"])), f"{name}: index mismatch"
        print(f"  {name:26s} {int(s.isna().sum()):7d}")

    # --- Sample values (last 3) ----------------------------------------------
    sample = ["FLOW_tbr_5m", "FLOW_cvd_1h", "FLOW_deltaz100_15m",
              "FLOW_cvd_div30_1d", "PA_day_pos", "PA_dist_pdhigh"]
    print("sample features (last 3 values):")
    for name in sample:
        print(f"  {name:22s} {np.array2string(feats[name].iloc[-3:].to_numpy(), precision=4)}")

    # --- Sanity checks --------------------------------------------------------
    tbr_bad = [k for k in feats if "_tbr_" in k
               and not ((feats[k].dropna() >= 0) & (feats[k].dropna() <= 1)).all()]
    div_bad = [k for k in feats if "_cvd_div30_" in k
               and not feats[k].dropna().isin([-1.0, 1.0]).all()]
    pos = feats["PA_day_pos"].dropna()
    pos_ok = bool(((pos >= 0) & (pos <= 1)).all())
    print(f"sanity: tbr in [0,1] -> {'OK' if not tbr_bad else tbr_bad}; "
          f"cvd_div30 only ±1 -> {'OK' if not div_bad else div_bad}; "
          f"day_pos in [0,1] -> {'OK' if pos_ok else 'FAIL'}")

    print(f"compute runtime: {runtime:.2f}s (total incl. load: {t_feat - t0:.1f}s)")
