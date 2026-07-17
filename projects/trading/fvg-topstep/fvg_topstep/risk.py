# CHANGE_SUMMARY
# 2026-07-16  subagent
#   - Created risk.py with RiskGuard: trade eligibility, entry checks, sizing,
#     peak-equity tracking, daily loss halt/flatten, and max contracts.
# WHY: Strategy requires fixed-dollar risk and hard daily/drawdown limits.

"""Risk management guards for the FVG Topstep engine."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from fvg_topstep.types import BotState, Direction, Position, RiskCheck, Setup, Signal

logger = logging.getLogger(__name__)


@dataclass
class AccountSnapshot:
    """Read-only view of account state for risk decisions."""

    equity: float = 0.0
    buying_power: float = 0.0
    cash: float = 0.0
    margin_per_contract: float = 0.0


class RiskGuard:
    """Enforce sizing, daily loss limits, drawdown, and concentration rules."""

    def __init__(
        self,
        risk_per_trade: float,
        daily_loss_limit: float,
        daily_loss_halt: float,
        daily_loss_flatten: float,
        max_drawdown: float,
        max_contracts: int,
        reward_ratio: float,
        max_open_setups_per_symbol: int = 2,
    ) -> None:
        self.risk_per_trade = risk_per_trade
        self.daily_loss_limit = daily_loss_limit
        self.daily_loss_halt = daily_loss_halt
        self.daily_loss_flatten = daily_loss_flatten
        self.max_drawdown = max_drawdown
        self.max_contracts = max_contracts
        self.reward_ratio = reward_ratio
        self.max_open_setups_per_symbol = max_open_setups_per_symbol

    def can_trade(
        self,
        account_snapshot: AccountSnapshot,
        state: BotState,
    ) -> RiskCheck:
        """Return whether the bot is allowed to take new trades right now."""
        if state.trading_paused:
            return RiskCheck(
                allow=False,
                reason="Trading paused by prior halt/flatten logic",
                limit_hit="trading_paused",
            )
        if state.failed:
            return RiskCheck(
                allow=False,
                reason=f"Account failed: {state.failed_reason}",
                limit_hit="failed",
            )

        drawdown = state.peak_equity - account_snapshot.equity
        if drawdown >= self.max_drawdown:
            return RiskCheck(
                allow=False,
                reason=f"Max drawdown hit: ${drawdown:,.2f}",
                limit_hit="max_drawdown",
            )

        if state.daily_pnl <= -self.daily_loss_halt:
            return RiskCheck(
                allow=False,
                reason=f"Daily loss halt: ${state.daily_pnl:,.2f}",
                limit_hit="daily_loss_halt",
            )

        return RiskCheck(allow=True, reason="OK")

    def check_entry(
        self,
        setup: Setup,
        state: BotState,
        account: AccountSnapshot,
        open_positions: dict[str, Position],
        working_orders: dict[int, dict[str, Any]],
    ) -> RiskCheck:
        """Evaluate a proposed setup and return a ``RiskCheck``."""
        base = self.can_trade(account, state)
        if not base.allow:
            return base

        if setup.fvg_zone.mitigated or setup.fvg_zone.traded:
            return RiskCheck(
                allow=False,
                reason="FVG zone already mitigated or traded",
                limit_hit="used_fvg",
            )

        symbol = setup.symbol.upper()
        symbol_positions = [
            p for p in open_positions.values() if p.symbol.upper() == symbol
        ]
        symbol_orders = [
            o
            for o in working_orders.values()
            if str(o.get("symbol", "")).upper() == symbol
        ]
        open_count = len(symbol_positions) + len(symbol_orders)
        if open_count >= self.max_open_setups_per_symbol:
            return RiskCheck(
                allow=False,
                reason="Max open setups for symbol reached",
                limit_hit="max_symbol_setups",
            )

        total_contracts = sum(abs(p.size) for p in open_positions.values())
        if total_contracts + setup.qty > self.max_contracts:
            remaining = max(0, self.max_contracts - total_contracts)
            return RiskCheck(
                allow=False,
                reason="Max total contracts would be exceeded",
                limit_hit="max_contracts",
                max_qty=remaining,
            )

        if setup.risk_reward_ratio < self.reward_ratio - 1e-9:
            return RiskCheck(
                allow=False,
                reason=(
                    f"RR {setup.risk_reward_ratio:.2f} below required "
                    f"{self.reward_ratio:.2f}"
                ),
                limit_hit="min_rr",
            )

        qty = self.compute_qty(
            self.risk_per_trade,
            setup.risk_distance,
            account.margin_per_contract,
            account.margin_per_contract or 1.0,
        )
        if qty <= 0:
            return RiskCheck(
                allow=False,
                reason="Computed quantity is zero (stop too wide)",
                limit_hit="zero_qty",
            )

        max_qty = min(qty, self.max_contracts - total_contracts)
        return RiskCheck(allow=True, reason="Entry allowed", max_qty=max_qty)

    def compute_qty(
        self,
        risk_per_trade: float,
        stop_distance: float,
        tick_value: float,
        tick_size: float,
    ) -> int:
        """Compute integer contract quantity for a fixed-dollar risk.

        ``tick_value`` is the dollar value of one tick, ``tick_size`` is the
        price increment of one tick.  If either is non-positive, fall back to
        treating ``stop_distance`` as the dollar risk directly (useful for
        unitized instruments).
        """
        if stop_distance <= 0 or risk_per_trade <= 0:
            return 0
        if tick_value > 0 and tick_size > 0:
            ticks_at_risk = stop_distance / tick_size
            dollar_risk_per_contract = ticks_at_risk * tick_value
            qty = int(risk_per_trade / dollar_risk_per_contract)
        else:
            qty = int(risk_per_trade / stop_distance)
        return max(0, qty)

    def update_peak_equity(self, state: BotState, equity: float) -> BotState:
        """Update peak equity and daily PnL; trigger halt/flatten if needed."""
        state.equity = equity
        if equity > state.peak_equity:
            state.peak_equity = equity

        today = datetime.now().date()
        if state.date != today:
            state.date = today
            state.day_start_equity = equity
            state.daily_pnl = 0.0

        if state.day_start_equity is not None:
            state.daily_pnl = equity - state.day_start_equity

        if state.daily_pnl <= -self.daily_loss_flatten:
            self._flatten(state, f"daily loss flatten: ${state.daily_pnl:,.2f}")
        elif state.daily_pnl <= -self.daily_loss_halt:
            self._halt(state, f"daily loss halt: ${state.daily_pnl:,.2f}")

        return state

    def _halt(self, state: BotState, reason: str) -> None:
        state.trading_paused = True
        logger.warning("Risk halt: %s", reason)

    def _flatten(self, state: BotState, reason: str) -> None:
        state.trading_paused = True
        logger.warning("Risk flatten: %s", reason)

    def daily_loss_exceeded(self, state: BotState) -> bool:
        """Return True if the hard daily loss limit has been breached."""
        return state.daily_pnl <= -self.daily_loss_limit

    def flatten_signals(
        self,
        open_positions: dict[str, Position],
        timestamp: datetime,
        reason: str = "risk flatten",
    ) -> list[Signal]:
        """Build flatten signals for every open position."""
        signals: list[Signal] = []
        for pos in open_positions.values():
            signals.append(
                Signal(
                    timestamp=timestamp,
                    symbol=pos.symbol,
                    direction=Direction.LONG if pos.size < 0 else Direction.SHORT,
                    action="flatten",
                    qty=abs(pos.size),
                    reason=reason,
                )
            )
        return signals


__all__ = ["AccountSnapshot", "RiskGuard"]
