# CHANGE_SUMMARY
# 2026-06-30  kilo
#   - Created v5 shared interface types for the YM ORB live trading bot.
#   - Added dataclasses/NamedTuples for AccountSnapshot, PositionSnapshot,
#     WorkingOrder, Signal, RiskCheck, StoredState, Bar, Quote, TFState, etc.
#   - Included SDK field mapping comments based on project_x_py source inspection.
# WHY: The v5 refactor needs small, typed, single-responsibility modules. Central
#      interfaces prevent ad-hoc dicts and make the SDK adapter layer explicit.

"""
Shared type definitions for the YM ORB v5 live trading bot.

These dataclasses and enums are intentionally pure-Python: they do NOT import
``project_x_py`` so that the math/strategy modules can remain decoupled from
the broker SDK.  SDK-facing adapters (market_feed.py, order_executor.py) are
responsible for converting ``project_x_py`` responses into these internal types.

SDK field mapping (project_x_py -> internal):

Account (project_x_py.models.Account)
  id               -> account_id
  name             -> name
  balance          -> balance
  canTrade         -> can_trade
  isVisible        -> is_visible
  simulated        -> simulated

Position (project_x_py.models.Position)
  id               -> position_id
  accountId        -> account_id
  contractId       -> contract_id
  creationTimestamp-> creation_timestamp
  type             -> direction (1=LONG, 2=SHORT)
  size             -> size
  averagePrice     -> average_price
  contractDisplayName -> contract_display_name

Order (project_x_py.models.Order)
  id               -> order_id
  accountId        -> account_id
  contractId       -> contract_id
  creationTimestamp-> creation_timestamp
  updateTimestamp  -> update_timestamp
  status           -> status (OrderStatus)
  type             -> order_type (OrderType)
  side             -> side (OrderSide: 0=buy, 1=sell)
  size             -> size
  fillVolume       -> fill_volume
  limitPrice       -> limit_price
  stopPrice        -> stop_price
  filledPrice      -> filled_price
  customTag        -> custom_tag
  symbolId         -> symbol_id

Quote WebSocket payloads are normalised from either snake_case or camelCase:
  contract_id / contractId -> contract_id
  lastPrice / last / data['last'] -> last_price
  bid / data['bid'] -> bid
  ask / data['ask'] -> ask
  volume / data['volume'] -> volume
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, NamedTuple


# ---------------------------------------------------------------------------
# Time / market data primitives
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Bar:
    """A single OHLCV bar, timezone-agnostic (caller supplies ET context)."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int | None = None
    time_min: int | None = None  # hour * 60 + minute, used by the ORB engine
    date: date | None = None


@dataclass(frozen=True, slots=True)
class Quote:
    """Normalised tick/quote snapshot.

    ``raw`` preserves the original SDK payload for forensics without polluting
    the typed interface.
    """

    contract_id: str
    last_price: float | None = None
    bid: float | None = None
    ask: float | None = None
    volume: int | None = None
    timestamp: datetime | None = None
    raw: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Trading enums with SDK mappings
# ---------------------------------------------------------------------------


class Direction(str, Enum):
    """Strategy direction, kept as strings to match the v4 engine."""

    LONG = "Long"
    SHORT = "Short"


class OrderSide(str, Enum):
    """Internal order side with ProjectX integer mapping."""

    BUY = "buy"
    SELL = "sell"

    @property
    def sdk_value(self) -> int:
        return 0 if self is OrderSide.BUY else 1

    @classmethod
    def from_sdk(cls, side: int) -> OrderSide:
        return cls.BUY if side == 0 else cls.SELL


class OrderStatus(Enum):
    """ProjectX order status codes.

    SDK mapping:
        0 = NONE, 1 = OPEN, 2 = FILLED, 3 = CANCELLED,
        4 = EXPIRED, 5 = REJECTED, 6 = PENDING.
    """

    NONE = 0
    OPEN = 1
    FILLED = 2
    CANCELLED = 3
    EXPIRED = 4
    REJECTED = 5
    PENDING = 6

    @property
    def is_terminal(self) -> bool:
        return self in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.EXPIRED, OrderStatus.REJECTED)

    @property
    def is_working(self) -> bool:
        return self in (OrderStatus.OPEN, OrderStatus.PENDING)


class OrderType(Enum):
    """ProjectX order type codes.

    SDK mapping:
        0 = UNKNOWN, 1 = LIMIT, 2 = MARKET, 3 = STOP_LIMIT, 4 = STOP,
        5 = TRAILING_STOP, 6 = JOIN_BID, 7 = JOIN_ASK.
    """

    UNKNOWN = 0
    LIMIT = 1
    MARKET = 2
    STOP_LIMIT = 3
    STOP = 4
    TRAILING_STOP = 5
    JOIN_BID = 6
    JOIN_ASK = 7


class SignalAction(str, Enum):
    """Actions the strategy engine can emit."""

    ENTER = "enter"
    EXIT = "exit"
    TRAIL_STOP = "trail_stop"
    CANCEL = "cancel"
    REARM = "rearm"


# ---------------------------------------------------------------------------
# Broker-state snapshots
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """Normalised broker account state.

    Built from ``project_x_py.models.Account`` by the SDK adapter layer.
    """

    account_id: int
    name: str
    balance: float
    can_trade: bool
    is_visible: bool
    simulated: bool
    raw: dict[str, Any] | None = None

    @classmethod
    def from_sdk(cls, account: Any) -> AccountSnapshot:
        """Best-effort conversion from an SDK ``Account`` dataclass or dict."""
        data = account if isinstance(account, dict) else vars(account)
        return cls(
            account_id=int(data.get("id", 0)),
            name=str(data.get("name", "")),
            balance=float(data.get("balance", 0.0)),
            can_trade=bool(data.get("canTrade", True)),
            is_visible=bool(data.get("isVisible", True)),
            simulated=bool(data.get("simulated", False)),
            raw=dict(data),
        )


@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    """Normalised open position.

    Built from ``project_x_py.models.Position`` by the SDK adapter layer.
    ``direction`` is derived from ``type`` (1=LONG, 2=SHORT).
    """

    position_id: int
    account_id: int
    contract_id: str
    creation_timestamp: str
    direction: Direction
    size: int
    average_price: float
    contract_display_name: str | None = None
    raw: dict[str, Any] | None = None

    @property
    def signed_size(self) -> int:
        return -self.size if self.direction is Direction.SHORT else self.size

    @classmethod
    def from_sdk(cls, position: Any) -> PositionSnapshot:
        data = position if isinstance(position, dict) else vars(position)
        px_type = data.get("type", 0)
        direction = Direction.LONG if px_type == 1 else Direction.SHORT if px_type == 2 else Direction.LONG
        return cls(
            position_id=int(data.get("id", 0)),
            account_id=int(data.get("accountId", 0)),
            contract_id=str(data.get("contractId", "")),
            creation_timestamp=str(data.get("creationTimestamp", "")),
            direction=direction,
            size=int(data.get("size", 0)),
            average_price=float(data.get("averagePrice", 0.0)),
            contract_display_name=data.get("contractDisplayName"),
            raw=dict(data),
        )


@dataclass(frozen=True, slots=True)
class WorkingOrder:
    """Normalised working order.

    Built from ``project_x_py.models.Order`` by the SDK adapter layer.  The
    optional ``tf_name`` field is used internally to attribute entry-stop fills
    back to the originating timeframe engine.
    """

    order_id: int
    account_id: int
    contract_id: str
    creation_timestamp: str
    status: OrderStatus
    order_type: OrderType
    side: OrderSide
    size: int
    update_timestamp: str | None = None
    fill_volume: int | None = None
    limit_price: float | None = None
    stop_price: float | None = None
    filled_price: float | None = None
    custom_tag: str | None = None
    symbol_id: str | None = None
    tf_name: str | None = None
    raw: dict[str, Any] | None = None

    @property
    def remaining_size(self) -> int:
        if self.fill_volume is None:
            return self.size
        return self.size - self.fill_volume

    @property
    def is_buy(self) -> bool:
        return self.side is OrderSide.BUY

    @classmethod
    def from_sdk(cls, order: Any, tf_name: str | None = None) -> WorkingOrder:
        data = order if isinstance(order, dict) else vars(order)
        return cls(
            order_id=int(data.get("id", 0)),
            account_id=int(data.get("accountId", 0)),
            contract_id=str(data.get("contractId", "")),
            creation_timestamp=str(data.get("creationTimestamp", "")),
            update_timestamp=data.get("updateTimestamp"),
            status=OrderStatus(int(data.get("status", 0))),
            order_type=OrderType(int(data.get("type", 0))),
            side=OrderSide.from_sdk(int(data.get("side", 0))),
            size=int(data.get("size", 0)),
            fill_volume=data.get("fillVolume"),
            limit_price=data.get("limitPrice"),
            stop_price=data.get("stopPrice"),
            filled_price=data.get("filledPrice"),
            custom_tag=data.get("customTag"),
            symbol_id=data.get("symbolId"),
            tf_name=tf_name,
            raw=dict(data),
        )


# ---------------------------------------------------------------------------
# Strategy / risk interfaces
# ---------------------------------------------------------------------------


class TimeFrameParam(NamedTuple):
    """ORB parameters for a single timeframe (matches v4 ``TIMEFRAMES`` values)."""

    or_min: int       # minute-of-day at which the opening range ends
    trig: float       # trailing-trigger threshold in multiples of sl_dist
    sint: float       # trailing step interval in multiples of sl_dist
    lock: float       # fraction of each step to lock in


@dataclass(frozen=True, slots=True)
class Signal:
    """An immutable instruction emitted by the strategy engine.

    The orchestrator/risk-guard decides whether and how to execute it.
    """

    timestamp: datetime
    tf_name: str
    action: SignalAction
    direction: Direction
    price: float
    qty: int
    reason: str


@dataclass(frozen=True, slots=True)
class RiskCheck:
    """Result of a risk-guard pre-flight check."""

    allow: bool
    reason: str = ""
    limit_hit: str | None = None
    max_qty: int = 0


# ---------------------------------------------------------------------------
# Per-TF and persisted state
# ---------------------------------------------------------------------------


@dataclass
class TFPosition:
    """Internal representation of a single timeframe's open position."""

    direction: Direction
    entry_price: float
    entry_time: datetime | None
    qty: int
    virtual_sl: float
    max_r: float = 0.0


@dataclass(frozen=True, slots=True)
class TradeRecord:
    """One closed round-turn for a timeframe."""

    tf: str
    direction: Direction
    entry_price: float
    exit_price: float
    qty: int
    gross: float
    net: float
    exit_reason: str
    entry_time: str
    exit_time: str
    duration_mins: int


@dataclass
class TFState:
    """Siloed state for a single timeframe engine."""

    tf_name: str
    or_high: float | None = None
    or_low: float | None = None
    buy_trigger: float | None = None
    sell_trigger: float | None = None
    entries_taken: int = 0
    pending_buy_id: int | None = None
    pending_sell_id: int | None = None
    daily_pnl: float = 0.0
    cumulative_pnl: float = 0.0
    position: TFPosition | None = None
    trade_history: list[TradeRecord] = field(default_factory=list)


@dataclass
class StoredState:
    """Full persistable bot state.

    This is what ``state_store.py`` writes atomically on every cycle.  It is
    deliberately a plain dataclass so it can be serialised to JSON and restored
    without importing broker SDK types.
    """

    date: date | None
    equity: float
    peak_equity: float
    daily_pnl: float = 0.0
    cumulative_pnl: float = 0.0
    mll: float = 0.0
    mll_locked: bool = False
    trading_paused: bool = False
    failed: bool = False
    failed_reason: str = ""
    last_bar_ts: datetime | None = None
    current_price: float | None = None
    stop_order_id: int | None = None
    stop_direction: Direction | None = None
    current_sl_price: float | None = None
    tf_states: dict[str, TFState] = field(default_factory=dict)
    pending_orders: list[WorkingOrder] = field(default_factory=list)
    open_positions: list[PositionSnapshot] = field(default_factory=list)
    order_to_tf: dict[int, str] = field(default_factory=dict)


__all__ = [
    "Bar",
    "Quote",
    "Direction",
    "OrderSide",
    "OrderStatus",
    "OrderType",
    "SignalAction",
    "AccountSnapshot",
    "PositionSnapshot",
    "WorkingOrder",
    "Signal",
    "RiskCheck",
    "TimeFrameParam",
    "TFPosition",
    "TradeRecord",
    "TFState",
    "StoredState",
]
