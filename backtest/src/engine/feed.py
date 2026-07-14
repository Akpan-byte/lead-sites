# CHANGE_SUMMARY
# 2026-07-02  kilo
#   - Created engine/feed.py: Redis reader with pipeline GETs for Polymarket + BTC spot.
#   - Normalizes orderbook shapes so downstream modules can walk depth safely.
# 2026-07-02  kilo
#   - Updated latest_book and snapshot to prefer polymarket:ob:* keys (published
#     by the upgraded collector) and fall back to paper:ob:* keys (legacy websocket
#     feed).
#   - Updated latest_prices and snapshot bid/ask/trade to prefer polymarket:*
#     keys (collector) and fall back to paper:* keys (legacy websocket feed).
# 2026-07-03  kilo
#   - Added latest_book_real() to return only real multi-level snapshots.
#   - Added latency_book() for a fresh book snapshot used by latency_guard.
#   - Synthetic fallback in latest_book is now clearly marked and rejected by
#     execution.py so callers cannot enter on placeholder depth.
# 2026-07-04  kilo
#   - Added latest_spot_price(asset) to read hyperliquid:trade:<asset> for any asset.
#   - latest_btc_price() is now an alias for latest_spot_price("BTC").
#   - snapshot() now supports include_spot_for list so one call can fetch BTC, ETH,
#     SOL, BNB, XRP, HYPE spot prices together.
# 2026-07-05  kilo
#   - Added latest_oracle_price(asset) reading chainlink:trade:<asset> from the
#     new Chainlink RTDS collector.
#   - latest_spot_price(asset) now tries hyperliquid:trade:<asset> first, then
#     Binance REST, then Coinbase REST for supported assets.
#   - Added _fetch_rest_spot(asset) helper with a short in-memory cache so we do
#     not hammer the fallbacks when Hyperliquid is unavailable.
#   - Added oracle_price_at_time(asset, timestamp) that queries the historical
#     Chainlink tick store (chainlink:history:<asset>) for market-open reference.
# 2026-07-05  kilo
#   - latest_prices() now derives bid/ask from the full orderbook snapshot when
#     available, falling back to top-of-book keys only when no book is present.
#     This keeps the prices seen by signal generation consistent with the depth
#     used by the execution layer.
# WHY: Entry price discovery must be Hyperliquid-first with REST fallbacks, while
#      expiry/oracle resolution must use Chainlink. Bid/ask must agree with the
#      orderbook the bot actually walks so paper fills mirror real life.

"""Read-only Redis feed wrapper for Polymarket prices, orderbooks, and spot/oracle prices."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

REDIS_HOST = "localhost"
REDIS_PORT = 6379
REDIS_DB = 0

# Short cache for REST spot fallbacks so we do not hit Binance/Coinbase on every
# runner loop tick when Hyperliquid is unavailable.
_REST_CACHE_TTL_SECONDS = 5.0


class RedisFeed:
    """Pipeline-aware Redis reader. Raises on connection errors so callers can retry."""

    def __init__(
        self,
        host: str = REDIS_HOST,
        port: int = REDIS_PORT,
        db: int = REDIS_DB,
        socket_connect_timeout: float = 5.0,
        socket_timeout: float = 5.0,
    ):
        import redis  # local import keeps module importable when redis is missing at rest

        self._redis = redis.Redis(
            host=host,
            port=port,
            db=db,
            decode_responses=True,
            socket_connect_timeout=socket_connect_timeout,
            socket_timeout=socket_timeout,
        )
        self._rest_cache: dict[str, tuple[float, float]] = {}

    def _get_with_fallback(self, primary_key: str, fallback_key: str) -> Any:
        """Return primary value if present, otherwise fallback value."""
        primary = self._redis.get(primary_key)
        if primary is not None:
            return primary
        return self._redis.get(fallback_key)

    def latest_prices(self, token_id: str) -> dict[str, Any]:
        """Return latest bid/ask/trade for a single token via a pipeline.

        Bid and ask are now derived from the full orderbook snapshot whenever it
        is available so they stay consistent with the depth the execution layer
        walks.  Only when no book snapshot exists do we fall back to the
        polymarket:* / paper:* top-of-book keys.
        """
        pipe = self._redis.pipeline()
        pipe.get(f"polymarket:ob:{token_id}")
        pipe.get(f"paper:ob:{token_id}")
        pipe.get(f"polymarket:bid:{token_id}")
        pipe.get(f"paper:bid:{token_id}")
        pipe.get(f"polymarket:ask:{token_id}")
        pipe.get(f"paper:ask:{token_id}")
        pipe.get(f"polymarket:trade:{token_id}")
        pipe.get(f"paper:trade:{token_id}")
        (
            collector_ob_raw,
            paper_ob_raw,
            collector_bid_raw,
            paper_bid_raw,
            collector_ask_raw,
            paper_ask_raw,
            collector_trade_raw,
            paper_trade_raw,
        ) = pipe.execute()

        # Derive best bid/ask from the actual book so prices and depth agree.
        ob_raw = collector_ob_raw or paper_ob_raw
        book_bid: float | None = None
        book_ask: float | None = None
        if ob_raw is not None:
            try:
                book = self._normalize_book(json.loads(ob_raw))
                if book:
                    bids = book.get("bids") or []
                    asks = book.get("asks") or []
                    if bids:
                        book_bid = float(bids[0][0])
                    if asks:
                        book_ask = float(asks[0][0])
            except (json.JSONDecodeError, TypeError):
                pass

        return {
            "token_id": token_id,
            "bid": book_bid if book_bid is not None else self._to_float(collector_bid_raw or paper_bid_raw),
            "ask": book_ask if book_ask is not None else self._to_float(collector_ask_raw or paper_ask_raw),
            "trade": self._to_json(collector_trade_raw or paper_trade_raw),
            "timestamp_utc": time.time(),
        }

    def _synthetic_book(self, token_id: str) -> dict[str, Any] | None:
        """Build a one-level book from best bid/ask when no full snapshot exists.

        This keeps paper trading functional for every market that has prices,
        while still using full websocket depth whenever the collector provides it.
        """
        prices = self.latest_prices(token_id)
        bid = prices.get("bid")
        ask = prices.get("ask")
        if bid is None or ask is None or bid <= 0 or ask <= 0:
            return None
        # Size is intentionally large so the min-contracts guard in the engine
        # is the binding constraint, not this synthetic level.
        return {
            "bids": [(bid, 1_000_000.0)],
            "asks": [(ask, 1_000_000.0)],
            "raw": {"synthetic": True, "bid": bid, "ask": ask},
        }

    def latest_book(self, token_id: str) -> dict[str, Any] | None:
        """Return a normalized orderbook dict {bids, asks} or None if missing.

        Prefers polymarket:ob:* (collector orderbook snapshots) and falls back to
        paper:ob:* (legacy websocket feed).  If neither is present, synthesizes a
        one-level book from the best bid/ask so that strategies can still trade
        markets that have prices but have not yet received a full depth snapshot.
        """
        pipe = self._redis.pipeline()
        pipe.get(f"polymarket:ob:{token_id}")
        pipe.get(f"paper:ob:{token_id}")
        collector_ob_raw, paper_ob_raw = pipe.execute()
        # Use collector first; fall back to the websocket namespace if absent.
        raw = collector_ob_raw or paper_ob_raw
        if raw is not None:
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, TypeError) as exc:
                logger.warning("Invalid orderbook JSON for %s: %s", token_id, exc)
                return None
            return self._normalize_book(parsed)
        # No full snapshot yet — fall back to a synthetic book from top-of-book.
        return self._synthetic_book(token_id)

    def latest_book_real(self, token_id: str) -> dict[str, Any] | None:
        """Return the book only if it is a real multi-level snapshot.

        Returns None for synthetic placeholder books so callers are forced to
        trade against genuine collector/websocket depth.
        """
        book = self.latest_book(token_id)
        if book is None:
            return None
        raw = book.get("raw") or {}
        if raw.get("synthetic") is True:
            return None
        asks = book.get("asks") or []
        bids = book.get("bids") or []
        if len(asks) < 1 and len(bids) < 1:
            return None
        return book

    def latency_book(self, token_id: str) -> dict[str, Any] | None:
        """Fetch a fresh book snapshot for the latency guard.

        Prefers collector orderbooks and falls back to the legacy websocket feed,
        but never synthesizes a placeholder book.
        """
        pipe = self._redis.pipeline()
        pipe.get(f"polymarket:ob:{token_id}")
        pipe.get(f"paper:ob:{token_id}")
        collector_ob_raw, paper_ob_raw = pipe.execute()
        raw = collector_ob_raw or paper_ob_raw
        if raw is None:
            return None
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("Invalid latency book JSON for %s: %s", token_id, exc)
            return None
        return self._normalize_book(parsed)

    def latest_spot_price(self, asset: str = "BTC") -> float | None:
        """Return the latest spot price for ``asset``.

        Tries the Hyperliquid Redis feed first (``hyperliquid:trade:<asset>``),
        then falls back to Binance REST, then Coinbase REST for supported assets.
        """
        asset = str(asset).upper()
        raw = self._redis.get(f"hyperliquid:trade:{asset}")
        price = self._extract_price(raw)
        if price is not None and price > 0:
            return price
        return self._fetch_rest_spot(asset)

    def latest_oracle_price(self, asset: str = "BTC") -> float | None:
        """Return the latest Chainlink oracle price from the RTDS collector.

        Reads ``chainlink:trade:<asset>`` published by
        ``collectors/chainlink_rtds_collector.py``.  This is the price used for
        expiry/oracle resolution on Polymarket crypto markets.
        """
        asset = str(asset).upper()
        raw = self._redis.get(f"chainlink:trade:{asset}")
        return self._extract_price(raw)

    def oracle_price_at_time(self, asset: str, timestamp: float) -> float | None:
        """Return the Chainlink oracle price closest to ``timestamp`` (Unix s).

        Queries the sorted set ``chainlink:history:<asset>`` populated by the
        Chainlink RTDS collector.  If no history is available, returns None so
        callers fall back to the live oracle or entry spot.
        """
        asset = str(asset).upper()
        history_key = f"chainlink:history:{asset}"
        try:
            # Convert Unix seconds to milliseconds to match the collector's score.
            target_ms = int(timestamp * 1000)
            # Nearest tick at or before the target window.
            before = self._redis.zrevrangebyscore(history_key, target_ms, "-inf", start=0, num=1)
            if before:
                data = json.loads(before[0])
                return self._to_float(data.get("price"))
            # If nothing before, take the earliest tick we have (market just opened).
            earliest = self._redis.zrange(history_key, 0, 0)
            if earliest:
                data = json.loads(earliest[0])
                return self._to_float(data.get("price"))
        except Exception as exc:
            logger.debug("oracle_price_at_time failed for %s @ %s: %s", asset, timestamp, exc)
        return None

    def _fetch_rest_spot(self, asset: str) -> float | None:
        """Fetch spot price from Binance then Coinbase REST, with a tiny cache."""
        now = time.time()
        cached = self._rest_cache.get(asset)
        if cached and now - cached[1] < _REST_CACHE_TTL_SECONDS:
            return cached[0]

        price = self._fetch_binance_spot(asset)
        if price is None:
            price = self._fetch_coinbase_spot(asset)
        if price is not None and price > 0:
            self._rest_cache[asset] = (price, now)
        return price

    @staticmethod
    def _fetch_binance_spot(asset: str) -> float | None:
        """Call Binance /api/v3/ticker/price?symbol=<ASSET>USDT."""
        import requests

        try:
            resp = requests.get(
                "https://api.binance.com/api/v3/ticker/price",
                params={"symbol": f"{asset}USDT"},
                timeout=5.0,
            )
            resp.raise_for_status()
            data = resp.json()
            return RedisFeed._to_float(data.get("price"))
        except Exception as exc:
            logger.debug("Binance spot fetch failed for %s: %s", asset, exc)
            return None

    @staticmethod
    def _fetch_coinbase_spot(asset: str) -> float | None:
        """Call Coinbase /products/<ASSET>-USD/ticker."""
        import requests

        try:
            resp = requests.get(
                f"https://api.exchange.coinbase.com/products/{asset}-USD/ticker",
                timeout=5.0,
            )
            resp.raise_for_status()
            data = resp.json()
            return RedisFeed._to_float(data.get("price"))
        except Exception as exc:
            logger.debug("Coinbase spot fetch failed for %s: %s", asset, exc)
            return None

    @staticmethod
    def _extract_price(raw: Any) -> float | None:
        """Parse a Redis value into a float price."""
        if raw is None:
            return None
        parsed = RedisFeed._to_json(raw)
        if isinstance(parsed, dict):
            price = parsed.get("price")
            if price is None:
                price = parsed.get("px")
            return RedisFeed._to_float(price)
        return RedisFeed._to_float(parsed)

    def latest_btc_price(self) -> float | None:
        """Backward-compatible alias for latest_spot_price('BTC')."""
        return self.latest_spot_price("BTC")

    def snapshot(
        self,
        token_ids: list[str],
        include_btc: bool = True,
        include_spot_for: list[str] | None = None,
    ) -> dict[str, Any]:
        """Bulk snapshot: one pipeline round-trip for books + prices + spot prices.

        Orderbooks prefer polymarket:ob:* keys (collector) and fall back to
        paper:ob:* keys (legacy websocket feed).  Bid/ask/trade prefer
        polymarket:* keys (collector) and fall back to paper:* keys so the engine
        uses the same canonical source for prices and depth.

        ``include_spot_for`` is a list of asset symbols (e.g. ["BTC","ETH"]) whose
        hyperliquid:trade:<asset> prices should be included. ``include_btc=True``
        is kept for backward compatibility and is equivalent to adding "BTC".
        """
        spot_assets: list[str] = []
        if include_spot_for:
            spot_assets = [a.upper() for a in include_spot_for]
        if include_btc and "BTC" not in spot_assets:
            spot_assets.append("BTC")

        pipe = self._redis.pipeline()
        for token_id in token_ids:
            pipe.get(f"polymarket:ob:{token_id}")
            pipe.get(f"paper:ob:{token_id}")
            pipe.get(f"polymarket:bid:{token_id}")
            pipe.get(f"paper:bid:{token_id}")
            pipe.get(f"polymarket:ask:{token_id}")
            pipe.get(f"paper:ask:{token_id}")
            pipe.get(f"polymarket:trade:{token_id}")
            pipe.get(f"paper:trade:{token_id}")
        for asset in spot_assets:
            pipe.get(f"hyperliquid:trade:{asset}")

        results = pipe.execute()
        snapshot: dict[str, Any] = {
            "markets": {},
            "btc_price": None,
            "spot_prices": {},
            "timestamp_utc": time.time(),
        }

        per_token = 8
        spot_index_start = len(token_ids) * per_token
        spot_results = results[spot_index_start:spot_index_start + len(spot_assets)]
        for asset, raw in zip(spot_assets, spot_results):
            price = self._to_float(self._to_json(raw))
            snapshot["spot_prices"][asset] = price
            if asset == "BTC":
                snapshot["btc_price"] = price

        for i, token_id in enumerate(token_ids):
            (
                collector_ob_raw,
                paper_ob_raw,
                collector_bid_raw,
                paper_bid_raw,
                collector_ask_raw,
                paper_ask_raw,
                collector_trade_raw,
                paper_trade_raw,
            ) = results[i * per_token : (i + 1) * per_token]

            ob_raw = collector_ob_raw or paper_ob_raw
            bid_raw = collector_bid_raw or paper_bid_raw
            ask_raw = collector_ask_raw or paper_ask_raw
            trade_raw = collector_trade_raw or paper_trade_raw

            book = self._normalize_book(json.loads(ob_raw)) if ob_raw else None
            if book is None and bid_raw and ask_raw:
                bid = self._to_float(bid_raw)
                ask = self._to_float(ask_raw)
                if bid and ask and bid > 0 and ask > 0:
                    book = {
                        "bids": [(bid, 1_000_000.0)],
                        "asks": [(ask, 1_000_000.0)],
                        "raw": {"synthetic": True, "bid": bid, "ask": ask},
                    }

            snapshot["markets"][token_id] = {
                "book": book,
                "bid": self._to_float(bid_raw),
                "ask": self._to_float(ask_raw),
                "trade": self._to_json(trade_raw),
            }
        return snapshot

    @staticmethod
    def _to_float(value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _to_json(value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, (dict, list)):
            return value
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        return value

    @staticmethod
    def _normalize_book(raw: Any) -> dict[str, list[tuple[float, float]]] | None:
        """Normalize common orderbook shapes into sorted (price, size) lists."""
        if raw is None:
            return None

        data: Any = raw
        if isinstance(raw, list) and len(raw) == 2 and isinstance(raw[0], list):
            # shape: [[bids], [asks]]
            bids_in, asks_in = raw[0], raw[1]
            return {
                "bids": RedisFeed._parse_levels(bids_in, reverse_sort=True),
                "asks": RedisFeed._parse_levels(asks_in, reverse_sort=False),
                "raw": raw,
            }

        if isinstance(raw, dict):
            if "orderbook" in raw:
                data = raw["orderbook"]
            bids_in = data.get("bids") or data.get("buy") or []
            asks_in = data.get("asks") or data.get("sell") or []
            return {
                "bids": RedisFeed._parse_levels(bids_in, reverse_sort=True),
                "asks": RedisFeed._parse_levels(asks_in, reverse_sort=False),
                "raw": raw,
            }

        logger.warning("Unrecognized orderbook shape: %s", type(raw).__name__)
        return None

    @staticmethod
    def _parse_levels(levels: Any, reverse_sort: bool) -> list[tuple[float, float]]:
        """Parse a list of [price, size] or {'price':..., 'size':...} tuples."""
        parsed: list[tuple[float, float]] = []
        if not isinstance(levels, list):
            return parsed

        for level in levels:
            if isinstance(level, (list, tuple)) and len(level) >= 2:
                try:
                    price = float(level[0])
                    size = float(level[1])
                except (ValueError, TypeError):
                    continue
            elif isinstance(level, dict):
                price = RedisFeed._level_value(level, "price", "px")
                size = RedisFeed._level_value(level, "size", "amount", "quantity")
                if price is None or size is None:
                    continue
            else:
                continue

            if price > 0 and size > 0:
                parsed.append((price, size))

        parsed.sort(key=lambda x: x[0], reverse=reverse_sort)
        return parsed

    @staticmethod
    def _level_value(level: dict[str, Any], *keys: str) -> float | None:
        for key in keys:
            val = level.get(key)
            if val is not None:
                try:
                    return float(val)
                except (ValueError, TypeError):
                    continue
        return None


__all__ = ["RedisFeed", "REDIS_HOST", "REDIS_PORT", "REDIS_DB"]
