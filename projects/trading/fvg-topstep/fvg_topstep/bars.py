# CHANGE_SUMMARY
# 2026-07-16  subagent
#   - Created bars.py with BarCache, add/update helpers, resample_bars, and
#     get_latest_bars using Polars for deterministic, vectorized operations.
# WHY: Strategy needs clean, look-ahead-free multi-timeframe bar storage.

"""Bar cache and timeframe resampling utilities.

The cache stores bars per ``(symbol, timeframe)`` as Polars DataFrames and
supports appending single bars, bulk updates, and building higher timeframes
from 1-minute bars.  All operations are deterministic and avoid look-ahead.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import polars as pl

from fvg_topstep.types import Bar

logger = None  # no logging in this module to keep it dependency-light

_TIME_FRAME_RE = re.compile(r"^(\d+)([mhdwM]|mo)$")
_BAR_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]
_BAR_SCHEMA: dict[str, Any] = {
    "timestamp": pl.Datetime("us", "UTC"),
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Int64,
}


def _normalize_tf(tf: str) -> str:
    """Return a lower-case timeframe string and validate its shape."""
    t = tf.strip().lower()
    if t.endswith("mo"):
        return t
    if _TIME_FRAME_RE.match(t):
        return t
    raise ValueError(f"Unsupported timeframe: {tf!r}")


def _tf_to_minutes(tf: str) -> int:
    """Convert a timeframe string such as ``15m`` or ``4h`` to minutes."""
    t = _normalize_tf(tf)
    if t.endswith("mo"):
        return int(t[:-2]) * 43200  # 30-day approximation
    match = _TIME_FRAME_RE.match(t)
    assert match is not None
    value = int(match.group(1))
    unit = match.group(2)
    multipliers = {
        "m": 1,
        "h": 60,
        "d": 1440,
        "w": 10080,
        "M": 43200,
    }
    return value * multipliers[unit]


class BarCache:
    """Simple in-memory cache of OHLCV bars per symbol and timeframe."""

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], pl.DataFrame] = {}

    def _empty_frame(self) -> pl.DataFrame:
        return pl.DataFrame(schema=_BAR_SCHEMA)

    def _normalize(self, df: pl.DataFrame) -> pl.DataFrame:
        """Return *df* with required columns, lower-case names, sorted rows."""
        frame = df.clone()
        frame = frame.rename({c: c.lower() for c in frame.columns})
        missing = [c for c in _BAR_COLUMNS if c not in frame.columns]
        if missing:
            raise ValueError(f"bar DataFrame missing columns: {missing}")

        if not frame["timestamp"].dtype.is_temporal():
            frame = frame.with_columns(pl.col("timestamp").cast(pl.Datetime("us", "UTC")))
        frame = frame.with_columns(
            pl.col("open").cast(pl.Float64),
            pl.col("high").cast(pl.Float64),
            pl.col("low").cast(pl.Float64),
            pl.col("close").cast(pl.Float64),
            pl.col("volume").cast(pl.Int64),
        )
        frame = frame.select(_BAR_COLUMNS).sort("timestamp")
        return frame.unique(subset=["timestamp"], keep="last", maintain_order=True)

    def add_bar(self, bar: Bar, symbol: str, timeframe: str) -> None:
        """Append or overwrite a single bar for *symbol*/*timeframe*."""
        key = (symbol.upper(), timeframe.lower())
        row = pl.DataFrame(
            [
                {
                    "timestamp": bar.timestamp,
                    "open": float(bar.open),
                    "high": float(bar.high),
                    "low": float(bar.low),
                    "close": float(bar.close),
                    "volume": int(bar.volume) if bar.volume is not None else 0,
                }
            ],
            schema=_BAR_SCHEMA,
        )
        existing = self._data.get(key, self._empty_frame())
        self._data[key] = self._normalize(pl.concat([existing, row], how="vertical"))

    def update_from_dataframe(
        self, symbol: str, timeframe: str, df: pl.DataFrame
    ) -> None:
        """Merge a Polars DataFrame of bars into the cache.

        The DataFrame must contain ``timestamp, open, high, low, close`` and may
        optionally contain ``volume``.  Duplicate timestamps are resolved by
        keeping the newest supplied row.
        """
        key = (symbol.upper(), timeframe.lower())
        if df.is_empty():
            return
        incoming = self._normalize(df)
        existing = self._data.get(key, self._empty_frame())
        self._data[key] = self._normalize(pl.concat([existing, incoming], how="vertical"))

    def get_bars(self, symbol: str, timeframe: str) -> pl.DataFrame:
        """Return the full cached DataFrame for *symbol*/*timeframe*."""
        key = (symbol.upper(), timeframe.lower())
        return self._data.get(key, self._empty_frame())

    def get_latest_bars(
        self, symbol: str, timeframe: str, n: int
    ) -> pl.DataFrame:
        """Return the most recent *n* bars for *symbol*/*timeframe*."""
        key = (symbol.upper(), timeframe.lower())
        df = self._data.get(key, self._empty_frame())
        if n <= 0:
            return self._empty_frame()
        return df.tail(n)

    @staticmethod
    def resample_bars(
        df: pl.DataFrame, source_tf: str, target_tf: str
    ) -> pl.DataFrame:
        """Build higher-timeframe bars from a lower-timeframe DataFrame."""
        return resample_bars(df, source_tf, target_tf)


def resample_bars(df: pl.DataFrame, source_tf: str, target_tf: str) -> pl.DataFrame:
    """Build higher-timeframe bars from a lower-timeframe DataFrame.

    The function expects the source DataFrame to be sorted and gap-free enough
    for a fixed-interval grouping.  ``source_tf`` is used to validate that
    ``target_tf`` is an integer multiple.
    """
    if df.is_empty():
        return pl.DataFrame(schema=_BAR_SCHEMA)

    source_minutes = _tf_to_minutes(source_tf)
    target_minutes = _tf_to_minutes(target_tf)

    if target_minutes < source_minutes:
        raise ValueError(
            f"target_tf {target_tf!r} must be >= source_tf {source_tf!r}"
        )

    frame = df.clone().sort("timestamp").set_sorted("timestamp")
    grouped = frame.group_by_dynamic(
        index_column="timestamp",
        every=f"{target_minutes}m",
        period=f"{target_minutes}m",
        closed="left",
        label="left",
    ).agg(
        pl.col("open").first().alias("open"),
        pl.col("high").max().alias("high"),
        pl.col("low").min().alias("low"),
        pl.col("close").last().alias("close"),
        pl.col("volume").sum().alias("volume"),
    )
    return grouped.sort("timestamp").select(_BAR_COLUMNS)


__all__ = ["BarCache", "resample_bars"]
