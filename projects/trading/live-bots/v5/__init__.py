# CHANGE_SUMMARY
# 2026-06-30  kilo
#   - Created v5 package __init__.py with public exports and version marker.
# WHY: A clean public API lets callers import from ``v5`` without knowing the
#      internal module layout.

"""YM Opening Range Breakout live trading bot — v5 modular rewrite."""

from v5.config import (
    BotConfig,
    DEFAULT_BASELINE_INDEX,
    DEFAULT_CONFIG,
    DEFAULT_CONTRACT_ID,
    DEFAULT_SYMBOL,
    DEFAULT_TICK_VALUE,
    DEFAULT_TIMEFRAMES,
    load_bot_config,
)
from v5.types import (
    AccountSnapshot,
    Bar,
    Direction,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSnapshot,
    Quote,
    RiskCheck,
    Signal,
    SignalAction,
    StoredState,
    TFPosition,
    TFState,
    TimeFrameParam,
    TradeRecord,
    WorkingOrder,
)

__version__ = "5.0.0"

__all__ = [
    # Package
    "__version__",
    # Config
    "BotConfig",
    "DEFAULT_BASELINE_INDEX",
    "DEFAULT_CONFIG",
    "DEFAULT_CONTRACT_ID",
    "DEFAULT_SYMBOL",
    "DEFAULT_TICK_VALUE",
    "DEFAULT_TIMEFRAMES",
    "load_bot_config",
    # Types
    "AccountSnapshot",
    "Bar",
    "Direction",
    "OrderSide",
    "OrderStatus",
    "OrderType",
    "PositionSnapshot",
    "Quote",
    "RiskCheck",
    "Signal",
    "SignalAction",
    "StoredState",
    "TFPosition",
    "TFState",
    "TimeFrameParam",
    "TradeRecord",
    "WorkingOrder",
]
