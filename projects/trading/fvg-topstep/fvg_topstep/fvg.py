# CHANGE_SUMMARY
# 2026-07-16  subagent
#   - Created fvg.py with FVG, Breakaway Gap, Breaker Block, and Unicorn
#     detection on Polars DataFrames plus mitigation tracking and filters.
# WHY: Strategy entries are built on these four PD-array setups.

"""Fair Value Gap and related PD-array detectors.

All detectors operate on Polars DataFrames with ``timestamp, open, high, low,
close`` columns.  Where possible the heavy lifting is vectorized; breaker-block
sweeps are handled with a forward-only scan to avoid look-ahead bias.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime
from typing import Iterable

import polars as pl

from fvg_topstep.types import Direction, FVGZone, SetupType

logger = logging.getLogger(__name__)

_REQUIRED_COLUMNS = {"timestamp", "open", "high", "low", "close"}


def _validate_df(df: pl.DataFrame) -> pl.DataFrame:
    """Return a normalized, sorted DataFrame with lower-case column names."""
    df = df.rename({c: c.lower() for c in df.columns})
    missing = _REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(f"DataFrame missing required columns: {missing}")
    return df.sort("timestamp")


def _gap_passes_filter(
    start: float,
    end: float,
    reference: float,
    min_gap_size: float,
    min_gap_percent: float,
) -> bool:
    size = abs(end - start)
    if size < min_gap_size:
        return False
    if min_gap_percent > 0 and reference != 0:
        if (size / abs(reference)) * 100.0 < min_gap_percent:
            return False
    return True


def detect_fvgs(
    df: pl.DataFrame,
    timeframe: str = "1m",
    min_gap_size: float = 0.0,
    min_gap_percent: float = 0.0,
) -> list[FVGZone]:
    """Detect bullish and bearish Fair Value Gaps.

    A bullish FVG forms when the high of candle ``i-1`` is below the low of
    candle ``i+1``; the zone is ``[high[i-1], low[i+1]]``.

    A bearish FVG forms when the low of candle ``i-1`` is above the high of
    candle ``i+1``; the zone is ``[low[i-1], high[i+1]]``.

    Candle ``i`` is the displacement candle and the zone is confirmed once
    candle ``i+1`` has formed.
    """
    df = _validate_df(df)
    if len(df) < 3:
        return []

    shifted = df.with_columns(
        pl.col("high").shift(1).alias("prev_high"),
        pl.col("low").shift(1).alias("prev_low"),
        pl.col("low").shift(-1).alias("next_low"),
        pl.col("high").shift(-1).alias("next_high"),
        pl.col("timestamp").shift(-1).alias("formed_at"),
        pl.col("close").shift(1).alias("prev_close"),
    )

    zones: list[FVGZone] = []
    for row in shifted.iter_rows(named=True):
        prev_high = row["prev_high"]
        prev_low = row["prev_low"]
        next_low = row["next_low"]
        next_high = row["next_high"]

        if prev_high is None or next_low is None or prev_low is None or next_high is None:
            continue

        if prev_high < next_low:
            if _gap_passes_filter(
                float(prev_high),
                float(next_low),
                float(row["prev_close"]),
                min_gap_size,
                min_gap_percent,
            ):
                zones.append(
                    FVGZone(
                        direction=Direction.LONG,
                        start=float(prev_high),
                        end=float(next_low),
                        formed_at=row["formed_at"],
                        timeframe=timeframe,
                        setup_type=SetupType.FVG,
                    )
                )
            continue

        if prev_low > next_high:
            if _gap_passes_filter(
                float(prev_low),
                float(next_high),
                float(row["prev_close"]),
                min_gap_size,
                min_gap_percent,
            ):
                zones.append(
                    FVGZone(
                        direction=Direction.SHORT,
                        start=float(prev_low),
                        end=float(next_high),
                        formed_at=row["formed_at"],
                        timeframe=timeframe,
                        setup_type=SetupType.FVG,
                    )
                )

    return zones


def detect_breakaway_gaps(
    df: pl.DataFrame,
    timeframe: str = "1m",
    min_gap_size: float = 0.0,
    min_gap_percent: float = 0.0,
    swing_lookback: int = 5,
) -> list[FVGZone]:
    """Detect Breakaway Gaps: FVGs whose displacement candle closes beyond a local swing.

    A bullish breakaway gap closes above the recent local swing high; a bearish
    breakaway gap closes below the recent local swing low.  The lookback window
    defines how many bars before the displacement candle are used to locate the
    swing.
    """
    df = _validate_df(df)
    if len(df) < max(3, swing_lookback + 2):
        return []

    swing_high = (
        pl.col("high").shift(1).rolling_max(window_size=swing_lookback, min_periods=1)
    )
    swing_low = (
        pl.col("low").shift(1).rolling_min(window_size=swing_lookback, min_periods=1)
    )

    shifted = df.with_columns(
        pl.col("high").shift(1).alias("prev_high"),
        pl.col("low").shift(1).alias("prev_low"),
        pl.col("low").shift(-1).alias("next_low"),
        pl.col("high").shift(-1).alias("next_high"),
        pl.col("timestamp").shift(-1).alias("formed_at"),
        pl.col("close").shift(1).alias("prev_close"),
        pl.col("close").alias("mid_close"),
        swing_high.alias("swing_high"),
        swing_low.alias("swing_low"),
    )

    zones: list[FVGZone] = []
    for row in shifted.iter_rows(named=True):
        prev_high = row["prev_high"]
        prev_low = row["prev_low"]
        next_low = row["next_low"]
        next_high = row["next_high"]

        if prev_high is None or next_low is None or prev_low is None or next_high is None:
            continue

        if prev_high < next_low and row["mid_close"] > row["swing_high"]:
            if _gap_passes_filter(
                float(prev_high),
                float(next_low),
                float(row["prev_close"]),
                min_gap_size,
                min_gap_percent,
            ):
                zones.append(
                    FVGZone(
                        direction=Direction.LONG,
                        start=float(prev_high),
                        end=float(next_low),
                        formed_at=row["formed_at"],
                        timeframe=timeframe,
                        setup_type=SetupType.BREAKAWAY_GAP,
                    )
                )
            continue

        if prev_low > next_high and row["mid_close"] < row["swing_low"]:
            if _gap_passes_filter(
                float(prev_low),
                float(next_high),
                float(row["prev_close"]),
                min_gap_size,
                min_gap_percent,
            ):
                zones.append(
                    FVGZone(
                        direction=Direction.SHORT,
                        start=float(prev_low),
                        end=float(next_high),
                        formed_at=row["formed_at"],
                        timeframe=timeframe,
                        setup_type=SetupType.BREAKAWAY_GAP,
                    )
                )

    return zones


def detect_breaker_blocks(
    df: pl.DataFrame,
    timeframe: str = "1m",
    min_gap_size: float = 0.0,
    min_gap_percent: float = 0.0,
    max_candles_back: int = 20,
) -> list[FVGZone]:
    """Detect Breaker Blocks (change in state of delivery / CISD).

    A bearish breaker block forms after price sweeps a prior candle high and a
    candle closes below the body of the most recent bullish candle before the
    sweep.  A bullish breaker block is the inverse: a sweep below a prior
    candle low followed by a close above the body of the most recent bearish
    candle.
    """
    df = _validate_df(df)
    if len(df) < 3:
        return []

    data = df.to_dict()
    timestamps = data["timestamp"]
    opens = data["open"]
    highs = data["high"]
    lows = data["low"]
    closes = data["close"]
    n = len(timestamps)

    zones: list[FVGZone] = []
    for k in range(2, n):
        # Bearish breaker: sweep of prior candle high, then close below last bullish body.
        if highs[k] > highs[k - 1]:
            ref = None
            for j in range(k - 1, max(k - max_candles_back - 1, -1), -1):
                if closes[j] > opens[j]:
                    ref = j
                    break
            if ref is not None and closes[k] < opens[ref]:
                top = max(opens[ref], closes[ref])
                bottom = min(opens[ref], closes[ref])
                if _gap_passes_filter(
                    float(top), float(bottom), float(opens[ref]), min_gap_size, min_gap_percent
                ):
                    zones.append(
                        FVGZone(
                            direction=Direction.SHORT,
                            start=float(top),
                            end=float(bottom),
                            formed_at=timestamps[k],
                            timeframe=timeframe,
                            setup_type=SetupType.BREAKER_BLOCK,
                        )
                    )

        # Bullish breaker: sweep below prior candle low, then close above last bearish body.
        if lows[k] < lows[k - 1]:
            ref = None
            for j in range(k - 1, max(k - max_candles_back - 1, -1), -1):
                if closes[j] < opens[j]:
                    ref = j
                    break
            if ref is not None and closes[k] > opens[ref]:
                top = max(opens[ref], closes[ref])
                bottom = min(opens[ref], closes[ref])
                if _gap_passes_filter(
                    float(bottom), float(top), float(opens[ref]), min_gap_size, min_gap_percent
                ):
                    zones.append(
                        FVGZone(
                            direction=Direction.LONG,
                            start=float(bottom),
                            end=float(top),
                            formed_at=timestamps[k],
                            timeframe=timeframe,
                            setup_type=SetupType.BREAKER_BLOCK,
                        )
                    )

    return zones


def detect_unicorns(
    df: pl.DataFrame,
    timeframe: str = "1m",
    min_gap_size: float = 0.0,
    min_gap_percent: float = 0.0,
) -> list[FVGZone]:
    """Detect Unicorn setups: a Breaker Block that overlaps an active FVG.

    Highest-probability setup per strategy rules.  Returns the overlapping
    region labeled as ``SetupType.UNICORN``.
    """
    fvgs = detect_fvgs(df, timeframe, min_gap_size, min_gap_percent)
    breakers = detect_breaker_blocks(df, timeframe, min_gap_size, min_gap_percent)

    zones: list[FVGZone] = []
    for breaker in breakers:
        for fvg in fvgs:
            if breaker.direction is not fvg.direction:
                continue
            if breaker.formed_at < fvg.formed_at:
                continue
            if not _zones_overlap(breaker, fvg):
                continue
            start, end = _overlap_range(breaker, fvg)
            zones.append(
                FVGZone(
                    direction=breaker.direction,
                    start=start,
                    end=end,
                    formed_at=breaker.formed_at,
                    timeframe=timeframe,
                    setup_type=SetupType.UNICORN,
                )
            )
    return zones


def _zones_overlap(a: FVGZone, b: FVGZone) -> bool:
    """Return True if two zones of the same direction overlap."""
    a_low, a_high = (a.start, a.end) if a.is_bullish else (a.end, a.start)
    b_low, b_high = (b.start, b.end) if b.is_bullish else (b.end, b.start)
    return a_low < b_high and b_low < a_high


def _overlap_range(a: FVGZone, b: FVGZone) -> tuple[float, float]:
    a_low, a_high = (a.start, a.end) if a.is_bullish else (a.end, a.start)
    b_low, b_high = (b.start, b.end) if b.is_bullish else (b.end, b.start)
    low = max(a_low, b_low)
    high = min(a_high, b_high)
    if a.is_bullish:
        return low, high
    return high, low


def mark_mitigated(
    zones: Iterable[FVGZone],
    df: pl.DataFrame,
    use_close: bool = False,
) -> list[FVGZone]:
    """Return a new list of zones with ``mitigated`` set when price revisits one.

    By default a bullish zone is mitigated once price trades at or below the
    zone start; a bearish zone is mitigated once price trades at or above the
    zone start.  Set ``use_close=True`` to require the close to violate the same
    threshold.
    """
    df = _validate_df(df)
    if df.is_empty():
        return list(zones)

    timestamps = df["timestamp"].to_list()
    highs = df["high"].to_list()
    lows = df["low"].to_list()
    closes = df["close"].to_list()

    out: list[FVGZone] = []
    for zone in zones:
        if zone.mitigated:
            out.append(zone)
            continue

        mitigated = False
        for idx, ts in enumerate(timestamps):
            if ts <= zone.formed_at:
                continue
            if use_close:
                price = closes[idx]
                if zone.is_bullish and price <= zone.start:
                    mitigated = True
                    break
                if not zone.is_bullish and price >= zone.start:
                    mitigated = True
                    break
            else:
                if zone.is_bullish and lows[idx] <= zone.start:
                    mitigated = True
                    break
                if not zone.is_bullish and highs[idx] >= zone.start:
                    mitigated = True
                    break

        out.append(replace(zone, mitigated=mitigated))

    return out


def detect_all_setups(
    df: pl.DataFrame,
    timeframe: str = "1m",
    min_gap_size: float = 0.0,
    min_gap_percent: float = 0.0,
) -> dict[str, list[FVGZone]]:
    """Convenience bundle returning FVG, Breakaway, Breaker, and Unicorn zones."""
    return {
        "fvg": detect_fvgs(df, timeframe, min_gap_size, min_gap_percent),
        "breakaway_gap": detect_breakaway_gaps(
            df, timeframe, min_gap_size, min_gap_percent
        ),
        "breaker_block": detect_breaker_blocks(
            df, timeframe, min_gap_size, min_gap_percent
        ),
        "unicorn": detect_unicorns(df, timeframe, min_gap_size, min_gap_percent),
    }


__all__ = [
    "detect_fvgs",
    "detect_breakaway_gaps",
    "detect_breaker_blocks",
    "detect_unicorns",
    "detect_all_setups",
    "mark_mitigated",
]
