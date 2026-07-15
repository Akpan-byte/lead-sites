# CHANGE_SUMMARY
# 2026-07-11  kimi
#   - Causal Binance 1m reference feed for the BTC-5m backtest (sandbox only).
#   - opening_range(): 09:30 ET-anchored OR high/low over or_seconds, from 1m bars.
#   - closes_upto()/bars_upto(): strictly causal (bar open_time < t) series for
#     indicator strategies that otherwise "bootstrap bars" over REST in live.
# 2026-07-15  kimi
#   - Added BT_REF_FEED selector ("binance" or "hyperliquid", default "binance").
#   - Hyperliquid path loads from BT_REF_HL_1M_DIR (default /tmp/ref_hl_1m) using
#     BTCUSDT-1m-YYYY-MM-DD.zip files in the same CSV layout as Binance.
# WHY: A parallel Hyperliquid 1m reference dataset is being built; the backtest
#      engine must be able to switch the causal reference substrate without
#      changing any caller code.
"""Causal 1m reference feed selector (backtest-only helper).

NOT live code. Provides the reference price substrate that the sandbox driver
uses in place of the live Hyperliquid trade archive (for the daily_orb_v5 opening
range) and the REST "bootstrap bars" calls (for indicator strategies).

The feed source is selected at load time by the BT_REF_FEED environment variable:
  "binance" (default) -> BT_REF_BTC_1M_DIR -> /tmp/ref_btc_1m
  "hyperliquid"       -> BT_REF_HL_1M_DIR  -> /tmp/ref_hl_1m
Both feeds use the same daily zip format:
  BTCUSDT-1m-YYYY-MM-DD.zip -> one CSV, 1440 1m rows/day, no header:
    open_time_us, open, high, low, close, volume, close_time_us, quote_volume,
    trades, taker_buy_base, taker_buy_quote, ignore
"""
from __future__ import annotations

import csv
import glob
import os
import zipfile
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

# Lazy-loaded singleton: list of (open_time_ms, open, high, low, close), sorted.
_ROWS: list[tuple[int, float, float, float, float]] = []
_LOADED = False
_SRC_DIR: str | None = None
_FEED: str = "binance"

_DEFAULT_FEEDS = {
    "binance": "/tmp/ref_btc_1m",
    "hyperliquid": "/tmp/ref_hl_1m",
}
_FEED_DIR_ENVS = {
    "binance": "BT_REF_BTC_1M_DIR",
    "hyperliquid": "BT_REF_HL_1M_DIR",
}
_VALID_FEEDS = set(_DEFAULT_FEEDS)


def _read_zip(path: str) -> list[tuple[int, float, float, float, float]]:
    out: list[tuple[int, float, float, float, float]] = []
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if not name.endswith(".csv"):
                continue
            with z.open(name) as fh:
                text = (b.decode("utf-8", "replace") for b in fh)
                for row in csv.reader(text):
                    if not row or len(row) < 5:
                        continue
                    try:
                        ot_ms = int(row[0]) // 1000  # micro -> milli seconds
                        o = float(row[1]); h = float(row[2])
                        lo = float(row[3]); c = float(row[4])
                    except ValueError:
                        continue
                    out.append((ot_ms, o, h, lo, c))
    return out


def load(src_dir: str | None = None, feed: str | None = None) -> int:
    """Load (or reload) all daily zips. Returns row count.

    If src_dir is given it is used directly. Otherwise feed (or the BT_REF_FEED
    environment variable) selects the default directory and environment variable
    override: "binance" (default) uses BT_REF_BTC_1M_DIR -> /tmp/ref_btc_1m,
    "hyperliquid" uses BT_REF_HL_1M_DIR -> /tmp/ref_hl_1m.
    """
    global _ROWS, _LOADED, _SRC_DIR, _FEED
    if src_dir is None:
        if feed is None:
            feed = os.environ.get("BT_REF_FEED", "binance").lower().strip()
        if feed not in _VALID_FEEDS:
            raise ValueError(
                f"BT_REF_FEED must be one of {_VALID_FEEDS}, got {feed!r}"
            )
        src_dir = os.environ.get(_FEED_DIR_ENVS[feed], _DEFAULT_FEEDS[feed])
        _FEED = feed
    else:
        _FEED = feed or "explicit"
    rows: list[tuple[int, float, float, float, float]] = []
    for path in sorted(glob.glob(os.path.join(src_dir, "*.zip"))):
        rows.extend(_read_zip(path))
    rows.sort(key=lambda r: r[0])
    _ROWS = rows
    _SRC_DIR = src_dir
    _LOADED = True
    return len(_ROWS)


def _ensure() -> None:
    if not _LOADED:
        load()


def _or_window_ms(d: date, or_seconds: int) -> tuple[int, int]:
    """[09:30:00 ET, 09:30:00 + or_seconds) of date d, as UTC epoch ms.

    Matches daily_orb_v5._window_ms (inclusive start, exclusive end)."""
    open_et = datetime(d.year, d.month, d.day, 9, 30, 0, tzinfo=ET)
    close_et = open_et + timedelta(seconds=or_seconds)
    return (
        int(open_et.astimezone(UTC).timestamp() * 1000),
        int(close_et.astimezone(UTC).timestamp() * 1000),
    )


def opening_range(asset: str, d: date, or_seconds: int) -> tuple[float, float] | None:
    """OR high/low over [09:30 ET, +or_seconds) for date d, from 1m bars.

    A 1m bar belongs to the window if its open_time_ms is in [open_ms, close_ms).
    Returns None if no bars fall in the window (data gap)."""
    _ensure()
    open_ms, close_ms = _or_window_ms(d, or_seconds)
    hi = float("-inf"); lo = float("inf"); n = 0
    for ot_ms, _o, h, lo_, _c in _ROWS:
        if ot_ms < open_ms:
            continue
        if ot_ms >= close_ms:
            break
        if h > hi:
            hi = h
        if lo_ < lo:
            lo = lo_
        n += 1
    if n == 0:
        return None
    return (hi, lo)


def bars_upto(asset: str, t_ms: int, n: int = 2000) -> list[tuple[int, float, float, float, float]]:
    """Up to n bars with open_time_ms < t_ms (strictly causal), oldest->newest."""
    _ensure()
    # _ROWS is sorted by open_time; bisect for the cut point.
    import bisect
    keys = [r[0] for r in _ROWS]
    i = bisect.bisect_left(keys, t_ms)  # first index with open_time >= t_ms
    start = max(0, i - n)
    return _ROWS[start:i]


def resample_upto(asset: str, t_ms: int, tf_min: int, limit: int = 400) -> list[tuple[float, float, float, float]]:
    """Last `limit` complete tf_min-minute OHLC bars strictly before t_ms,
    resampled from 1m bars. A tf bucket is complete only if all tf_min 1m bars
    exist and the bucket's final 1m bar closed at or before t_ms (causal).
    Returns [(open, high, low, close), ...] oldest->newest."""
    _ensure()
    span = tf_min * 60000
    rows = bars_upto(asset, t_ms, n=limit * tf_min + tf_min)
    buckets: dict[int, list] = {}
    order: list[int] = []
    for ot_ms, o, h, lo, c in rows:
        b = ot_ms // span
        if b not in buckets:
            buckets[b] = []
            order.append(b)
        buckets[b].append((ot_ms, o, h, lo, c))
    out = []
    for b in order:
        rows_b = buckets[b]
        if len(rows_b) != tf_min:
            continue                      # gap -> incomplete bucket, skip
        last_ot = rows_b[-1][0]
        if last_ot + 60000 > t_ms:
            continue                      # last 1m bar not closed before t -> not causal
        o = rows_b[0][1]
        h = max(r[2] for r in rows_b)
        lo = min(r[3] for r in rows_b)
        c = rows_b[-1][4]
        out.append((o, h, lo, c))
    return out[-limit:]


def closes_upto(asset: str, t_ms: int, n: int = 2000) -> list[float]:
    """Causal close series (open_time < t_ms), oldest->newest, last n."""
    return [r[4] for r in bars_upto(asset, t_ms, n)]


def price_at(asset: str, t_ms: int) -> float | None:
    """Last reference close price with open_time_ms <= t_ms (causal)."""
    _ensure()
    import bisect
    keys = [r[0] for r in _ROWS]
    i = bisect.bisect_right(keys, t_ms) - 1
    if i < 0:
        return None
    return _ROWS[i][4]


def coverage() -> tuple[int, int, int] | None:
    """(n_rows, first_open_ms, last_open_ms) or None if empty."""
    _ensure()
    if not _ROWS:
        return None
    return (len(_ROWS), _ROWS[0][0], _ROWS[-1][0])


if __name__ == "__main__":
    total = load()
    print("loaded rows:", total, "coverage:", coverage())
    # Sanity: 09:30 ET OR for a mid-range date, all tf or_seconds.
    test = date(2026, 6, 15)
    for tf, secs in (("1m", 60), ("5m", 300), ("15m", 900), ("1h", 3600)):
        print(f"OR {tf:>3s} ({secs:4d}s) {test}:", opening_range("BTC", test, secs))
    # Causal check: closes strictly before t.
    t = int(datetime(2026, 6, 15, 13, 35, 0, tzinfo=ET).astimezone(UTC).timestamp() * 1000)
    cs = closes_upto("BTC", t, 5)
    bs = bars_upto("BTC", t, 5)
    print("last 5 causal closes before 09:35 ET:", [round(c, 2) for c in cs])
    print("their open_times (ms):", [b[0] for b in bs], "< t", t,
          "-> causal:", all(b[0] < t for b in bs))
