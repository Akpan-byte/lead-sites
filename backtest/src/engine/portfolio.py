# CHANGE_SUMMARY
# 2026-07-02  kilo
#   - Created engine/portfolio.py to track active trades and handle exits.
#   - Entry respects max price, reentry limits, and capital. Exits snipe at 0.99 or resolve at expiry.
# 2026-07-02  kilo
#   - perf_dict() now includes name, total_trades (active + closed), and open_positions.
# 2026-07-02  runner-diagnostics-fix
#   - Hardened check_exits() against None prices/rem_sec so a transient Redis miss
#     does not crash the market loop.
# 2026-07-03  kilo
#   - Added entry_spot to Trade so expiry resolution works for up/down-over-window
#     markets that have no fixed strike.
#   - check_entry now accepts entry_spot from the runner and stores it on the trade.
#   - check_exits resolves YES/NO against entry_spot when available, falling back to strike.
#   - Added Portfolio.force_exit() for closing positions whose markets dropped from discovery.
# 2026-07-03  kilo
#   - Dropped EXIT_SNIPE_PRICE from 0.99 to 0.97 for both YES and NO exits.
#   - check_entry now accepts latency_book, fair_price, and queue_fraction.
#   - Added synthetic-book, EV slippage, and latency guards to check_entry.
# 2026-07-03  kilo
#   - check_entry now uses max_notional as the spendable cap when rounding up to
#     min_contracts and scaling down oversized fills. This prevents a runner from
#     spending more from the global pool than it reserved.
# 2026-07-05  kilo
#   - Expiry resolution now prefers the market-open/start-of-window oracle price
#     (market["open_oracle_price"]) and uses a Chainlink oracle spot when supplied.
#   - Entry/exit fees now use the market's feeSchedule when present, matching
#     Polymarket's crypto fee formula: shares * rate * price * (1 - price).
#   - Added Trade.exit_fee and applied exit taker fees to proceeds and PnL.
# 2026-07-05  kilo
#   - Added MIN_ENTRY_PRICE = 0.05 guard so the bot never enters positions at
#     prices that imply an already-resolved market (e.g., 0.001 lottery tickets).
# 2026-07-05  kilo
#   - Phase 1 accounting fixes:
#     * PnL now subtracts both entry_fee and exit_fee.
#     * Added ``_cash_derived`` flag and ``cash_adjustment_log`` for ledger repairs.
#     * ``state_dict`` recomputes cash from the ledger when ``_cash_derived`` is True.
#     * ``load_state`` enforces the invariant ``cash == initial_capital + total_pnl - committed``
#       and repairs the ledger when the stored cash diverges by more than $0.05.
#     * Documented that ``equity()`` marks active positions at entry price unless
#       ``mark_prices`` is supplied.
# WHY: Phase 1 accounting fix. Cash must stay consistent with realized PnL and
#      committed capital. The previous PnL formula ignored the entry fee and state
#      files were allowed to drift from the ledger invariant.

"""Position tracking, entry sizing, and exit logic for one strategy/stack wallet."""

from __future__ import annotations

import logging
import time
from typing import Any

from engine.execution import (
    DEFAULT_MAX_ENTRY_PRICE,
    MIN_CONTRACTS,
    RISK_PCT,
    calculate_taker_fee,
    ev_slippage_allowance,
    latency_guard,
    position_notional,
    taker_fee_shares,
    walk_book_buy,
)

logger = logging.getLogger(__name__)

EXIT_SNIPE_PRICE = 0.97
# Minimum entry price for a position.  Prices below this floor usually mean
# the market is already resolved, the quote is stale, or the outcome is
# effectively certain; allowing entries here produces lottery-ticket PnL that
# would not happen in live trading.
MIN_ENTRY_PRICE = 0.05


class Trade:
    """One open or closed paper trade."""

    def __init__(
        self,
        condition_id: str,
        direction: str,
        entry_price: float,
        shares: float,
        entry_notional: float,
        entry_fee: float,
        market: dict[str, Any],
        opened_at: float | None = None,
        entry_spot: float | None = None,
        fee_shares: float = 0.0,
        adds: int = 0,
    ):
        self.condition_id = condition_id
        self.direction = direction.upper()
        self.entry_price = entry_price
        self.shares = shares
        self.entry_notional = entry_notional
        self.entry_fee = entry_fee
        self.fee_shares = fee_shares
        # Number of same-direction scale-in adds merged into this position after
        # the initial entry. 0 means a fresh (un-scaled) position.
        self.adds = int(adds)
        self.market = market
        self.opened_at = opened_at or time.time()
        self.entry_spot = entry_spot
        self.closed_at: float | None = None
        self.exit_price: float | None = None
        self.exit_fee: float = 0.0
        self.pnl = 0.0
        self.exit_reason = ""

    # Market metadata fields that must survive restarts so expiry resolution and
    # fee accounting remain correct after a deploy/restart.
    _MARKET_PERSIST_FIELDS = (
        "condition_id", "token_id_yes", "token_id_no", "duration", "end_date_iso",
        "start_date_iso", "resolution_source", "fee_schedule", "open_oracle_price",
        "strike", "question", "event_title", "asset",
    )

    def net_shares(self) -> float:
        """Return shares actually held after taker-fee share deduction."""
        return max(0.0, self.shares - self.fee_shares)

    def to_dict(self) -> dict[str, Any]:
        return {
            "condition_id": self.condition_id,
            "direction": self.direction,
            "entry_price": self.entry_price,
            "shares": self.shares,
            "fee_shares": self.fee_shares,
            "entry_notional": self.entry_notional,
            "entry_fee": self.entry_fee,
            "entry_spot": self.entry_spot,
            "exit_price": self.exit_price,
            "exit_fee": self.exit_fee,
            "opened_at": self.opened_at,
            "closed_at": self.closed_at,
            "pnl": self.pnl,
            "exit_reason": self.exit_reason,
            "adds": self.adds,
            "market": {k: self.market.get(k) for k in self._MARKET_PERSIST_FIELDS},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], market: dict[str, Any]) -> "Trade":
        """Reconstruct a Trade from a state-dict snapshot.

        If the snapshot contains a persisted market dict, use it; otherwise fall
        back to the minimal market passed by the caller.  Legacy snapshots that
        lack fee_shares default to 0.0 (pre-share-deduction behaviour).
        """
        persisted = data.get("market") or {}
        merged_market = {**market, **persisted}
        trade = cls(
            condition_id=data["condition_id"],
            direction=data["direction"],
            entry_price=data["entry_price"],
            shares=data["shares"],
            entry_notional=data["entry_notional"],
            entry_fee=data["entry_fee"],
            market=merged_market,
            opened_at=data.get("opened_at"),
            entry_spot=data.get("entry_spot"),
            fee_shares=data.get("fee_shares", 0.0) or 0.0,
        )
        trade.exit_price = data.get("exit_price")
        trade.exit_fee = data.get("exit_fee", 0.0) or 0.0
        trade.closed_at = data.get("closed_at")
        trade.pnl = data.get("pnl", 0.0)
        trade.exit_reason = data.get("exit_reason", "")
        trade.adds = int(data.get("adds", 0) or 0)
        return trade


class Portfolio:
    """
    Track active trades for a single strategy or stack wallet.

    Only one trade per ``condition_id`` is allowed at a time. Reentries are
    gated by ``allow_reentry`` and ``max_reentries``.
    """

    def __init__(self, capital: float = 200.0, risk_pct: float = RISK_PCT, min_contracts: int = MIN_CONTRACTS, name: str = ""):
        self.name = name
        self._initial_capital = float(capital)
        self._cash = float(capital)
        self.risk_pct = risk_pct
        self.min_contracts = min_contracts
        self.active_trades: dict[str, Trade] = {}
        self.closed_trades: list[Trade] = []
        self._entry_count: dict[str, int] = {}
        # Per-contract count of same-direction scale-in adds applied to the open
        # position (separate from _entry_count, which counts post-close re-entries).
        self._add_count: dict[str, int] = {}
        # Phase 1: when True, state_dict derives cash from the ledger instead of
        # trusting the stored value. Set after a load_state repair or any manual
        # ledger adjustment.
        self._cash_derived = False
        # Record of any manual cash repairs so auditors can see why cash moved.
        self._cash_adjustment_log: list[dict[str, Any]] = []

    @property
    def cash(self) -> float:
        return self._cash

    @property
    def committed_notional(self) -> float:
        return sum(t.entry_notional for t in self.active_trades.values())

    def equity(self, mark_prices: dict[str, float] | None = None) -> float:
        """
        Approximate equity using last known mark prices for active trades.

        Active positions are marked at the supplied ``mark_prices`` when
        available. If a mark price is missing for a position, the position is
        valued at its entry price (conservative: no unrealized PnL). Pass
        mark_prices to include unrealized gains/losses.
        """
        total = self._cash
        for condition_id, trade in self.active_trades.items():
            if mark_prices and condition_id in mark_prices:
                price = mark_prices[condition_id]
            else:
                price = trade.entry_price
            total += trade.net_shares() * price
        return total

    def has_position(self, condition_id: str) -> bool:
        return condition_id in self.active_trades

    def check_entry(
        self,
        signal: dict[str, Any],
        market: dict[str, Any],
        book: dict[str, Any] | None,
        max_entry_price: float = DEFAULT_MAX_ENTRY_PRICE,
        allow_reentry: bool = False,
        max_reentries: int = 0,
        slippage_tolerance: float = 0.01,
        entry_spot: float | None = None,
        latency_book: dict[str, Any] | None = None,
        fair_price: float | None = None,
        queue_fraction: float = 1.0,
        max_notional: float | None = None,
    ) -> Trade | None:
        """
        Attempt to open a position from a signal.

        ``max_notional`` lets a global capital pool cap the trade size so the
        portfolio does not spend more than was reserved from the shared wallet.

        Returns the new Trade on success, or None if no entry occurred.
        """
        if not signal.get("triggered"):
            return None

        direction = signal.get("direction")
        if direction not in ("YES", "NO"):
            return None

        condition_id = market.get("condition_id")
        if not condition_id:
            return None

        if condition_id in self.active_trades:
            return None

        # Do not enter trades on markets that have already expired. The
        # resolution is not a tradable entry and would resolve immediately.
        end_date_iso = market.get("end_date_iso")
        if end_date_iso:
            try:
                from datetime import datetime, timezone
                end_dt = datetime.fromisoformat(str(end_date_iso).replace("Z", "+00:00"))
                if datetime.now(timezone.utc) >= end_dt:
                    return None
            except (ValueError, TypeError):
                pass

        entries_so_far = self._entry_count.get(condition_id, 0)
        if entries_so_far > 0 and (not allow_reentry or entries_so_far > max_reentries):
            return None

        signal_entry_price = float(signal.get("entry_price", 0.0))
        if signal_entry_price <= 0:
            return None

        # Reject lottery-ticket entries on prices that imply a resolved market.
        if signal_entry_price < MIN_ENTRY_PRICE:
            logger.debug(
                "Signal entry price %.4f below MIN_ENTRY_PRICE %.4f for %s",
                signal_entry_price, MIN_ENTRY_PRICE, condition_id,
            )
            return None

        # Use the stricter of the signal's requested price or the caller's cap.
        entry_cap = min(max_entry_price, signal_entry_price + slippage_tolerance)
        if signal_entry_price > max_entry_price:
            logger.debug("Signal entry price %.3f above cap %.3f for %s", signal_entry_price, max_entry_price, condition_id)
            return None

        # Signals may request scaled size (e.g., ORB re-entries). Apply the scale
        # after the base risk-sized notional so re-entries are larger but still bounded by cash.
        size_scale = float(signal.get("size_scale", 1.0))
        target_notional = position_notional(
            self._cash, signal_entry_price, self.risk_pct, self.min_contracts
        ) * size_scale
        # Honor a global capital-pool cap so we do not spend more than reserved.
        if max_notional is not None:
            target_notional = min(target_notional, float(max_notional))
        if target_notional <= 0:
            return None

        fee_schedule = market.get("fee_schedule")
        fill = walk_book_buy(book, target_notional, queue_fraction=queue_fraction, fee_schedule=fee_schedule)
        avg_fill = fill.get("avg_fill_price", 0.0)
        gross_shares = fill.get("shares", 0.0)
        if gross_shares <= 0 or avg_fill <= 0:
            logger.debug("No book liquidity for %s", condition_id)
            return None

        if not ev_slippage_allowance(signal, avg_fill, fair_price=fair_price):
            logger.debug(
                "EV slippage guard rejected %s: avg_fill=%.4f entry_price=%.4f",
                condition_id, avg_fill, signal_entry_price,
            )
            return None

        if not latency_guard(latency_book, book, direction):
            logger.debug("Latency guard rejected %s", condition_id)
            return None

        # Polymarket deducts the taker fee in shares, not extra USDC.  Compute
        # the gross order and the net position received.
        entry_fee = fill.get("fee", 0.0)
        fee_shares = fill.get("fee_shares", 0.0)
        net_shares = max(0.0, gross_shares - fee_shares)

        # Round to hundredth of a share as requested.
        gross_shares = round(gross_shares, 2)
        fee_shares = round(fee_shares, 2)
        net_shares = round(net_shares, 2)

        # Risk sizing uses the signal's expected price, but the actual book walk
        # may fill slightly fewer contracts.  Enforce the minimum on the gross
        # order size; the net position will be slightly smaller due to the fee.
        spendable = min(self._cash, float(max_notional)) if max_notional is not None else self._cash
        if 0 < gross_shares < self.min_contracts:
            needed_cost = avg_fill * self.min_contracts
            if needed_cost <= spendable:
                gross_shares = float(self.min_contracts)
                entry_fee = calculate_taker_fee(gross_shares, avg_fill, fee_schedule)
                fee_shares = round(taker_fee_shares(gross_shares, avg_fill, fee_schedule), 2)
                net_shares = round(max(0.0, gross_shares - fee_shares), 2)
            else:
                logger.debug(
                    "Fill too small for %s: gross_shares=%.2f < min_contracts=%d",
                    condition_id, gross_shares, self.min_contracts,
                )
                return None

        if avg_fill > entry_cap:
            logger.debug(
                "Average fill %.3f exceeds entry cap %.3f for %s", avg_fill, entry_cap, condition_id
            )
            return None

        gross_notional = avg_fill * gross_shares
        if gross_notional > spendable:
            # Scale down to available cash while keeping the minimum gross order.
            max_gross = int(spendable / avg_fill)
            if max_gross < self.min_contracts:
                return None
            gross_shares = float(max_gross)
            entry_fee = calculate_taker_fee(gross_shares, avg_fill, fee_schedule)
            fee_shares = round(taker_fee_shares(gross_shares, avg_fill, fee_schedule), 2)
            net_shares = round(max(0.0, gross_shares - fee_shares), 2)
            gross_notional = avg_fill * gross_shares

        trade = Trade(
            condition_id=condition_id,
            direction=direction,
            entry_price=avg_fill,
            shares=gross_shares,
            entry_notional=gross_notional,
            entry_fee=entry_fee,
            fee_shares=fee_shares,
            market=market,
            entry_spot=signal.get("entry_spot") or entry_spot,
        )
        # We pay gross_notional USDC and receive net_shares of the position.
        self._cash -= gross_notional
        self.active_trades[condition_id] = trade
        self._entry_count[condition_id] = entries_so_far + 1
        logger.info(
            "ENTER %s %s gross_shares=%.2f net_shares=%.2f price=%.4f notional=%.2f fee=%.4f fee_shares=%.2f",
            condition_id,
            direction,
            gross_shares,
            net_shares,
            avg_fill,
            trade.entry_notional,
            trade.entry_fee,
            fee_shares,
        )
        return trade

    def add_count(self, condition_id: str) -> int:
        """Number of same-direction scale-in adds already merged on a contract."""
        return self._add_count.get(condition_id, 0)

    def scale_in(
        self,
        signal: dict[str, Any],
        market: dict[str, Any],
        book: dict[str, Any] | None,
        max_entry_price: float = DEFAULT_MAX_ENTRY_PRICE,
        max_adds: int = 0,
        slippage_tolerance: float = 0.01,
        entry_spot: float | None = None,
        latency_book: dict[str, Any] | None = None,
        fair_price: float | None = None,
        queue_fraction: float = 1.0,
        max_notional: float | None = None,
    ) -> Trade | None:
        """Merge additional same-direction size into an already-open position.

        This is "doubling down": if we are already holding YES and another YES
        signal fires, we buy more contracts and merge them into the SAME trade at
        a volume-weighted average entry price. Opposite-direction signals are
        ignored (no flip, no hedge). The ledger cash identity is preserved
        exactly as in check_entry: cash falls by the add's gross notional and the
        active trade's entry_notional rises by the same amount.

        Returns the updated Trade on success, or None if the add was not taken.
        """
        if not signal.get("triggered"):
            return None
        direction = signal.get("direction")
        if direction not in ("YES", "NO"):
            return None
        condition_id = market.get("condition_id")
        if not condition_id:
            return None

        trade = self.active_trades.get(condition_id)
        if trade is None:
            return None
        # Same-direction only: never flip or hedge an open position.
        if trade.direction != direction:
            return None
        # Add cap (max_adds very large == effectively unlimited; still bounded by
        # the number of same-direction breakouts before exit and by the pool).
        if self._add_count.get(condition_id, 0) >= max_adds:
            return None

        signal_entry_price = float(signal.get("entry_price", 0.0))
        if signal_entry_price <= 0 or signal_entry_price < MIN_ENTRY_PRICE:
            return None
        entry_cap = min(max_entry_price, signal_entry_price + slippage_tolerance)
        if signal_entry_price > max_entry_price:
            return None

        size_scale = float(signal.get("size_scale", 1.0))
        target_notional = position_notional(
            self._cash, signal_entry_price, self.risk_pct, self.min_contracts
        ) * size_scale
        if max_notional is not None:
            target_notional = min(target_notional, float(max_notional))
        if target_notional <= 0:
            return None

        fee_schedule = market.get("fee_schedule")
        fill = walk_book_buy(book, target_notional, queue_fraction=queue_fraction, fee_schedule=fee_schedule)
        avg_fill = fill.get("avg_fill_price", 0.0)
        gross_shares = fill.get("shares", 0.0)
        if gross_shares <= 0 or avg_fill <= 0:
            return None
        if not ev_slippage_allowance(signal, avg_fill, fair_price=fair_price):
            return None
        if not latency_guard(latency_book, book, direction):
            return None

        entry_fee = fill.get("fee", 0.0)
        fee_shares = fill.get("fee_shares", 0.0)
        net_shares = max(0.0, gross_shares - fee_shares)
        gross_shares = round(gross_shares, 2)
        fee_shares = round(fee_shares, 2)
        net_shares = round(net_shares, 2)

        spendable = min(self._cash, float(max_notional)) if max_notional is not None else self._cash
        if 0 < gross_shares < self.min_contracts:
            needed_cost = avg_fill * self.min_contracts
            if needed_cost <= spendable:
                gross_shares = float(self.min_contracts)
                entry_fee = calculate_taker_fee(gross_shares, avg_fill, fee_schedule)
                fee_shares = round(taker_fee_shares(gross_shares, avg_fill, fee_schedule), 2)
                net_shares = round(max(0.0, gross_shares - fee_shares), 2)
            else:
                return None
        if avg_fill > entry_cap:
            return None

        gross_notional = avg_fill * gross_shares
        if gross_notional > spendable:
            max_gross = int(spendable / avg_fill)
            if max_gross < self.min_contracts:
                return None
            gross_shares = float(max_gross)
            entry_fee = calculate_taker_fee(gross_shares, avg_fill, fee_schedule)
            fee_shares = round(taker_fee_shares(gross_shares, avg_fill, fee_schedule), 2)
            net_shares = round(max(0.0, gross_shares - fee_shares), 2)
            gross_notional = avg_fill * gross_shares

        # Merge into the open position at a gross-notional-weighted average price.
        trade.shares = round(trade.shares + gross_shares, 2)
        trade.fee_shares = round(trade.fee_shares + fee_shares, 2)
        trade.entry_notional = trade.entry_notional + gross_notional
        trade.entry_fee = trade.entry_fee + entry_fee
        # Weighted avg entry price = total notional / total gross shares, since each
        # leg's notional == avg_fill * gross_shares for that leg.
        trade.entry_price = trade.entry_notional / trade.shares if trade.shares > 0 else trade.entry_price
        trade.adds += 1
        self._cash -= gross_notional
        self._add_count[condition_id] = self._add_count.get(condition_id, 0) + 1
        logger.info(
            "SCALEIN %s %s add#%d gross_shares=%.2f net_shares=%.2f price=%.4f add_notional=%.2f total_notional=%.2f avg_price=%.4f",
            condition_id,
            direction,
            trade.adds,
            gross_shares,
            net_shares,
            avg_fill,
            gross_notional,
            trade.entry_notional,
            trade.entry_price,
        )
        return trade

    def check_exits(
        self,
        spot: float,
        yp: float,
        np_val: float,
        rem_sec: float,
        oracle_spot: float | None = None,
    ) -> list[Trade]:
        """Evaluate active trades and close any that hit EXIT_SNIPE_PRICE or expiry.

        ``oracle_spot`` is the Chainlink RTDS reference price. When provided it
        is used for expiry resolution instead of the regular ``spot`` feed.
        """
        closed: list[Trade] = []
        # Defensive: callers may pass None when Redis prices are missing. Treat
        # missing prices as 0.0 so the comparison does not raise.
        yp = float(yp) if yp is not None else 0.0
        np_val = float(np_val) if np_val is not None else 0.0
        rem_sec = float(rem_sec) if rem_sec is not None else 0.0
        resolve_spot = oracle_spot if oracle_spot is not None else spot
        for condition_id, trade in list(self.active_trades.items()):
            exit_price: float | None = None
            reason = ""

            # Expiry resolution: current Polymarket UP/DOWN markets are
            # "up or down over the window" contracts with no fixed strike. Prefer
            # the market-open/start-of-window oracle price when available; fall
            # back to the spot at trade entry, then to the market's strike.
            reference = trade.market.get("open_oracle_price") or trade.entry_spot
            if reference is None:
                reference = trade.market.get("strike", 0.0) or 0.0

            if trade.direction == "YES":
                if yp >= EXIT_SNIPE_PRICE:
                    exit_price = EXIT_SNIPE_PRICE
                    reason = "snipe_yes_0.97"
                elif rem_sec <= 0:
                    exit_price = 1.0 if resolve_spot >= reference else 0.0
                    reason = "expiry_resolve"
            else:  # NO
                if np_val >= EXIT_SNIPE_PRICE:
                    exit_price = EXIT_SNIPE_PRICE
                    reason = "snipe_no_0.97"
                elif rem_sec <= 0:
                    exit_price = 1.0 if resolve_spot < reference else 0.0
                    reason = "expiry_resolve"

            if exit_price is not None:
                self._close_trade(trade, exit_price, reason)
                closed.append(trade)
        return closed

    def _close_trade(self, trade: Trade, exit_price: float, reason: str) -> None:
        trade.exit_price = exit_price
        trade.closed_at = time.time()
        trade.exit_reason = reason
        net_shares = trade.net_shares()
        # Apply the market's fee schedule to the exit side as well. For expiry
        # resolutions at 1.0 or 0.0 the Polymarket formula yields zero fee.
        trade.exit_fee = calculate_taker_fee(
            net_shares, exit_price, trade.market.get("fee_schedule")
        )
        # Phase 1: PnL includes both entry and exit fees. The entry fee is the
        # USDC-equivalent taker fee paid via share deduction; subtracting it here
        # makes the ledger consistent with the cash invariant.
        trade.pnl = net_shares * (exit_price - trade.entry_price) - trade.entry_fee - trade.exit_fee
        proceeds = net_shares * exit_price - trade.exit_fee
        self._cash += proceeds
        del self.active_trades[trade.condition_id]
        self.closed_trades.append(trade)
        logger.info(
            "EXIT %s %s gross_shares=%.2f net_shares=%.2f entry=%.4f exit=%.4f fee=%.4f pnl=%.2f reason=%s",
            trade.condition_id,
            trade.direction,
            trade.shares,
            net_shares,
            trade.entry_price,
            exit_price,
            trade.exit_fee,
            trade.pnl,
            reason,
        )

    def force_exit(
        self, condition_id: str, exit_price: float, reason: str
    ) -> Trade | None:
        """Close an active trade by condition_id. Returns the closed Trade or None."""
        trade = self.active_trades.get(condition_id)
        if trade is None:
            return None
        self._close_trade(trade, exit_price, reason)
        return trade

    def state_dict(self) -> dict[str, Any]:
        total_pnl = sum(t.pnl for t in self.closed_trades)
        committed = sum(t.entry_notional for t in self.active_trades.values())
        # Phase 1: derive cash from the ledger when the stored value has been
        # flagged as repaired. This guarantees the invariant after a load_state
        # repair or any manual adjustment.
        if self._cash_derived:
            cash = self._initial_capital + total_pnl - committed
        else:
            cash = self._cash
        return {
            "cash": round(cash, 4),
            "initial_capital": self._initial_capital,
            "active_count": len(self.active_trades),
            "closed_count": len(self.closed_trades),
            "total_pnl": round(total_pnl, 4),
            "active_trades": {cid: t.to_dict() for cid, t in self.active_trades.items()},
            "closed_trades": [t.to_dict() for t in self.closed_trades],
            # Phase 1: expose any ledger repairs for diagnostics.
            "cash_adjustment_log": list(self._cash_adjustment_log),
            "_cash_derived": self._cash_derived,
        }

    def load_state(self, state: dict[str, Any]) -> None:
        """Restore portfolio state from a previous state_dict snapshot.

        Phase 1: after restoring trades, verify the cash invariant
        ``cash == initial_capital + sum(closed.pnl) - sum(active.entry_notional)``.
        If stored cash diverges by more than $0.05, repair the ledger, log the
        adjustment, and mark cash as derived so the next state_dict writes the
        corrected value.
        """
        self._cash = float(state.get("cash", self._cash))
        self._initial_capital = float(state.get("initial_capital", self._initial_capital))

        # Restore closed trades for PnL history and statistics.
        self.closed_trades = []
        for t in state.get("closed_trades", []):
            try:
                persisted = t.get("market") or {"condition_id": t.get("condition_id", "")}
                trade = Trade.from_dict(t, market=persisted)
                # Phase 1: recompute closed-trade PnL so historical trades use the
                # corrected formula that includes both entry and exit fees. This
                # keeps the ledger invariant true after the formula change.
                if trade.exit_price is not None:
                    trade.pnl = (
                        trade.net_shares() * (trade.exit_price - trade.entry_price)
                        - trade.entry_fee
                        - trade.exit_fee
                    )
                self.closed_trades.append(trade)
            except Exception:
                # Skip corrupt rows; a partial restore is better than a crash.
                pass

        # Restore active trades. Use the persisted market metadata so expiry
        # resolution and fee accounting remain correct after a restart.
        self.active_trades = {}
        for cid, t in state.get("active_trades", {}).items():
            try:
                persisted = t.get("market") or {"condition_id": cid}
                self.active_trades[cid] = Trade.from_dict(t, market=persisted)
            except Exception:
                pass

        # Rebuild entry counts so reentry rules remain consistent across restarts.
        self._entry_count = {}
        for t in self.active_trades.values():
            self._entry_count[t.condition_id] = self._entry_count.get(t.condition_id, 0) + 1
        for t in self.closed_trades:
            self._entry_count[t.condition_id] = self._entry_count.get(t.condition_id, 0) + 1

        # Rebuild scale-in add counts from the persisted open positions so a
        # restart cannot grant extra adds beyond the cap.
        self._add_count = {}
        for t in self.active_trades.values():
            if getattr(t, "adds", 0):
                self._add_count[t.condition_id] = int(t.adds)

        # Phase 1 invariant check and repair.
        total_pnl = sum(t.pnl for t in self.closed_trades)
        committed = sum(t.entry_notional for t in self.active_trades.values())
        expected_cash = self._initial_capital + total_pnl - committed
        if abs(self._cash - expected_cash) > 0.05:
            adjustment = expected_cash - self._cash
            logger.warning(
                "CASH_INVARIANT_REPAIR portfolio=%s stored=%.4f expected=%.4f adjustment=%+.4f",
                self.name, self._cash, expected_cash, adjustment,
            )
            self._cash = expected_cash
            self._cash_derived = True
            self._cash_adjustment_log.append({
                "ts": time.time(),
                "reason": "load_state invariant repair",
                "stored_cash": round(float(state.get("cash", 0.0)), 4),
                "expected_cash": round(expected_cash, 4),
                "adjustment": round(adjustment, 4),
            })

        # Carry forward any prior adjustment log from the snapshot.
        if state.get("cash_adjustment_log"):
            self._cash_adjustment_log.extend(state["cash_adjustment_log"])

    def perf_dict(self) -> dict[str, Any]:
        wins = sum(1 for t in self.closed_trades if t.pnl > 0)
        losses = sum(1 for t in self.closed_trades if t.pnl <= 0)
        total_pnl = sum(t.pnl for t in self.closed_trades)
        avg_pnl = total_pnl / len(self.closed_trades) if self.closed_trades else 0.0
        active_count = len(self.active_trades)
        closed_count = len(self.closed_trades)
        return {
            "timestamp_utc": time.time(),
            "name": self.name,
            "initial_capital": self._initial_capital,
            "cash": round(self._cash, 4),
            "active_count": active_count,
            "closed_count": closed_count,
            # total_trades and open_positions are explicit aliases used by the dashboard aggregator.
            "total_trades": active_count + closed_count,
            "open_positions": active_count,
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / len(self.closed_trades), 4) if self.closed_trades else 0.0,
            "total_pnl": round(total_pnl, 4),
            "avg_pnl": round(avg_pnl, 4),
            "equity": round(self.equity(), 4),
        }

    # Aliases used by generated runners.
    state = state_dict
    perf = perf_dict


__all__ = ["Portfolio", "Trade", "EXIT_SNIPE_PRICE"]
