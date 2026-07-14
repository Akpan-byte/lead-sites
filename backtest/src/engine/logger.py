# CHANGE_SUMMARY
# 2026-07-02  kilo
#   - Created engine/logger.py for CSV trade logging, atomic state/perf JSON, and rotating logs.
# WHY: Every runner/stack needs durable, crash-safe logging with the file layout from SPEC.md.

"""Logging utilities: CSV trades, atomic JSON state/perf, and rotating text logs."""

from __future__ import annotations

import csv
import json
import logging
import logging.handlers
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

import engine

TRADE_FIELDS = [
    "timestamp",
    "strategy",
    "condition_id",
    "direction",
    "entry_price",
    "shares",
    "fee_shares",
    "entry_notional",
    "entry_fee",
    "exit_price",
    "exit_notional",
    "exit_fee",
    "pnl",
    "exit_reason",
]


def _project_root() -> Path:
    env_root = os.environ.get("PAPER_TRADING_ROOT")
    if env_root:
        return Path(env_root).resolve()
    return Path(engine.__file__).resolve().parent.parent


class TradeLogger:
    """
    One logger per strategy/stack.

    Writes:
      trades/<name>_trades.csv
      state/<name>_state.json
      perf/<name>_perf.json
      logs/<name>.log
    """

    def __init__(self, name: str, project_root: Path | None = None):
        self.name = name
        self.root = project_root or _project_root()
        self._csv_path = self.root / "trades" / f"{name}_trades.csv"
        self._state_path = self.root / "state" / f"{name}_state.json"
        self._perf_path = self.root / "perf" / f"{name}_perf.json"
        self._log_path = self.root / "logs" / f"{name}.log"

        # Ensure directories exist before first write.
        for path in (self._csv_path, self._state_path, self._perf_path, self._log_path):
            path.parent.mkdir(parents=True, exist_ok=True)

        self._csv_lock = threading.Lock()
        self._logger = self._setup_text_logger()

    def _setup_text_logger(self) -> logging.Logger:
        log = logging.getLogger(f"paper.{self.name}")
        log.setLevel(logging.INFO)
        # Avoid duplicate handlers if the same name is reconstructed.
        if not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in log.handlers):
            handler = logging.handlers.RotatingFileHandler(
                self._log_path,
                maxBytes=10 * 1024 * 1024,  # 10 MB
                backupCount=5,
                encoding="utf-8",
            )
            formatter = logging.Formatter(
                "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
            )
            handler.setFormatter(formatter)
            log.addHandler(handler)
        return log

    def info(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._logger.info(msg, *args, **kwargs)

    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._logger.warning(msg, *args, **kwargs)

    def error(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._logger.error(msg, *args, **kwargs)

    def log_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Log a structured event to the text log (used by runners for discovery, errors, etc.)."""
        self._logger.info("event=%s %s", event_type, json.dumps(data, default=str))

    def log_trade(self, trade: dict[str, Any], strategy: str | None = None) -> None:
        """
        Append or update one trade row in the CSV.

        Open trades are appended with empty exit columns. Closed trades update
        the matching open row (matched by condition_id + opened_at) so each
        trade appears exactly once in the file.
        """
        strategy_name = strategy or self.name
        is_closed = trade.get("exit_price") is not None

        gross_shares = trade.get("shares", 0.0) or 0.0
        fee_shares = trade.get("fee_shares", 0.0) or 0.0
        net_shares = max(0.0, gross_shares - fee_shares)
        row = dict.fromkeys(TRADE_FIELDS)
        row.update(
            {
                "timestamp": trade.get("closed_at") or trade.get("opened_at"),
                "strategy": strategy_name,
                "condition_id": trade.get("condition_id"),
                "direction": trade.get("direction"),
                "entry_price": trade.get("entry_price"),
                "shares": gross_shares,
                "fee_shares": fee_shares,
                "entry_notional": trade.get("entry_notional"),
                "entry_fee": trade.get("entry_fee"),
                "exit_price": trade.get("exit_price"),
                "exit_notional": (
                    trade.get("exit_price", 0.0) * net_shares
                    if trade.get("exit_price") is not None
                    else 0.0
                ),
                "exit_fee": trade.get("exit_fee", 0.0),
                "pnl": trade.get("pnl", 0.0),
                "exit_reason": trade.get("exit_reason", ""),
            }
        )

        with self._csv_lock:
            file_exists = self._csv_path.exists() and self._csv_path.stat().st_size > 0

            if not is_closed or not file_exists:
                with open(self._csv_path, "a", newline="", encoding="utf-8") as fh:
                    writer = csv.DictWriter(fh, fieldnames=TRADE_FIELDS)
                    if not file_exists:
                        writer.writeheader()
                    writer.writerow(row)
                return

            # Closed trade: try to update the matching open row in-place.
            rows: list[dict[str, Any]] = []
            matched = False
            try:
                with open(self._csv_path, "r", newline="", encoding="utf-8") as fh:
                    reader = csv.DictReader(fh)
                    for existing in reader:
                        if (
                            not matched
                            and existing.get("condition_id") == row["condition_id"]
                            and existing.get("strategy") == row["strategy"]
                            and existing.get("exit_price") in ("", None)
                            and existing.get("entry_price") == str(row["entry_price"])
                        ):
                            existing.update(row)
                            matched = True
                        rows.append(existing)
            except Exception:
                # If we cannot read the file, fall back to appending the close row.
                rows = []

            if matched:
                with open(self._csv_path, "w", newline="", encoding="utf-8") as fh:
                    writer = csv.DictWriter(fh, fieldnames=TRADE_FIELDS)
                    writer.writeheader()
                    writer.writerows(rows)
            else:
                # No matching open row found (e.g., legacy file); append the close row.
                with open(self._csv_path, "a", newline="", encoding="utf-8") as fh:
                    writer = csv.DictWriter(fh, fieldnames=TRADE_FIELDS)
                    if not file_exists:
                        writer.writeheader()
                    writer.writerow(row)

    def write_state(self, state: dict[str, Any]) -> None:
        """Atomically write the state JSON file."""
        self._write_json(self._state_path, state)

    def write_perf(self, perf: dict[str, Any]) -> None:
        """Atomically write the perf JSON file."""
        self._write_json(self._perf_path, perf)

    @staticmethod
    def _write_json(path: Path, data: dict[str, Any]) -> None:
        tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, default=str)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


# Backward-compatible alias used by the generated runners.
StrategyLogger = TradeLogger

__all__ = ["TradeLogger", "StrategyLogger", "TRADE_FIELDS"]
