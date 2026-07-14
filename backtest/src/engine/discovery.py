# CHANGE_SUMMARY
# 2026-07-02  kilo
#   - Created engine/discovery.py for Polymarket BTC UP/DOWN market discovery.
#   - Primary: mirror the live collector's slug-based discovery (btc-updown-<tf>-<window>).
#   - Fallback: Gamma /events endpoint for BTC-titled events.
# 2026-07-02  runner-diagnostics-fix
#   - Added Redis-backed shared cache for discovered BTC markets so 84 runners do
#     not all hammer the Gamma API simultaneously. Cache TTL is 30s; a short
#     setnx lock prevents a thundering herd when the cache expires.
# 2026-07-03  kilo
#   - Extended cache TTL to 300s and lock TTL to 30s because a fetch takes ~13s.
#   - Runners that lose the lock now poll the cache for 15s instead of falling
#     back to a local fetch, eliminating the 0-market windows between refreshes.
# 2026-07-03  kilo
#   - Added _is_active() filter and applied it in discover_btc_markets() so
#     active_only=True actually excludes markets whose end_date has passed.
# 2026-07-04  kilo
#   - Generalized discovery to support any asset (BTC, ETH, SOL, BNB, XRP, HYPE).
#   - Added discover_asset_markets(asset, ...); discover_btc_markets is an alias.
#   - Slug discovery now uses <asset>-updown-<tf>-<window> and the title/question
#     filter matches the asset's keywords.
#   - Redis cache keys are per-asset so BTC runners do not share cache with ETH.
# 2026-07-05  kilo
#   - Captured event startDate and resolutionSource in _normalize_market.
#   - Added start_date_iso, resolution_source, and open_oracle_price to market dicts.
#   - Added _open_oracle_price helper; uses RedisFeed.oracle_price_at_time on the
#     chainlink:history:<asset> store and only for markets whose startDate is in
#     the past.
# 2026-07-05  kilo
#   - Lowered _extract_strike() threshold from >= $1,000 to > $0 so low-priced
#     assets (XRP, HYPE, SOL, BNB) retain their real strikes.
# 2026-07-05  kilo
#   - _normalize_market now extracts feeSchedule from Gamma event/market data.
#   - _load_collector_markets_from_redis enriches collector-derived markets by
#     fetching the corresponding Gamma slug and merging fee_schedule,
#     start_date_iso, resolution_source, and open_oracle_price.
# WHY: The paper-trading fleet is expanding to ETH/SOL/BNB/XRP/HYPE UP/DOWN
#      markets. Discovery must be asset-aware and isolated to avoid token-ID
#      mismatches and cross-asset cache pollution, and must not discard strikes
#      simply because the underlying asset trades below $1,000.

"""Discover active Polymarket BTC UP/DOWN markets."""

from __future__ import annotations

import json
import logging
import math
import re
import time
from datetime import datetime, timezone
from typing import Any

import requests

logger = logging.getLogger(__name__)

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
REQUEST_TIMEOUT = 15

# Shared cache so the paper-trading processes do not all hit Polymarket APIs.
# Keys are per-asset so BTC discovery does not pollute ETH discovery.
REDIS_CACHE_KEY_TEMPLATE = "polymarket:{asset}:markets"
REDIS_CACHE_TTL_SECONDS = 300  # 5 min: discovery is slow (~13s), avoid repeated fetches.
REDIS_LOCK_KEY_TEMPLATE = "polymarket:{asset}:markets:lock"
REDIS_LOCK_TTL_SECONDS = 30    # Must cover the typical fetch duration.
DEFAULT_TTL_SECONDS = 300

DURATION_KEYWORDS = {
    r"\b5\s*-?\s*(?:min|minute)s?\b": "5m",
    r"\b15\s*-?\s*(?:min|minute)s?\b": "15m",
    r"\b1\s*-?\s*(?:hour|hr)s?\b": "1h",
    r"\b4\s*-?\s*(?:hour|hr)s?\b": "4h",
    r"\b1\s*-?\s*(?:day|daily)\b": "1d",
}

TIMEFRAMES = {
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}

# Keywords used to confirm that an event/market belongs to a given asset.
# The slug already encodes the asset, but the events fallback needs this.
ASSET_KEYWORDS = {
    "BTC": ["bitcoin", "btc"],
    "ETH": ["ethereum", "eth"],
    "SOL": ["solana", "sol"],
    "BNB": ["bnb", "binance coin", "binance"],
    "XRP": ["xrp", "ripple"],
    "HYPE": ["hype", "hyperliquid"],
    "DOGE": ["dogecoin", "doge"],
}


def _markets_complete(markets: list[dict[str, Any]]) -> bool:
    """Return True if all markets carry the metadata needed for reality fidelity."""
    for m in markets:
        if not m.get("fee_schedule"):
            return False
        if not m.get("start_date_iso"):
            return False
        if not m.get("resolution_source"):
            return False
    return True


def _window_start(asset: str, duration: str, end_date_iso: Any) -> datetime | None:
    """Compute the window-open datetime from the end date and duration.

    Polymarket UP/DOWN markets resolve by comparing the end-of-window price to
    the price at the beginning of that window.  Gamma's startDate is the event
    creation time, which is usually ~24h earlier, so we derive the true
    window-open time ourselves.
    """
    if not end_date_iso or duration not in TIMEFRAMES:
        return None
    try:
        end_dt = datetime.fromisoformat(str(end_date_iso).replace("Z", "+00:00"))
        tf_sec = TIMEFRAMES[duration]
        window_start_ts = int((end_dt.timestamp() - tf_sec) // tf_sec * tf_sec)
        return datetime.fromtimestamp(window_start_ts, tz=timezone.utc)
    except (ValueError, TypeError):
        return None


def _slug_for_market(asset: str, duration: str, end_date_iso: Any) -> str | None:
    """Reconstruct the collector slug from asset, duration label, and end date."""
    ws = _window_start(asset, duration, end_date_iso)
    if ws is None:
        return None
    return f"{asset.lower()}-updown-{duration}-{int(ws.timestamp())}"


def _is_active(market: dict[str, Any]) -> bool:
    """Return True if the market has not yet reached its end_date."""
    end_date_iso = market.get("end_date_iso")
    if not end_date_iso:
        return True
    try:
        end_dt = datetime.fromisoformat(str(end_date_iso).replace("Z", "+00:00"))
        return datetime.now(timezone.utc) < end_dt
    except (ValueError, TypeError):
        return True


def _open_oracle_price(asset: str, start_date_iso: Any) -> float | None:
    """Return the Chainlink oracle price at market-open/start-of-window.

    Only attempts a lookup when the market has already started (startDate is in
    the past).  Uses the historical oracle tick store populated by the Chainlink
    RTDS collector.  If the exact window-open tick is not yet in history, falls
    back to the earliest available tick so we still anchor expiry to Chainlink
    rather than the entry spot.
    """
    if not start_date_iso:
        return None
    try:
        start_dt = datetime.fromisoformat(str(start_date_iso).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if start_dt >= datetime.now(timezone.utc):
        return None
    timestamp = start_dt.timestamp()
    try:
        from engine.feed import RedisFeed

        feed = RedisFeed()
        price = feed.oracle_price_at_time(asset, timestamp)
        if price is None:
            # Window opened before our history window; use the earliest Chainlink
            # tick we have as the best available reference.
            price = feed.oracle_price_at_time(asset, 0.0)
        if price is None:
            # Last resort: use the current live Chainlink oracle.  This is only
            # appropriate when the window just opened and history is not yet
            # populated; it avoids falling back to entry_spot.
            price = feed.latest_oracle_price(asset)
        return price
    except Exception as exc:
        logger.debug(
            "Could not fetch open oracle price for %s at %s: %s", asset, start_date_iso, exc
        )
        return None


def _text(container: dict[str, Any], key: str, default: str) -> str:
    value = container.get(key)
    return str(value) if value is not None else default


def _looks_like_up_down(text: str) -> bool:
    return bool(
        re.search(r"\b(above|below|higher|lower|up|down|over|under)\b", text, re.IGNORECASE)
    )


def _infer_duration(question: str, end_date_iso: Any) -> str:
    text = f"{question} "
    for pattern, label in DURATION_KEYWORDS.items():
        if re.search(pattern, text, re.IGNORECASE):
            return label

    if end_date_iso:
        try:
            end_dt = datetime.fromisoformat(str(end_date_iso).replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            delta = end_dt - now
            total_minutes = delta.total_seconds() / 60.0
            if total_minutes <= 10:
                return "5m"
            if total_minutes <= 20:
                return "15m"
            if total_minutes <= 90:
                return "1h"
            if total_minutes <= 300:
                return "4h"
            return "1d"
        except (ValueError, TypeError):
            pass

    return "unknown"


def _extract_strike(question: str) -> float | None:
    """Extract a numeric strike from the market question.

    Previously required strikes >= $1,000, which discarded real strikes for
    low-priced assets (XRP, HYPE, SOL, BNB). Now any positive value is kept.
    """
    if not question:
        return None
    matches = re.findall(r"[$€£]\s*([\d,]+(?:\.\d+)?)\s*([kK])?", question)
    for num, suffix in matches:
        try:
            value = float(num.replace(",", ""))
            if suffix:
                value *= 1_000.0
            # Keep any positive strike; $1k+ filter removed for multi-asset.
            if value > 0.0:
                return value
        except ValueError:
            continue

    fallback = re.findall(r"([\d,]+(?:\.\d+)?)\s*(?:USD|usd|dollars?)", question)
    for num in fallback:
        try:
            value = float(num.replace(",", ""))
            # Keep any positive strike; $1k+ filter removed for multi-asset.
            if value > 0.0:
                return value
        except ValueError:
            continue
    return None


def _parse_json_field(value: Any) -> Any:
    """Parse a JSON-encoded string field; return as-is if already parsed."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            pass
    return value


def _find_yes_no_tokens(market: dict[str, Any]) -> tuple[str | None, str | None]:
    """Find the YES/NO token IDs for a binary market.

    Polymarket UP/DOWN crypto markets use outcomes "Up" and "Down" but the
    paper-trading engine still treats them as YES/NO (Up = YES, Down = NO).
    """
    tokens = _parse_json_field(market.get("tokens")) or []
    token_yes: str | None = None
    token_no: str | None = None

    for token in tokens:
        if not isinstance(token, dict):
            continue
        outcome = str(token.get("outcome", "")).strip().lower()
        token_id = token.get("token_id") or token.get("tokenId") or token.get("clobTokenId")
        if not token_id:
            continue
        if outcome in ("yes", "up"):
            token_yes = str(token_id)
        elif outcome in ("no", "down"):
            token_no = str(token_id)

    if not (token_yes and token_no):
        outcomes = _parse_json_field(market.get("outcomes")) or []
        token_ids = _parse_json_field(market.get("clobTokenIds")) or []
        if len(outcomes) == len(token_ids) >= 2:
            for oc, tid in zip(outcomes, token_ids):
                oc_norm = str(oc).strip().lower()
                if oc_norm in ("yes", "up"):
                    token_yes = str(tid)
                elif oc_norm in ("no", "down"):
                    token_no = str(tid)

    return token_yes, token_no


def _normalize_market(
    event: dict[str, Any],
    market: dict[str, Any],
    asset: str = "BTC",
) -> dict[str, Any] | None:
    """Normalize a Gamma event/market into our internal format.

    ``asset`` determines which keywords must appear in the title/question.
    Outcomes must be YES/NO (Polymarket's canonical shape for UP/DOWN markets).
    """
    asset = str(asset).upper()
    title = _text(event, "title", "")
    question = _text(market, "question", "")
    text = f"{title} {question}".lower()

    keywords = ASSET_KEYWORDS.get(asset, [asset.lower()])
    if not any(kw in text for kw in keywords):
        return None
    if not _looks_like_up_down(text):
        return None

    condition_id = market.get("conditionId") or market.get("condition_id")
    if not condition_id:
        return None

    token_yes, token_no = _find_yes_no_tokens(market)
    if not token_yes or not token_no:
        return None

    end_date_iso = market.get("endDate") or event.get("endDate")
    # Use the true window-open time for UP/DOWN markets, not Gamma's event
    # creation date.  This is the reference price Polymarket actually uses.
    duration_label = _infer_duration(question, end_date_iso)
    window_start_dt = _window_start(asset, duration_label, end_date_iso)
    gamma_start_iso = event.get("startDate") or market.get("startDate")
    start_date_iso = window_start_dt.isoformat().replace("+00:00", "Z") if window_start_dt else gamma_start_iso
    resolution_source = event.get("resolutionSource") or market.get("resolutionSource")
    fee_schedule = market.get("feeSchedule") or event.get("feeSchedule")
    open_oracle_price = _open_oracle_price(asset, start_date_iso)
    return {
        "condition_id": condition_id,
        "token_id_yes": token_yes,
        "token_id_no": token_no,
        "duration": duration_label,
        "end_date_iso": end_date_iso,
        "start_date_iso": start_date_iso,
        "resolution_source": resolution_source,
        "fee_schedule": fee_schedule,
        "open_oracle_price": open_oracle_price,
        "strike": _extract_strike(question),
        "question": question,
        "event_title": title,
        "asset": asset,
    }


def _fetch_slug(slug: str, asset: str = "BTC") -> list[dict[str, Any]]:
    """Fetch a single slug from Gamma events endpoint."""
    try:
        r = requests.get(
            f"{GAMMA_EVENTS_URL}?slug={slug}",
            timeout=REQUEST_TIMEOUT,
        )
        if r.status_code != 200:
            return []
        data = r.json()
        if not data or not isinstance(data, list) or not data[0]:
            return []
        event = data[0]
        markets = event.get("markets") or []
        results = []
        for market in markets:
            norm = _normalize_market(event, market, asset=asset)
            if norm:
                results.append(norm)
        return results
    except Exception as exc:
        logger.debug("Slug fetch failed for %s: %s", slug, exc)
        return []


def _discover_by_slugs(asset: str = "BTC") -> list[dict[str, Any]]:
    """Mirror the collector's slug discovery for <asset> UP/DOWN markets."""
    asset = str(asset).lower()
    now_ts = time.time()
    results = []
    seen = set()

    for tf_label, tf_sec in TIMEFRAMES.items():
        window_start = math.floor(now_ts / tf_sec) * tf_sec
        for offset in (0, tf_sec):
            slug = f"{asset}-updown-{tf_label}-{int(window_start + offset)}"
            for market in _fetch_slug(slug, asset=asset.upper()):
                if market["condition_id"] not in seen:
                    seen.add(market["condition_id"])
                    results.append(market)
    return results


def _discover_by_events(asset: str = "BTC") -> list[dict[str, Any]]:
    """Fallback: scan /events for asset-titled events."""
    try:
        r = requests.get(
            GAMMA_EVENTS_URL,
            params={"limit": 500, "active": "true", "closed": "false", "archived": "false"},
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        events = r.json()
    except Exception as exc:
        logger.warning("Gamma events fallback failed: %s", exc)
        return []

    results = []
    seen = set()
    for event in events or []:
        if not isinstance(event, dict):
            continue
        for market in event.get("markets") or []:
            norm = _normalize_market(event, market, asset=asset)
            if norm and norm["condition_id"] not in seen:
                seen.add(norm["condition_id"])
                results.append(norm)
    return results


def _load_collector_markets_from_redis(
    redis_client: Any,
    asset: str = "BTC",
) -> list[dict[str, Any]] | None:
    """Read the live collector's asset market list from Redis.

    The collector publishes the exact markets it is subscribed to under
    polymarket:collector:markets:<asset>.  Using this eliminates token-ID
    mismatches between discovery and the live websocket feed.
    """
    if redis_client is None:
        return None
    asset = str(asset).upper()
    try:
        raw = redis_client.get(f"polymarket:collector:markets:{asset.lower()}")
        if not raw:
            # Legacy fallback for BTC only.
            if asset == "BTC":
                raw = redis_client.get("polymarket:collector:btc_markets")
            if not raw:
                return None
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            return None
        results = []
        seen = set()
        for m in parsed:
            cid = m.get("condition_id")
            if not cid or cid in seen:
                continue
            seen.add(cid)
            duration = m.get("duration", "unknown")
            question = m.get("question", "")
            end_date_iso = m.get("end_date_iso")
            # Derive the true window-open time from end_date + duration.
            ws_dt = _window_start(asset, duration, end_date_iso)
            start_date_iso = m.get("start_date_iso")
            if ws_dt is not None:
                start_date_iso = ws_dt.isoformat().replace("+00:00", "Z")
            resolution_source = m.get("resolution_source")
            fee_schedule = m.get("fee_schedule")
            open_oracle_price = m.get("open_oracle_price")

            # Enrich with Gamma slug data if the collector payload is missing
            # fee_schedule, resolution_source, or open_oracle_price.  This keeps
            # the token-ID alignment of the collector path while giving us
            # reality-fidelity fields that only Gamma currently provides.
            if not fee_schedule or not resolution_source or open_oracle_price is None:
                slug = _slug_for_market(asset, duration, end_date_iso)
                if slug:
                    for enriched in _fetch_slug(slug, asset=asset):
                        if enriched.get("condition_id") == cid:
                            fee_schedule = fee_schedule or enriched.get("fee_schedule")
                            resolution_source = resolution_source or enriched.get("resolution_source")
                            open_oracle_price = open_oracle_price or enriched.get("open_oracle_price")
                            break

            if open_oracle_price is None and start_date_iso:
                open_oracle_price = _open_oracle_price(asset, start_date_iso)

            results.append({
                "condition_id": cid,
                "token_id_yes": m.get("token_id_yes"),
                "token_id_no": m.get("token_id_no"),
                "duration": duration,
                "end_date_iso": end_date_iso,
                "start_date_iso": start_date_iso,
                "resolution_source": resolution_source,
                "fee_schedule": fee_schedule,
                "open_oracle_price": open_oracle_price,
                "strike": _extract_strike(question),
                "question": question,
                "event_title": m.get("event_title", ""),
                "asset": asset,
            })
        return results
    except Exception as exc:
        logger.debug("Failed to load collector markets from Redis: %s", exc)
    return None


def _try_collector_discovery(asset: str = "BTC") -> list[dict[str, Any]] | None:
    """If the production collector module is present, reuse its discovery."""
    asset = str(asset).upper()
    collector_paths = [
        "/root/projects/trading/data/poly-data/collectors/poly_updown",
    ]
    for path in collector_paths:
        if path not in __import__("sys").path:
            try:
                __import__("sys").path.insert(0, path)
                mod = __import__("poly_updown_collector")
                raw_markets = mod.discover_markets()
                results = []
                seen = set()
                for m in raw_markets:
                    if m.get("asset") != asset:
                        continue
                    cid = m.get("condition_id")
                    if not cid or cid in seen:
                        continue
                    seen.add(cid)
                    duration = m.get("duration", "unknown")
                    end_date_iso = m.get("end_date")
                    ws_dt = _window_start(asset, duration, end_date_iso)
                    start_date_iso = m.get("start_date")
                    if ws_dt is not None:
                        start_date_iso = ws_dt.isoformat().replace("+00:00", "Z")
                    fee_schedule = m.get("fee_schedule")
                    resolution_source = m.get("resolution_source")
                    open_oracle_price = _open_oracle_price(asset, start_date_iso)

                    # Enrich from Gamma slug if collector module lacks metadata.
                    if not fee_schedule or not resolution_source or open_oracle_price is None:
                        slug = _slug_for_market(asset, duration, end_date_iso)
                        if slug:
                            for enriched in _fetch_slug(slug, asset=asset):
                                if enriched.get("condition_id") == cid:
                                    fee_schedule = fee_schedule or enriched.get("fee_schedule")
                                    resolution_source = resolution_source or enriched.get("resolution_source")
                                    open_oracle_price = open_oracle_price or enriched.get("open_oracle_price")
                                    break

                    if open_oracle_price is None and start_date_iso:
                        open_oracle_price = _open_oracle_price(asset, start_date_iso)

                    results.append({
                        "condition_id": cid,
                        "token_id_yes": m.get("yes_token"),
                        "token_id_no": m.get("no_token"),
                        "duration": duration,
                        "end_date_iso": end_date_iso,
                        "start_date_iso": start_date_iso,
                        "resolution_source": resolution_source,
                        "fee_schedule": fee_schedule,
                        "open_oracle_price": open_oracle_price,
                        "strike": _extract_strike(m.get("question", "")),
                        "question": m.get("question", ""),
                        "event_title": m.get("slug", ""),
                        "asset": asset,
                    })
                return results
            except Exception as exc:
                logger.debug("Collector discovery import failed: %s", exc)
                continue
    return None


class MarketDiscovery:
    """Lightweight caching discoverer for asset binary option markets."""

    def __init__(self, asset: str = "BTC", ttl_seconds: float = DEFAULT_TTL_SECONDS):
        self._asset = str(asset).upper()
        self._ttl_seconds = ttl_seconds
        self._last_fetch: float = 0.0
        self._cache: list[dict[str, Any]] = []

    def get_markets(self, active_only: bool = True) -> list[dict[str, Any]]:
        now = time.time()
        if now - self._last_fetch > self._ttl_seconds or not self._cache:
            self._cache = self._fetch(active_only=active_only)
            self._last_fetch = now
        return list(self._cache)

    def refresh(self, active_only: bool = True) -> list[dict[str, Any]]:
        self._cache = self._fetch(active_only=active_only)
        self._last_fetch = time.time()
        return list(self._cache)

    def _fetch(self, active_only: bool) -> list[dict[str, Any]]:
        asset = self._asset
        # First preference: the live collector's published asset market list.
        # This guarantees paper-trading uses the exact tokens the collector is
        # subscribed to on the websocket.
        redis_client = _get_redis_client()
        collector_markets = _load_collector_markets_from_redis(redis_client, asset=asset)
        if collector_markets:
            logger.info("Discovered %d %s markets via collector Redis", len(collector_markets), asset)
            return collector_markets

        # Fallback: import the collector module directly and ask it to discover.
        collector_markets = _try_collector_discovery(asset=asset)
        if collector_markets:
            logger.info("Discovered %d %s markets via collector module", len(collector_markets), asset)
            return collector_markets

        # Fallback to slug-based discovery, then events scan.
        markets = _discover_by_slugs(asset=asset)
        if markets:
            logger.info("Discovered %d %s markets via slugs", len(markets), asset)
            return markets

        markets = _discover_by_events(asset=asset)
        if markets:
            logger.info("Discovered %d %s markets via events fallback", len(markets), asset)
        return markets


def _get_redis_client() -> Any | None:
    """Return a Redis client if redis is installed and reachable, else None."""
    try:
        import redis  # local import keeps discovery importable without redis
    except Exception:
        return None
    try:
        return redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)
    except Exception:
        return None


def _cache_key(asset: str) -> str:
    return REDIS_CACHE_KEY_TEMPLATE.format(asset=str(asset).lower())


def _lock_key(asset: str) -> str:
    return REDIS_LOCK_KEY_TEMPLATE.format(asset=str(asset).lower())


def _load_markets_from_cache(
    redis_client: Any,
    asset: str = "BTC",
) -> list[dict[str, Any]] | None:
    """Load cached market list from Redis if present and valid JSON."""
    if redis_client is None:
        return None
    try:
        raw = redis_client.get(_cache_key(asset))
        if not raw:
            return None
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return parsed
    except Exception as exc:
        logger.debug("Failed to load discovery cache: %s", exc)
    return None


def _save_markets_to_cache(
    redis_client: Any,
    markets: list[dict[str, Any]],
    asset: str = "BTC",
) -> None:
    """Write market list to Redis with a short TTL.

    If the market list is missing fee_schedule/start_date/resolution_source,
    use a much shorter TTL so runners retry and pick up enriched data quickly.
    """
    if redis_client is None or not markets:
        return
    ttl = REDIS_CACHE_TTL_SECONDS if _markets_complete(markets) else 30
    try:
        redis_client.set(_cache_key(asset), json.dumps(markets), ex=ttl)
    except Exception as exc:
        logger.debug("Failed to save discovery cache: %s", exc)


def _acquire_discovery_lock(redis_client: Any, asset: str = "BTC") -> bool:
    """Try to acquire a short-lived lock so only one runner fetches from Gamma."""
    if redis_client is None:
        return True  # No Redis; caller must fetch locally.
    try:
        return bool(redis_client.set(_lock_key(asset), "1", nx=True, ex=REDIS_LOCK_TTL_SECONDS))
    except Exception:
        return True  # Fail open: fetch locally if Redis lock fails.


def discover_asset_markets(
    asset: str = "BTC",
    active_only: bool = True,
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
) -> list[dict[str, Any]]:
    """Return a list of active <asset> UP/DOWN market dicts.

    Uses a Redis shared cache per asset to avoid all runners hitting the Gamma
    API at once. If the cache is empty, one runner acquires a lock, fetches the
    markets, and writes them back; the rest wait briefly and then read the cache.
    """
    asset = str(asset).upper()
    redis_client = _get_redis_client()

    cached = _load_markets_from_cache(redis_client, asset=asset)
    if cached is not None:
        if active_only:
            return [m for m in cached if _is_active(m)]
        return cached

    # Cache miss. Try to become the single runner that fetches from the API.
    lock_acquired = _acquire_discovery_lock(redis_client, asset=asset)
    if not lock_acquired:
        # Another runner is fetching. Poll the cache for up to the typical fetch
        # duration so we do not return an empty list during the refresh window.
        for _ in range(15):
            time.sleep(1)
            cached = _load_markets_from_cache(redis_client, asset=asset)
            if cached is not None:
                return cached
        # Still nothing; this is unusual, but return empty rather than joining
        # the thundering herd. The next discovery cycle will retry.
        return []

    markets = MarketDiscovery(asset=asset, ttl_seconds=ttl_seconds).get_markets(active_only=active_only)

    if active_only:
        markets = [m for m in markets if _is_active(m)]

    # Save non-empty results so other runners benefit from this fetch.
    _save_markets_to_cache(redis_client, markets, asset=asset)

    if lock_acquired and redis_client is not None:
        try:
            redis_client.delete(_lock_key(asset))
        except Exception:
            pass

    return markets


def discover_btc_markets(
    active_only: bool = True,
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
) -> list[dict[str, Any]]:
    """Backward-compatible alias for discover_asset_markets('BTC')."""
    return discover_asset_markets(asset="BTC", active_only=active_only, ttl_seconds=ttl_seconds)


__all__ = [
    "MarketDiscovery",
    "discover_asset_markets",
    "discover_btc_markets",
    "GAMMA_EVENTS_URL",
]
