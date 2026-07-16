"""Typed configuration loader for the FVG Topstep bot."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BotConfig:
    """Immutable runtime configuration."""

    mode: str
    projectx_api_key: str
    projectx_username: str
    projectx_account_name: str
    symbols: list[str]
    context_timeframes: list[str]
    pda_timeframes: list[str]
    entry_timeframes: list[str]
    killzones: list[tuple[str, str]]
    initial_capital: float
    risk_per_trade: float
    daily_loss_limit: float
    daily_loss_halt: float
    daily_loss_flatten: float
    max_drawdown: float
    max_contracts: int
    min_gap_size: float
    min_gap_percent: float
    reward_ratio: float
    max_open_setups_per_symbol: int
    backtest_years: int
    state_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent / "state")
    log_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent / "state" / "logs")
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


def _parse_timeframes(value: str) -> list[str]:
    return [tf.strip().lower() for tf in value.split(",") if tf.strip()]


def _parse_symbols(value: str) -> list[str]:
    return [s.strip().upper() for s in value.split(",") if s.strip()]


def _parse_killzones(value: str) -> list[tuple[str, str]]:
    zones = []
    for part in value.split(","):
        part = part.strip()
        if "-" in part:
            start, end = part.split("-", 1)
            zones.append((start.strip(), end.strip()))
    return zones


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)


def load_config(config_path: Path | str | None = None) -> BotConfig:
    """Load configuration from env file and environment variables."""
    if config_path is None:
        config_path = Path(__file__).resolve().parent.parent / "config" / "config.env"

    config: dict[str, Any] = {}

    if Path(config_path).exists():
        with open(config_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                config[key.strip().lower()] = value.strip().strip('"').strip("'")

    env_map = {
        "PROJECTX_API_KEY": "projectx_api_key",
        "PROJECT_X_API_KEY": "projectx_api_key",
        "PROJECTX_USERNAME": "projectx_username",
        "PROJECT_X_USERNAME": "projectx_username",
        "PROJECTX_ACCOUNT_NAME": "projectx_account_name",
        "PROJECT_X_ACCOUNT_NAME": "projectx_account_name",
    }
    for env_name, cfg_key in env_map.items():
        env_value = os.environ.get(env_name)
        if env_value:
            config[cfg_key] = env_value

    def get(key: str, default: Any) -> Any:
        val = config.get(key.lower(), default)
        if isinstance(default, bool) and isinstance(val, str):
            return _coerce_bool(val)
        if isinstance(default, (int, float)) and isinstance(val, str):
            try:
                return type(default)(val)
            except ValueError:
                raise ValueError(f"config key {key} expects a number, got {val!r}")
        return val if val is not None else default

    cfg = BotConfig(
        mode=str(get("MODE", "paper")).lower(),
        projectx_api_key=str(get("PROJECTX_API_KEY", "")),
        projectx_username=str(get("PROJECTX_USERNAME", "")),
        projectx_account_name=str(get("PROJECTX_ACCOUNT_NAME", "")),
        symbols=_parse_symbols(str(get("SYMBOLS", "YM"))),
        context_timeframes=_parse_timeframes(str(get("CONTEXT_TIMEFRAMES", "4h,1h"))),
        pda_timeframes=_parse_timeframes(str(get("PDA_TIMEFRAMES", "1h"))),
        entry_timeframes=_parse_timeframes(str(get("ENTRY_TIMEFRAMES", "15m,5m,1m"))),
        killzones=_parse_killzones(str(get("KILLZONES", "01:00-05:00,07:00-11:00,12:30-15:00"))),
        initial_capital=float(get("INITIAL_CAPITAL", 50000.0)),
        risk_per_trade=float(get("RISK_PER_TRADE", 250.0)),
        daily_loss_limit=float(get("DAILY_LOSS_LIMIT", 1000.0)),
        daily_loss_halt=float(get("DAILY_LOSS_HALT", 700.0)),
        daily_loss_flatten=float(get("DAILY_LOSS_FLATTEN", 900.0)),
        max_drawdown=float(get("MAX_DRAWDOWN", 2000.0)),
        max_contracts=int(get("MAX_CONTRACTS", 5)),
        min_gap_size=float(get("MIN_GAP_SIZE", 0.0)),
        min_gap_percent=float(get("MIN_GAP_PERCENT", 0.0)),
        reward_ratio=float(get("REWARD_RATIO", 2.0)),
        max_open_setups_per_symbol=int(get("MAX_OPEN_SETUPS_PER_SYMBOL", 2)),
        backtest_years=int(get("BACKTEST_YEARS", 5)),
        raw=config,
    )

    _validate(cfg)
    return cfg


def _validate(cfg: BotConfig) -> None:
    errors: list[str] = []
    if cfg.mode not in ("paper", "live"):
        errors.append(f"mode must be paper or live, got {cfg.mode}")
    if not cfg.projectx_api_key and cfg.mode == "live":
        errors.append("projectx_api_key required in live mode")
    if cfg.initial_capital <= 0:
        errors.append("initial_capital must be positive")
    if cfg.risk_per_trade <= 0:
        errors.append("risk_per_trade must be positive")
    if cfg.daily_loss_limit <= 0:
        errors.append("daily_loss_limit must be positive")
    if cfg.max_drawdown <= 0:
        errors.append("max_drawdown must be positive")
    if cfg.max_contracts < 1:
        errors.append("max_contracts must be at least 1")
    if cfg.reward_ratio <= 0:
        errors.append("reward_ratio must be positive")
    if errors:
        raise ValueError("BotConfig validation failed: " + "; ".join(errors))


__all__ = ["BotConfig", "load_config"]
