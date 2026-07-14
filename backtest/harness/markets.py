# CHANGE_SUMMARY
# 2026-07-11  kimi
#   - Created harness/markets.py: load polybacktest 5m market files and build the
#     same market dicts engine.discovery produces in live trading.
# WHY: Runners consume market dicts with condition_id/token ids/window times/
#      fee_schedule/open_oracle_price. Polybacktest files carry only market_id +
#      snapshots, so the harness reconstructs the window (floor to 5m boundary),
#      synthetic token ids (UP/DOWN), the real Polymarket fee schedule
#      (rate 0.07, exponent 1, takerOnly — verified via Gamma 2026-07-11), and
#      the Binance-based open-oracle reference.
"""Market dict construction + snapshot loading for the replay driver."""

from __future__ import annotations

import datetime as dt
import gzip
import json
import os

WINDOW_SECONDS = 300  # BTC 5m markets

# Verified against Polymarket Gamma on 2026-07-11 for "Bitcoin Up or Down"
# markets: {'exponent': 1, 'rate': 0.07, 'takerOnly': True, 'rebateRate': 0.2}.
FEE_SCHEDULE = {"rate": 0.07, "exponent": 1, "takerOnly": True}


def _iso(ts: float) -> str:
    return dt.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%SZ")


class MarketReplay:
    """One market's replay payload: discovery-style dict + snapshot arrays.

    Owns a replay pointer: advance(t) moves to the latest snapshot with
    time <= t (mirrors "latest Redis value at poll time" in live).
    """

    __slots__ = ("market", "times", "snaps", "window_start", "window_end", "_p")

    def __init__(self, market, times, snaps, window_start, window_end):
        self.market = market
        self.times = times          # list[float] unix seconds
        self.snaps = snaps          # list[dict] raw snapshots
        self.window_start = window_start
        self.window_end = window_end
        self._p = -1

    def reset(self) -> None:
        self._p = -1

    def advance(self, t: float):
        """Move pointer to the last snapshot with time <= t; return it or None."""
        p = self._p
        n = len(self.times)
        while p + 1 < n and self.times[p + 1] <= t:
            p += 1
        self._p = p
        if p < 0:
            return None
        return self.snaps[p]

    def snapshot_after(self, t: float, window: float, token: str):
        """First snapshot in (t, t+window] whose book for ``token`` has asks.

        Used to emulate the runner's BOOK_WAIT re-poll: live re-polls the real
        book for up to 2s; the fill then happens at that later time. Does not
        move the main replay pointer. Returns the normalized book with an extra
        ``_exec_ts`` key (the later execution time), or None.
        """
        from harness.feed import normalize_snapshot_book  # local to avoid cycle

        side = "orderbook_up" if token.endswith(":UP") else "orderbook_down"
        p = self._p + 1
        n = len(self.times)
        deadline = t + window
        while p < n and self.times[p] <= deadline:
            book = normalize_snapshot_book(self.snaps[p].get(side))
            if book and book.get("asks"):
                book["_exec_ts"] = self.times[p]
                return book
            p += 1
        return None


def load_market_file(path: str, ref, asset: str = "BTC") -> MarketReplay | None:
    """Load one {market_id}.json.gz and build its discovery-style market dict."""
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        data = json.load(fh)
    if not data:
        return None

    times = []
    for r in data:
        t = r["time"]
        # '2026-05-08T16:05:38.003803Z' -> epoch seconds; some snapshots have no
        # fractional part ('2026-06-15T17:49:56Z').
        try:
            ts = dt.datetime.strptime(t, "%Y-%m-%dT%H:%M:%S.%fZ").timestamp()
        except ValueError:
            ts = dt.datetime.strptime(t, "%Y-%m-%dT%H:%M:%SZ").timestamp()
        times.append(ts)

    first_ts = times[0]
    window_start = int(first_ts // WINDOW_SECONDS) * WINDOW_SECONDS
    window_end = window_start + WINDOW_SECONDS
    market_id = str(data[0].get("market_id") or os.path.basename(path).split(".")[0])

    # Window-open reference (known the moment the window starts -> no lookahead).
    open_oracle = ref.price_at(window_start) if ref is not None else None

    market = {
        "condition_id": market_id,
        "token_id_yes": f"{market_id}:UP",
        "token_id_no": f"{market_id}:DOWN",
        "start_date_iso": _iso(window_start),
        "end_date_iso": _iso(window_end),
        "duration": "5m",
        "asset": asset,
        "strike": open_oracle,
        "open_oracle_price": open_oracle,
        "fee_schedule": FEE_SCHEDULE,
        "resolution_source": "binance",
        "question": f"Bitcoin Up or Down - 5m window {_iso(window_start)}",
        "event_title": "Bitcoin Up or Down",
    }
    return MarketReplay(market, times, data, window_start, window_end)


__all__ = ["MarketReplay", "load_market_file", "WINDOW_SECONDS", "FEE_SCHEDULE"]
