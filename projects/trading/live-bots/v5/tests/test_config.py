# CHANGE_SUMMARY
# 2026-06-30  kilo
#   - Smoke tests for v5.config loader and validation.
# WHY: Configuration errors are a common source of live-trading incidents.

"""Smoke tests for the v5 configuration loader."""

import os
import tempfile
from pathlib import Path

import pytest

from v5.config import DEFAULT_TIMEFRAMES, BotConfig, load_bot_config


def test_default_config_loads_in_paper_mode() -> None:
    cfg = load_bot_config(config_path="/nonexistent/env/file.env")
    assert isinstance(cfg, BotConfig)
    assert cfg.mode == "paper"
    assert cfg.initial_capital == 50000.0
    assert cfg.max_contracts == 5
    assert cfg.contract_id == "CON.F.US.YM.U26"
    assert cfg.timeframes == DEFAULT_TIMEFRAMES
    assert cfg.state_dir.is_absolute()


def test_env_file_overrides_defaults() -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
        f.write("MODE=live\n")
        f.write("MAX_CONTRACTS=3\n")
        f.write("PROJECTX_API_KEY=test-key\n")
        f.write("PROJECTX_USERNAME=test-user\n")
        f.write("PROJECTX_ACCOUNT_NAME=50KTC\n")
        path = Path(f.name)

    try:
        cfg = load_bot_config(config_path=path)
        assert cfg.mode == "live"
        assert cfg.max_contracts == 3
        assert cfg.projectx_api_key == "test-key"
        assert cfg.projectx_username == "test-user"
        assert cfg.projectx_account_name == "50KTC"
    finally:
        path.unlink()


def test_environment_variable_override() -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
        f.write("MODE=live\n")
        f.write("PROJECTX_API_KEY=file-key\n")
        f.write("PROJECTX_USERNAME=file-user\n")
        path = Path(f.name)

    old_key = os.environ.get("PROJECT_X_API_KEY")
    try:
        os.environ["PROJECT_X_API_KEY"] = "env-key"
        cfg = load_bot_config(config_path=path)
        assert cfg.projectx_api_key == "env-key"
        assert cfg.projectx_username == "file-user"
    finally:
        if old_key is None:
            os.environ.pop("PROJECT_X_API_KEY", None)
        else:
            os.environ["PROJECT_X_API_KEY"] = old_key
        path.unlink()


def test_live_mode_requires_credentials() -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
        f.write("MODE=live\n")
        path = Path(f.name)

    try:
        with pytest.raises(ValueError) as exc_info:
            load_bot_config(config_path=path)
        err = str(exc_info.value)
        assert "projectx_api_key" in err
        assert "projectx_username" in err
    finally:
        path.unlink()


def test_validation_rejects_negative_limits() -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
        f.write("DAILY_LOSS_LIMIT=-100\n")
        path = Path(f.name)

    try:
        with pytest.raises(ValueError) as exc_info:
            load_bot_config(config_path=path)
        assert "daily_loss_limit" in str(exc_info.value)
    finally:
        path.unlink()


def test_path_overrides_resolve() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "state"
        logs = Path(tmp) / "logs"
        cfg = load_bot_config(
            config_path="/nonexistent",
            state_dir=state,
            log_dir=logs,
        )
        assert cfg.state_dir == state.resolve()
        assert cfg.log_dir == logs.resolve()
