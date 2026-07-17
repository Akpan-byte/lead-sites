# CHANGE_SUMMARY
# 2026-07-16  subagent
#   - Created clock.py with now_et, is_killzone, is_news_blackout, and
#     is_session_open helpers for the FVG Topstep engine.
# WHY: Strategy filters entries by ET killzones, scheduled news blackouts, and
#      approximate CME futures session hours.

"""Time, killzone, news blackout, and session helpers.

All datetime objects are assumed timezone-aware when ET is relevant.  The
module uses ``zoneinfo`` so no external timezone library is required on
Python 3.9+.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")

#: Approximate CME Globex maintenance window (ET) observed by all asset classes.
_MAINTENANCE_START = time(17, 0)
_MAINTENANCE_END = time(18, 0)

_EQUITY_INDEX = {
    "ES", "NQ", "YM", "RTY", "M2K", "MNQ", "MES", "MYM", "NKD",
}
_METALS = {"GC", "SI", "HG", "MGC", "SIL", "PL"}
_ENERGY = {"CL", "NG", "QM", "RB", "HO", "MBT", "MET"}
_AGRICULTURE = {"ZC", "ZW", "ZS", "LE", "HE"}


@dataclass(frozen=True, slots=True)
class NewsEvent:
    """A scheduled macroeconomic release that triggers a news blackout."""

    timestamp: datetime
    name: str
    symbols: tuple[str, ...] | None = None


# Placeholder hard-coded news calendar.  Populate this with the actual release
# dates/times for the year being traded.  When no events exist for a year the
# function falls back to ``False`` so the engine does not silently block entries.
_DEFAULT_NEWS_EVENTS: dict[int, list[NewsEvent]] = {
    # Example entries (uncomment and keep current before going live):
    # 2026: [
    #     NewsEvent(datetime(2026, 1, 9, 8, 30, tzinfo=ET), "NFP"),
    #     NewsEvent(datetime(2026, 1, 14, 8, 30, tzinfo=ET), "CPI"),
    #     NewsEvent(datetime(2026, 1, 28, 14, 0, tzinfo=ET), "FOMC"),
    # ],
}


def now_et() -> datetime:
    """Return the current wall-clock time in America/New_York."""
    return datetime.now(ET)


def _parse_hhmm(value: str) -> time:
    """Parse ``HH:MM`` into a :class:`time`."""
    h, m = value.split(":")
    return time(int(h), int(m))


def is_killzone(dt: datetime, killzones: list[tuple[str, str]]) -> bool:
    """Return ``True`` if *dt* falls inside any configured killzone window.

    Killzones are supplied as ``[("HH:MM", "HH:MM"), ...]`` in ET.  Windows that
    wrap midnight (e.g. ``18:00-17:00``) are treated as overnight sessions.
    """
    t = (dt.astimezone(ET) if dt.tzinfo else dt.replace(tzinfo=ET)).time()
    for start_str, end_str in killzones:
        start = _parse_hhmm(start_str)
        end = _parse_hhmm(end_str)
        if start < end:
            if start <= t <= end:
                return True
        else:
            if t >= start or t <= end:
                return True
    return False


def is_news_blackout(
    dt: datetime,
    symbol: str | None = None,
    events: list[NewsEvent] | None = None,
    window: timedelta = timedelta(minutes=30),
) -> bool:
    """Return ``True`` if *dt* is inside a news blackout window.

    If *events* is provided it is used directly.  Otherwise the function looks
    for hard-coded events for *dt.year*.  If no hard-coded data exists the
    function returns ``False`` so the caller can supply a calendar feed instead.

    Parameters
    ----------
    dt:
        Time to test (naive times are assumed ET).
    symbol:
        Optional symbol; if an event lists specific symbols the blackout only
        applies when the symbol is in that list.
    events:
        Optional caller-supplied list of blackout events.
    window:
        +/- window around each release time (default 30 minutes).
    """
    dt = dt.astimezone(ET) if dt.tzinfo else dt.replace(tzinfo=ET)

    if events is None:
        events = _DEFAULT_NEWS_EVENTS.get(dt.year, [])
        if not events:
            return False

    for event in events:
        event_dt = event.timestamp.astimezone(ET)
        if abs((dt - event_dt).total_seconds()) <= window.total_seconds():
            if event.symbols is None or symbol is None:
                return True
            if symbol.upper() in event.symbols:
                return True
    return False


def _asset_class(symbol: str) -> str:
    """Classify a symbol into an asset class for session rules."""
    s = symbol.upper()
    if s in _EQUITY_INDEX:
        return "equity_index"
    if s in _METALS:
        return "metals"
    if s in _ENERGY:
        return "energy"
    if s in _AGRICULTURE:
        return "agriculture"
    return "unknown"


def is_session_open(symbol: str, dt: datetime) -> bool:
    """Return ``True`` when *symbol* is inside its approximate CME session.

    This uses the standard CME Globex nearly-24-hour schedule with a daily
    maintenance window from 17:00-18:00 ET.  It is intentionally simple and
    does not model holidays, early closes, or product-specific micro sessions.
    """
    dt = dt.astimezone(ET) if dt.tzinfo else dt.replace(tzinfo=ET)
    t = dt.time()
    weekday = dt.weekday()  # Monday=0 ... Sunday=6

    if _MAINTENANCE_START <= t < _MAINTENANCE_END:
        return False

    asset = _asset_class(symbol)

    # All supported asset classes follow the same Globex schedule in this model.
    if asset in ("equity_index", "metals", "energy", "agriculture", "unknown"):
        if weekday == 5:  # Saturday
            return False
        if weekday == 6:  # Sunday
            return t >= _MAINTENANCE_END
        if weekday == 4:  # Friday
            return t < _MAINTENANCE_START
        return True

    return False


__all__ = [
    "ET",
    "NewsEvent",
    "now_et",
    "is_killzone",
    "is_news_blackout",
    "is_session_open",
]
