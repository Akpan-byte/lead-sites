# CHANGE_SUMMARY
# 2026-06-30  kilo
#   - Created v5 typed configuration loader (`BotConfig`) and defaults.
#   - Added `load_bot_config()` that reads env files and validates parameters.
#   - Added backward-compatible `BASELINE_INDEX`, `TICK_VALUE`, and `TIMEFRAMES`
#     aliases so the existing `strategy_engine.py` and test suite keep working.
# WHY: v4 loaded config through an untyped dict. Strongly-typed config catches
#      misconfiguration early and keeps the rest of v5 focused on trading logic.

"""
Typed configuration for the YM ORB v5 live trading bot.

``BotConfig`` is an immutable dataclass that captures every runtime parameter
needed by the strategy, risk guard, and order executor.  ``load_bot_config()``
merges defaults, an optional ``config.env`` file, and environment variables,
then validates the result.

This module does NOT import ``project_x_py``; it is pure configuration.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from v5.types import TimeFrameParam


DEFAULT_BASELINE_INDEX = 29174.0
DEFAULT_TICK_VALUE = 5.0
DEFAULT_SYMBOL = "YM"
DEFAULT_CONTRACT_ID = "CON.F.US.YM.U26"

# Matches the v4 TIMEFRAMES table exactly.  Values are preserved so the v5
# strategy engine can produce bit-identical signals during parity testing.
DEFAULT_TIMEFRAMES: dict[str, TimeFrameParam] = {
    "1m": TimeFrameParam(or_min=571, trig=0.05, sint=1.50, lock=0.90),
    "3m": TimeFrameParam(or_min=573, trig=0.05, sint=0.50, lock=0.50),
    "5m": TimeFrameParam(or_min=575, trig=0.05, sint=2.00, lock=0.90),
    "15m": TimeFrameParam(or_min=585, trig=0.05, sint=1.79, lock=0.90),
    "30m": TimeFrameParam(or_min=600, trig=0.25, sint=0.50, lock=0.50),
    "60m": TimeFrameParam(or_min=630, trig=0.05, sint=1.79, lock=0.75),
}

# Backward-compatible dict-of-dicts form used by strategy_engine.py and the
# existing v5 test suite.  Values are identical to DEFAULT_TIMEFRAMES above.
TIMEFRAMES: dict[str, dict[str, float]] = {
    "1m": {"or_min": 571, "trig": 0.05, "sint": 1.50, "lock": 0.90},
    "3m": {"or_min": 573, "trig": 0.05, "sint": 0.50, "lock": 0.50},
    "5m": {"or_min": 575, "trig": 0.05, "sint": 2.00, "lock": 0.90},
    "15m": {"or_min": 585, "trig": 0.05, "sint": 1.79, "lock": 0.90},
    "30m": {"or_min": 600, "trig": 0.25, "sint": 0.50, "lock": 0.50},
    "60m": {"or_min": 630, "trig": 0.05, "sint": 1.79, "lock": 0.75},
}

# Convenience aliases used by the existing v5 strategy engine and tests.
BASELINE_INDEX = DEFAULT_BASELINE_INDEX
TICK_VALUE = DEFAULT_TICK_VALUE
SYMBOL = DEFAULT_SYMBOL
CONTRACT_ID = DEFAULT_CONTRACT_ID

DEFAULT_CONFIG: dict[str, Any] = {
    "mode": "paper",
    "initial_capital": 50000.0,
    "risk_per_trade": 166.67,
    "max_drawdown": 2000.0,
    "daily_loss_limit": 900.0,
    "buffer_pts": 20.0,
    "sl_pts": 20.0,
    "baseline_index": DEFAULT_BASELINE_INDEX,
    "max_entries": 4,
    "max_contracts": 5,
    "symbol": DEFAULT_SYMBOL,
    "contract_id": DEFAULT_CONTRACT_ID,
    "tick_value": DEFAULT_TICK_VALUE,
    "projectx_api_key": "",
    "projectx_username": "",
    "projectx_account_name": None,
}


@dataclass(frozen=True)
class BotConfig:
    """Immutable runtime configuration for the bot.

    All monetary values are in USD and all point distances are in YM points.
    Paths are resolved to absolute values during loading.
    """

    mode: str
    initial_capital: float
    risk_per_trade: float
    max_drawdown: float
    daily_loss_limit: float
    buffer_pts: float
    sl_pts: float
    baseline_index: float
    max_entries: int
    max_contracts: int
    symbol: str
    contract_id: str
    tick_value: float
    projectx_api_key: str
    projectx_username: str
    projectx_account_name: str | None = None
    state_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parent / "state")
    log_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parent / "state" / "logs")
    timeframes: dict[str, TimeFrameParam] = field(default_factory=lambda: dict(DEFAULT_TIMEFRAMES))
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)


def _parse_env_line(line: str) -> tuple[str, str] | None:
    """Parse one ``KEY=value`` line from a config.env file."""
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    key, value = line.split("=", 1)
    key = key.strip().lower()
    value = value.strip().strip('"').strip("'")
    return key, value


def _apply_env_overrides(config: dict[str, Any]) -> dict[str, Any]:
    """Allow explicit environment variables to override file defaults."""
    env_map = {
        "PROJECT_X_API_KEY": "projectx_api_key",
        "PROJECTX_API_KEY": "projectx_api_key",
        "PROJECT_X_USERNAME": "projectx_username",
        "PROJECTX_USERNAME": "projectx_username",
        "PROJECT_X_ACCOUNT_NAME": "projectx_account_name",
        "PROJECTX_ACCOUNT_NAME": "projectx_account_name",
    }
    for env_name, cfg_key in env_map.items():
        env_value = os.environ.get(env_name)
        if env_value:
            config[cfg_key] = env_value
    return config


def _cast_value(key: str, value: Any, defaults: dict[str, Any]) -> Any:
    """Cast a config value to the same type as the default, where possible."""
    if value is None:
        return defaults.get(key)

    default = defaults.get(key)
    if isinstance(default, bool) and not isinstance(value, bool):
        return _coerce_bool(value)
    if isinstance(default, (int, float)) and isinstance(value, str):
        try:
            if isinstance(default, int):
                return int(value)
            return float(value)
        except ValueError as exc:
            raise ValueError(f"config key '{key}' expects a number, got '{value}'") from exc
    if isinstance(default, dict) and isinstance(value, str):
        # Allow JSON-like dict strings if needed later; for now pass through.
        return value
    return value


def load_bot_config(
    config_path: Path | str | None = None,
    *,
    state_dir: Path | str | None = None,
    log_dir: Path | str | None = None,
) -> BotConfig:
    """Load and validate bot configuration.

    The loader merges, in order of lowest to highest precedence:
      1. ``DEFAULT_CONFIG``
      2. ``config/config.env`` next to this package (if ``config_path`` is omitted)
      3. ``config_path`` if explicitly provided
      4. Environment variables ``PROJECT_X_API_KEY`` / ``PROJECTX_*``

    Args:
        config_path: Optional explicit path to a ``config.env`` file.
        state_dir: Optional override for the state directory.
        log_dir: Optional override for the log directory.

    Returns:
        A validated ``BotConfig`` instance.

    Raises:
        ValueError: If validation fails (missing credentials in live mode,
            negative limits, unknown mode, etc.).
    """
    config = dict(DEFAULT_CONFIG)

    # Default env file lives at live-bots/config/config.env
    if config_path is None:
        default_env = Path(__file__).resolve().parent.parent / "config" / "config.env"
        if default_env.exists():
            config_path = default_env

    if config_path is not None:
        path = Path(config_path)
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    parsed = _parse_env_line(line)
                    if parsed:
                        config[parsed[0]] = parsed[1]

    config = _apply_env_overrides(config)

    # Cast everything to the type expected by DEFAULT_CONFIG
    typed: dict[str, Any] = {}
    for key, value in config.items():
        typed[key] = _cast_value(key, value, DEFAULT_CONFIG)

    # Resolve paths
    base_state = Path(state_dir) if state_dir else Path(__file__).resolve().parent / "state"
    base_log = Path(log_dir) if log_dir else base_state / "logs"
    base_state = base_state.resolve()
    base_log = base_log.resolve()

    bot_config = BotConfig(
        mode=str(typed["mode"]).lower(),
        initial_capital=float(typed["initial_capital"]),
        risk_per_trade=float(typed["risk_per_trade"]),
        max_drawdown=float(typed["max_drawdown"]),
        daily_loss_limit=float(typed["daily_loss_limit"]),
        buffer_pts=float(typed["buffer_pts"]),
        sl_pts=float(typed["sl_pts"]),
        baseline_index=float(typed["baseline_index"]),
        max_entries=int(typed["max_entries"]),
        max_contracts=int(typed["max_contracts"]),
        symbol=str(typed["symbol"]),
        contract_id=str(typed["contract_id"]),
        tick_value=float(typed["tick_value"]),
        projectx_api_key=str(typed.get("projectx_api_key", "")),
        projectx_username=str(typed.get("projectx_username", "")),
        projectx_account_name=typed.get("projectx_account_name"),
        state_dir=base_state,
        log_dir=base_log,
        timeframes=dict(DEFAULT_TIMEFRAMES),
        raw=typed,
    )

    _validate(bot_config)
    return bot_config


def _validate(cfg: BotConfig) -> None:
    """Validate a ``BotConfig``."""
    errors: list[str] = []

    if cfg.mode not in ("paper", "live"):
        errors.append(f"mode must be 'paper' or 'live', got '{cfg.mode}'")

    if cfg.initial_capital <= 0:
        errors.append("initial_capital must be positive")
    if cfg.risk_per_trade <= 0:
        errors.append("risk_per_trade must be positive")
    if cfg.max_drawdown <= 0:
        errors.append("max_drawdown must be positive")
    if cfg.daily_loss_limit <= 0:
        errors.append("daily_loss_limit must be positive")
    if cfg.buffer_pts < 0:
        errors.append("buffer_pts must be non-negative")
    if cfg.sl_pts <= 0:
        errors.append("sl_pts must be positive")
    if cfg.baseline_index <= 0:
        errors.append("baseline_index must be positive")
    if cfg.max_entries < 1:
        errors.append("max_entries must be at least 1")
    if cfg.max_contracts < 1:
        errors.append("max_contracts must be at least 1")
    if not cfg.symbol:
        errors.append("symbol must not be empty")
    if not cfg.contract_id:
        errors.append("contract_id must not be empty")

    if cfg.mode == "live":
        if not cfg.projectx_api_key:
            errors.append("projectx_api_key is required in live mode")
        if not cfg.projectx_username:
            errors.append("projectx_username is required in live mode")

    if errors:
        raise ValueError("BotConfig validation failed: " + "; ".join(errors))


__all__ = [
    "BotConfig",
    "BASELINE_INDEX",
    "CONTRACT_ID",
    "DEFAULT_BASELINE_INDEX",
    "DEFAULT_CONFIG",
    "DEFAULT_CONTRACT_ID",
    "DEFAULT_SYMBOL",
    "DEFAULT_TICK_VALUE",
    "DEFAULT_TIMEFRAMES",
    "SYMBOL",
    "TICK_VALUE",
    "TIMEFRAMES",
    "load_bot_config",
]
