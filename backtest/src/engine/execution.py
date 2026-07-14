# CHANGE_SUMMARY
# 2026-07-02  kilo
#   - Created engine/execution.py with orderbook walk, position sizing, and fee math.
#   - walk_book_buy consumes asks until the target notional is filled.
# 2026-07-03  kilo
#   - Added QUEUE_FILL_FRACTION (default 0.5) so only half of displayed ask size
#     is assumed available to the bot, modeling queue position and competition.
#   - Added ev_slippage_allowance and latency_guard for reality-fidelity checks.
#   - walk_book_buy now returns zero fill for synthetic one-level books so callers
#     are forced to use real depth.
# WHY: Realistic paper fills require full orderbook traversal, strict 2% taker fee
#      accounting, and guards against synthetic/ stale liquidity.

"""Execution primitives: orderbook walk, position sizing, and fee application."""

from __future__ import annotations

import logging
from decimal import Decimal, ROUND_DOWN
from typing import Any

logger = logging.getLogger(__name__)

INITIAL_CAPITAL = 200.0
RISK_PCT = 0.005
MIN_CONTRACTS = 5
TAKER_FEE = 0.02
DEFAULT_MAX_ENTRY_PRICE = 0.85
QUEUE_FILL_FRACTION = 0.5
SYNTHETIC_SIZE_THRESHOLD = 1_000_000.0
# Polymarket fee-aware markets round the taker fee to 5 decimals and enforce a
# minimum fee of 0.00001 USDC (see feeSchedule docs).
FEE_ROUND_DECIMALS = 5
MIN_FEE_USDC = 0.00001
# When only a one-level synthetic book is available (best bid/ask only), assume
# at most this many contracts are available at that level. This prevents the
# engine from treating a top-of-book quote as infinite liquidity while still
# allowing trades when the collector has not yet published a full depth snapshot.
SYNTHETIC_MAX_CONTRACTS = 10.0


def _floor_shares(amount: float, price: float) -> int:
    """Compute whole-contract shares without floating-point floor drift."""
    if amount <= 0 or price <= 0:
        return 0
    dec_amount = Decimal(str(amount))
    dec_price = Decimal(str(price))
    quotient = dec_amount / dec_price
    return int(quotient.to_integral_value(rounding=ROUND_DOWN))


def _fee_rate(fee_schedule: Any) -> float | None:
    """Extract the taker fee rate from a Polymarket feeSchedule object."""
    if isinstance(fee_schedule, dict):
        rate = fee_schedule.get("rate")
        if rate is None:
            rate = fee_schedule.get("takerRate")
        if rate is not None:
            try:
                return float(rate)
            except (ValueError, TypeError):
                return None
    return None


def calculate_taker_fee(
    shares: float,
    price: float,
    fee_schedule: Any = None,
) -> float:
    """
    Return the taker fee in USDC terms for ``shares`` at ``price``.

    Polymarket's fee-aware formula is::

        fee = shares * rate * price * (1 - price)

    rounded to ``FEE_ROUND_DECIMALS`` decimals with a minimum of ``MIN_FEE_USDC``.
    If no ``fee_schedule`` is provided, a default crypto-market rate of 0.07 is
    used so paper trading never silently falls back to the legacy 2% flat fee.
    """
    if shares <= 0 or price <= 0:
        return 0.0
    rate = _fee_rate(fee_schedule)
    if rate is None or rate <= 0.0:
        rate = 0.07
    raw_fee = float(shares) * rate * float(price) * (1.0 - float(price))
    if raw_fee <= 0.0:
        return 0.0
    fee = round(raw_fee + 1e-12, FEE_ROUND_DECIMALS)
    return max(MIN_FEE_USDC, fee)


def taker_fee_shares(gross_shares: float, price: float, fee_schedule: Any = None) -> float:
    """Return the number of shares deducted as taker fee on Polymarket.

    Polymarket subtracts the fee from the share count received on a buy order:
    you pay ``gross_shares * price`` USDC but receive ``gross_shares - fee_shares``.
    """
    if gross_shares <= 0 or price <= 0:
        return 0.0
    fee_usdc = calculate_taker_fee(gross_shares, price, fee_schedule)
    return round(fee_usdc / price, FEE_ROUND_DECIMALS)


def position_notional(
    capital: float,
    entry_price: float,
    risk_pct: float = RISK_PCT,
    min_contracts: int = MIN_CONTRACTS,
) -> float:
    """
    Return the target entry notional for a trade.

    Sizing follows the project rule: risk 0.5% of capital, but always trade at
    least ``min_contracts`` if the risk-based size is smaller. The notional is
    capped by available capital so paper accounts cannot go negative.
    """
    if capital <= 0 or entry_price <= 0:
        return 0.0

    risk_amount = capital * risk_pct
    shares_by_risk = _floor_shares(risk_amount, entry_price)
    shares = max(min_contracts, shares_by_risk)

    max_affordable = _floor_shares(capital, entry_price)
    if max_affordable < shares:
        if max_affordable < 1:
            return 0.0
        shares = max_affordable

    return shares * entry_price


def position_size(
    capital: float,
    entry_price: float,
    risk_pct: float = RISK_PCT,
    min_contracts: int = MIN_CONTRACTS,
) -> int:
    """Return the integer number of contracts to buy."""
    notional = position_notional(capital, entry_price, risk_pct, min_contracts)
    if notional <= 0 or entry_price <= 0:
        return 0
    return _floor_shares(notional, entry_price)


def is_synthetic_book(book: dict[str, Any] | None) -> bool:
    """Detect the synthetic one-level placeholder books built from top-of-book."""
    if not book:
        return False
    raw = book.get("raw") or {}
    if raw.get("synthetic") is not True:
        return False
    asks = book.get("asks") or []
    bids = book.get("bids") or []
    if len(asks) != 1 or len(bids) != 1:
        return False
    _, ask_size = asks[0]
    _, bid_size = bids[0]
    return ask_size >= SYNTHETIC_SIZE_THRESHOLD or bid_size >= SYNTHETIC_SIZE_THRESHOLD


def walk_book_buy(
    book: dict[str, Any] | None,
    target_notional: float,
    queue_fraction: float = QUEUE_FILL_FRACTION,
    fee_schedule: Any = None,
) -> dict[str, float]:
    """
    Walk the ask side of a normalized orderbook to fill ``target_notional``.

    Only ``queue_fraction`` of each displayed level is assumed available to the
    bot, modeling queue position and competition. Synthetic one-level books
    (built from best bid/ask only) are allowed but capped at
    ``SYNTHETIC_MAX_CONTRACTS`` so they do not provide fantasy liquidity.

    Returns a dict with:
        avg_fill_price: volume-weighted average fill price
        shares: gross contracts filled
        fee: taker fee in USDC using the market's feeSchedule
        notional: filled notional (USDC paid)
        fee_shares: shares deducted as fee

    If liquidity is insufficient, the function returns partial fill results.
    """
    if not book or target_notional <= 0:
        return {"avg_fill_price": 0.0, "shares": 0.0, "fee": 0.0, "notional": 0.0, "fee_shares": 0.0}

    is_synthetic = is_synthetic_book(book)
    asks = book.get("asks") or []
    remaining = float(target_notional)
    filled_notional = 0.0
    shares_filled = 0.0
    queue_fraction = max(0.0, min(1.0, float(queue_fraction)))

    for price, size in asks:
        if remaining <= 0 or price <= 0 or size <= 0:
            break

        if is_synthetic:
            # Cap synthetic books to a small number of contracts; queue_fraction
            # still applies so we model queue position even on synthetic levels.
            available_size = min(size, SYNTHETIC_MAX_CONTRACTS) * queue_fraction
        else:
            available_size = size * queue_fraction
        level_notional = price * available_size
        take_notional = min(remaining, level_notional)
        take_shares = take_notional / price

        shares_filled += take_shares
        filled_notional += take_notional
        remaining -= take_notional

    avg_fill_price = filled_notional / shares_filled if shares_filled > 0 else 0.0
    fee = calculate_taker_fee(shares_filled, avg_fill_price, fee_schedule)
    fee_shares = taker_fee_shares(shares_filled, avg_fill_price, fee_schedule)
    return {
        "avg_fill_price": avg_fill_price,
        "shares": shares_filled,
        "fee": fee,
        "notional": filled_notional,
        "fee_shares": fee_shares,
    }


def ev_slippage_allowance(
    signal: dict[str, Any],
    avg_fill_price: float,
    fair_price: float | None = None,
) -> bool:
    """
    Return True if the realized slippage is within half the expected edge.

    The expected edge must come from either a caller-supplied ``fair_price``
    (distance between fair mid and requested entry) or an explicit
    ``signal["edge"]`` price-edge fraction.  ``signal["confidence"]`` is
    intentionally ignored here because strategies use it as a probability/
    confidence score, not as a price-edge distance.

    Slippage is capped at $0.03 regardless of edge.
    """
    entry_price = float(signal.get("entry_price") or 0.0)
    if entry_price <= 0 or avg_fill_price <= 0:
        return False

    edge: float | None = None
    if fair_price is not None:
        edge = abs(float(fair_price) - entry_price)
    elif signal.get("edge") is not None:
        edge = float(signal["edge"])

    if edge is None:
        return False

    slippage = abs(avg_fill_price - entry_price)
    max_slippage = min(0.03, edge * 0.5)
    return slippage <= max_slippage


def latency_guard(
    current_book: dict[str, Any] | None,
    signal_book: dict[str, Any] | None,
    direction: str,
    tolerance: float = 0.03,
) -> bool:
    """
    Reject entry if the best ask moved against us between signal and now.

    For YES entries we compare the YES best ask; for NO entries we compare the
    NO best ask. A move greater than ``tolerance`` against the bot is a reject.
    """
    if not current_book or not signal_book:
        return False

    current_asks = current_book.get("asks") or []
    signal_asks = signal_book.get("asks") or []
    if not current_asks or not signal_asks:
        return False

    current_best = float(current_asks[0][0]) if current_asks else 0.0
    signal_best = float(signal_asks[0][0]) if signal_asks else 0.0
    if current_best <= 0 or signal_best <= 0:
        return False

    # For both YES and NO, an increase in the ask we must lift is adverse.
    return (current_best - signal_best) <= float(tolerance)


def entry_cost(avg_fill_price: float, shares: float) -> float:
    """Total cash required to enter a position including the taker fee."""
    notional = avg_fill_price * shares
    return notional * (1.0 + TAKER_FEE)


__all__ = [
    "INITIAL_CAPITAL",
    "RISK_PCT",
    "MIN_CONTRACTS",
    "TAKER_FEE",
    "DEFAULT_MAX_ENTRY_PRICE",
    "QUEUE_FILL_FRACTION",
    "SYNTHETIC_SIZE_THRESHOLD",
    "FEE_ROUND_DECIMALS",
    "MIN_FEE_USDC",
    "calculate_taker_fee",
    "taker_fee_shares",
    "position_notional",
    "position_size",
    "walk_book_buy",
    "is_synthetic_book",
    "ev_slippage_allowance",
    "latency_guard",
    "entry_cost",
]
