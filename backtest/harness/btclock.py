# CHANGE_SUMMARY
# 2026-07-11  kimi
#   - Created harness/btclock.py: process-wide fake clock for backtest replay.
# WHY: The live engine/signals read wall-clock time (time.time, datetime.now).
#      A feed-swap backtest must drive them with replay time so Trade.opened_at,
#      check_entry's expired-market guard, and daily_orb_v5's 9:30 ET anchor all
#      see simulated time. install() MUST run before any repo module is imported
#      so `from datetime import datetime` binds FakeDateTime everywhere.
"""Deterministic replay clock. Patches time.time and datetime.datetime."""

import datetime as _dt
import os
import sys
import time as _time


class FakeDateTime(_dt.datetime):
    """datetime.datetime subclass whose now() returns the replay timestamp."""

    _ts: float = 0.0

    @classmethod
    def set(cls, ts: float) -> None:
        cls._ts = float(ts)

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        if tz is None:
            return cls.fromtimestamp(cls._ts)
        return cls.fromtimestamp(cls._ts, tz)

    @classmethod
    def utcnow(cls):  # type: ignore[override]
        return cls.utcfromtimestamp(cls._ts)


# Captured at import (before install()) so log timestamps can use real time.
_REAL_TIME_FN = _time.time
_REAL_DATETIME = _dt.datetime


def real_now_iso() -> str:
    """Real wall-clock ISO timestamp (immune to the fake clock)."""
    return _REAL_DATETIME.utcfromtimestamp(_REAL_TIME_FN()).isoformat() + "Z"


def _fake_time() -> float:
    return FakeDateTime._ts


def install(ts0: float = 0.0) -> None:
    """Install the fake clock. Call BEFORE importing engine/signals modules.

    - time.time -> replay seconds
    - datetime.datetime -> FakeDateTime (now/utcnow return replay time; all other
      constructors, fromisoformat, arithmetic keep working via inheritance)
    - TZ forced to UTC so naive datetime.now() matches the UTC VPS runners.
    """
    FakeDateTime.set(ts0)
    os.environ["TZ"] = "UTC"
    _time.tzset()
    _time.time = _fake_time  # type: ignore[assignment]
    sys.modules["datetime"].datetime = FakeDateTime  # type: ignore[attr-defined]


def set_ts(ts: float) -> None:
    FakeDateTime.set(ts)


class Clock:
    """Small handle passed around the replay: callable read + explicit set."""

    def __call__(self) -> float:
        return FakeDateTime._ts

    def set(self, ts: float) -> None:
        FakeDateTime.set(ts)
