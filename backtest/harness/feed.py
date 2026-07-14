# CHANGE_SUMMARY
# 2026-07-11  kimi
#   - Created harness/feed.py: BacktestFeed, a drop-in replacement for
#     engine.feed.RedisFeed served from polybacktest snapshots + BinanceReference.
# WHY: Runners call latest_prices/latest_book_real/latency_book/latest_spot_price/
#      latest_oracle_price/oracle_price_at_time. Reimplementing that exact surface
#      over replayed snapshots lets the SAME strategy/engine code run unchanged.
#      Bid/ask are derived from the book top-of-book exactly like RedisFeed does.
"""Backtest feed: polybacktest orderbook snapshots + Binance reference prices."""

from __future__ import annotations

import sys
from typing import Any

sys.path.insert(0, "/config/backtest_repo")
from engine.feed import RedisFeed  # reuse the live book normalization


class BacktestFeed:
    """RedisFeed-compatible interface over replayed data (no Redis, no network)."""

    def __init__(self, ref, clock):
        self._ref = ref
        self._clock = clock
        # token_id -> (normalized_book, raw_snapshot) for the current tick
        self._books: dict[str, dict[str, Any] | None] = {}

    def set_book(self, token_id: str, book: dict[str, Any] | None) -> None:
        """Driver installs the current tick's normalized book per token."""
        self._books[token_id] = book

    # -- RedisFeed surface used by the runner ---------------------------------
    def latest_prices(self, token_id: str) -> dict[str, Any]:
        book = self._books.get(token_id)
        bid = ask = None
        if book:
            bids = book.get("bids") or []
            asks = book.get("asks") or []
            if bids:
                bid = float(bids[0][0])
            if asks:
                ask = float(asks[0][0])
        return {"token_id": token_id, "bid": bid, "ask": ask, "trade": None,
                "timestamp_utc": self._clock()}

    def latest_book(self, token_id: str) -> dict[str, Any] | None:
        # No synthetic fallback in the backtest: books are always real snapshots
        # or None (missing/empty at this tick).
        return self._books.get(token_id)

    def latest_book_real(self, token_id: str) -> dict[str, Any] | None:
        return self._books.get(token_id)

    def latency_book(self, token_id: str) -> dict[str, Any] | None:
        return self._books.get(token_id)

    def latest_spot_price(self, asset: str = "BTC") -> float | None:
        return self._ref.price_at(self._clock())

    def latest_oracle_price(self, asset: str = "BTC") -> float | None:
        # Chainlink is replaced by the Binance reference for the backtest
        # (user-approved substitution; documented in the run manifest).
        return self._ref.price_at(self._clock())

    def oracle_price_at_time(self, asset: str, timestamp: float) -> float | None:
        return self._ref.price_at(timestamp)


def normalize_snapshot_book(snapshot_book: dict | None) -> dict[str, Any] | None:
    """Normalize a polybacktest {asks:[{price,size}], bids:[...]} via the live
    engine normalizer; returns None when the book is empty/missing."""
    if not snapshot_book:
        return None
    book = RedisFeed._normalize_book(snapshot_book)
    if not book:
        return None
    if not book.get("asks") and not book.get("bids"):
        return None
    return book


__all__ = ["BacktestFeed", "normalize_snapshot_book"]
