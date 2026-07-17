"""Shared type definitions for the FVG Topstep engine.

All strategy and risk modules operate on these pure-Python types so they stay
decoupled from the broker SDK.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any


class Direction(str, Enum):
    LONG = "Long"
    SHORT = "Short"

    @classmethod
    def from_sdk(cls, value: Any) -> Direction:
        if isinstance(value, cls):
            return value
        if isinstance(value, int):
            if value == 1:
                return cls.LONG
            if value == 2:
                return cls.SHORT
        if isinstance(value, str):
            s = value.strip().lower()
            if s in ("1", "long", "buy"):
                return cls.LONG
            if s in ("2", "short", "sell"):
                return cls.SHORT
        raise ValueError(f"Cannot coerce {value!r} to Direction")


class SetupType(str, Enum):
    FVG = "fvg"
    BREAKAWAY_GAP = "breakaway_gap"
    BREAKER_BLOCK = "breaker_block"
    UNICORN = "unicorn"


@dataclass(frozen=True, slots=True)
class Bar:
    """One OHLCV bar."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int | None = None


@dataclass(frozen=True, slots=True)
class FVGZone:
    """A detected Fair Value Gap or Breakaway Gap zone."""

    direction: Direction
    start: float
    end: float
    formed_at: datetime
    timeframe: str
    setup_type: SetupType
    mitigated: bool = False
    traded: bool = False

    @property
    def is_bullish(self) -> bool:
        return self.direction is Direction.LONG

    @property
    def size(self) -> float:
        return abs(self.end - self.start)


@dataclass(frozen=True, slots=True)
class SwingLeg:
    """A swing high or low used for invalidation / stop placement."""

    direction: Direction
    price: float
    timestamp: datetime


@dataclass(frozen=True, slots=True)
class Setup:
    """A complete trade setup emitted by the strategy engine."""

    symbol: str
    direction: Direction
    entry_price: float
    stop_loss: float
    take_profit: float
    context_tf: str
    pda_tf: str
    entry_tf: str
    setup_type: SetupType
    setup_id: str
    formed_at: datetime
    fvg_zone: FVGZone
    swing_leg: SwingLeg
    reason: str

    @property
    def risk_distance(self) -> float:
        return abs(self.entry_price - self.stop_loss)

    @property
    def reward_distance(self) -> float:
        return abs(self.take_profit - self.entry_price)

    @property
    def risk_reward_ratio(self) -> float:
        r = self.risk_distance
        return self.reward_distance / r if r else 0.0


@dataclass(frozen=True, slots=True)
class Signal:
    """Order-level signal ready for execution."""

    timestamp: datetime
    symbol: str
    direction: Direction
    action: str  # "enter", "cancel", "flatten"
    setup: Setup | None = None
    qty: int = 0
    reason: str = ""


@dataclass(frozen=True, slots=True)
class Fill:
    """One fill event."""

    order_id: int
    symbol: str
    direction: Direction
    qty: int
    price: float
    timestamp: datetime
    setup_id: str | None = None


@dataclass(frozen=True, slots=True)
class TradeRecord:
    """One completed round-turn."""

    setup_id: str
    symbol: str
    direction: Direction
    entry_price: float
    exit_price: float
    qty: int
    gross_pnl: float
    net_pnl: float
    entry_time: datetime
    exit_time: datetime
    exit_reason: str


@dataclass
class Position:
    """Internal position tracking."""

    symbol: str
    direction: Direction
    size: int
    avg_entry_price: float
    entry_time: datetime
    stop_loss: float
    take_profit: float
    setup_id: str | None = None


@dataclass
class BotState:
    """Persistable bot state."""

    date: date | None = None
    equity: float = 0.0
    peak_equity: float = 0.0
    daily_pnl: float = 0.0
    cumulative_pnl: float = 0.0
    mll: float = 0.0
    mll_locked: bool = False
    trading_paused: bool = False
    failed: bool = False
    failed_reason: str = ""
    day_start_equity: float | None = None
    used_fvgs: set[str] = field(default_factory=set)
    open_positions: dict[str, Position] = field(default_factory=dict)
    working_orders: dict[int, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RiskCheck:
    allow: bool
    reason: str = ""
    limit_hit: str | None = None
    max_qty: int = 0


@dataclass(frozen=True, slots=True)
class BacktestResult:
    symbol: str
    trades: list[TradeRecord]
    equity_curve: list[tuple[datetime, float]]
    initial_capital: float
    final_equity: float
    total_return_pct: float
    win_rate: float
    profit_factor: float
    sharpe_ratio: float
    max_drawdown_pct: float
    max_drawdown_dollar: float
    avg_trades_per_day: float
    avg_win: float
    avg_loss: float


__all__ = [
    "Direction",
    "SetupType",
    "Bar",
    "FVGZone",
    "SwingLeg",
    "Setup",
    "Signal",
    "Fill",
    "TradeRecord",
    "Position",
    "BotState",
    "RiskCheck",
    "BacktestResult",
]
