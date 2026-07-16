# CHANGE_SUMMARY
# 2026-07-16  subagent
#   - Created metrics.py with compute_metrics calculating win rate, profit
#     factor, Sharpe, max drawdown, and average trades per day.
# WHY: Backtest and live monitoring need standardized performance stats.

"""Performance metric calculators for the FVG Topstep engine."""

from __future__ import annotations

import math
from datetime import datetime
from statistics import mean, stdev
from typing import Any

from fvg_topstep.types import TradeRecord


def compute_metrics(
    trades: list[TradeRecord],
    equity_curve: list[tuple[datetime, float]],
    initial_capital: float,
) -> dict[str, Any]:
    """Compute standard trading performance metrics.

    Parameters
    ----------
    trades:
        Completed round-turn trades.
    equity_curve:
        Chronological list of ``(timestamp, equity)`` observations.
    initial_capital:
        Starting equity.

    Returns
    -------
    dict
        win_rate, profit_factor, sharpe_ratio, max_drawdown_pct,
        max_drawdown_dollar, avg_trades_per_day, avg_win, avg_loss, net_pnl.
    """
    if not trades:
        return {
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "sharpe_ratio": 0.0,
            "max_drawdown_pct": 0.0,
            "max_drawdown_dollar": 0.0,
            "avg_trades_per_day": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "net_pnl": 0.0,
        }

    wins = [t.net_pnl for t in trades if t.net_pnl > 0]
    losses = [t.net_pnl for t in trades if t.net_pnl <= 0]
    net_pnl = sum(t.net_pnl for t in trades)

    win_rate = len(wins) / len(trades) if trades else 0.0
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0.0

    avg_win = mean(wins) if wins else 0.0
    avg_loss = mean(losses) if losses else 0.0

    max_dd_dollar, max_dd_pct = _max_drawdown(equity_curve, initial_capital)
    sharpe = _sharpe_ratio(equity_curve)
    avg_trades_per_day = _avg_trades_per_day(trades)

    return {
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "sharpe_ratio": sharpe,
        "max_drawdown_pct": max_dd_pct,
        "max_drawdown_dollar": max_dd_dollar,
        "avg_trades_per_day": avg_trades_per_day,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "net_pnl": net_pnl,
    }


def _max_drawdown(
    equity_curve: list[tuple[datetime, float]],
    initial_capital: float,
) -> tuple[float, float]:
    """Return (max drawdown dollar, max drawdown percent)."""
    if not equity_curve:
        return 0.0, 0.0

    peak = initial_capital
    max_dd_dollar = 0.0
    for _, equity in equity_curve:
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd_dollar:
            max_dd_dollar = dd

    max_dd_pct = (max_dd_dollar / peak) * 100.0 if peak > 0 else 0.0
    return max_dd_dollar, max_dd_pct


def _sharpe_ratio(equity_curve: list[tuple[datetime, float]]) -> float:
    """Approximate annualized Sharpe from daily equity returns.

    Uses 252 trading days/year.  Returns 0.0 when there is insufficient data.
    """
    if len(equity_curve) < 2:
        return 0.0

    by_date: dict[datetime.date, float] = {}
    for ts, eq in equity_curve:
        d = ts.date() if isinstance(ts, datetime) else ts
        by_date[d] = eq

    sorted_dates = sorted(by_date)
    returns: list[float] = []
    for prev, curr in zip(sorted_dates, sorted_dates[1:]):
        prev_eq = by_date[prev]
        curr_eq = by_date[curr]
        if prev_eq != 0:
            returns.append((curr_eq - prev_eq) / prev_eq)

    if len(returns) < 2:
        return 0.0

    avg = mean(returns)
    try:
        sd = stdev(returns)
    except Exception:
        return 0.0
    if sd == 0:
        return 0.0
    return (avg / sd) * math.sqrt(252)


def _avg_trades_per_day(trades: list[TradeRecord]) -> float:
    if not trades:
        return 0.0
    dates = sorted({t.entry_time.date() for t in trades})
    if len(dates) <= 1:
        return float(len(trades))
    days = max(1, (dates[-1] - dates[0]).days)
    return len(trades) / days


__all__ = ["compute_metrics"]
