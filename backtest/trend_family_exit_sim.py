#!/usr/bin/env python3
"""
Post-process trend-family hold-to-expiry trades to simulate early exits.

Reads raw Polymarket BTC 5m up/down trades and local 1m BTCUSDT reference bars,
estimates an intraday mark price for each binary contract, and applies stop-loss,
time-stop, and take-profit rules.  Outputs comparison tables vs. the baseline.

Usage:
    python3 trend_family_exit_sim.py
"""

# CHANGE_SUMMARY
# 2026-07-17  kilo_exit_test
#   - Created trend_family_exit_sim.py to test early exits on trend-family legs.
#   - Uses a per-trade calibrated logit mark model driven by 1m spot bars.
#   - Tests stop-loss, time-stop, take-profit, and combined rules.
# WHY: Trend-family legs hold to expiry and are the main drawdown driver.

from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data" / "trend_exits"
TRADES_DIR = DATA_DIR / "trades"
BARS_DIR = DATA_DIR / "bars"
REPORT_DIR = ROOT / "reports"
REPORT_DIR.mkdir(parents=True, exist_ok=True)

TOP_LEGS = [
    "tf_dema_lb20_dev002_emax85_alp0001",
    "tf_vwap_ticks_lb50_dev002_emax80",
    "tf_dema_lb20_dev002_emax85_alp0002",
    "tf_alma_lb50_dev002_emax80_alm0075_alm6",
    "tf_holt_lb50_dev002_emax85_alp0002_hol0005",
]

WINDOW_SEC = 300.0  # Polymarket BTC 5m up/down window
FEE_ROUND_DECIMALS = 5
MIN_FEE_USDC = 0.00001
DEFAULT_TAKER_RATE = 0.07


def sigmoid(z: float) -> float:
    """Numerically stable sigmoid."""
    if z >= 0:
        ez = math.exp(-z)
        return 1.0 / (1.0 + ez)
    else:
        ez = math.exp(z)
        return ez / (1.0 + ez)


def logit(p: float) -> float:
    """Logit, clipped away from 0/1."""
    p = max(1e-6, min(1.0 - 1e-6, p))
    return math.log(p / (1.0 - p))


# ---------------------------------------------------------------------------
# Fee helpers copied from engine/execution.py to keep the script standalone.
# ---------------------------------------------------------------------------
def fee_rate(fee_schedule: Any) -> float:
    if isinstance(fee_schedule, dict):
        rate = fee_schedule.get("rate")
        if rate is None:
            rate = fee_schedule.get("takerRate")
        if rate is not None:
            try:
                return float(rate)
            except (ValueError, TypeError):
                pass
    return DEFAULT_TAKER_RATE


def calculate_taker_fee(shares: float, price: float, fee_schedule: Any = None) -> float:
    if shares <= 0 or price <= 0:
        return 0.0
    rate = fee_rate(fee_schedule)
    raw_fee = float(shares) * rate * float(price) * (1.0 - float(price))
    if raw_fee <= 0.0:
        return 0.0
    fee = round(raw_fee + 1e-12, FEE_ROUND_DECIMALS)
    return max(MIN_FEE_USDC, fee)


def taker_fee_shares(gross_shares: float, price: float, fee_schedule: Any = None) -> float:
    if gross_shares <= 0 or price <= 0:
        return 0.0
    return round(calculate_taker_fee(gross_shares, price, fee_schedule) / price, FEE_ROUND_DECIMALS)


# ---------------------------------------------------------------------------
# Reference bars
# ---------------------------------------------------------------------------
def load_bars(bars_dir: Path) -> Dict[int, Dict[str, float]]:
    """Load all 1m bars into a dict keyed by open_time in seconds."""
    bars: Dict[int, Dict[str, float]] = {}
    if not bars_dir.exists():
        raise FileNotFoundError(f"Bars directory not found: {bars_dir}")
    for zf in sorted(bars_dir.glob("BTCUSDT-1m-*.zip")):
        import zipfile

        with zipfile.ZipFile(zf) as z:
            for name in z.namelist():
                with z.open(name) as fh:
                    for line in fh:
                        line = line.decode().strip()
                        if not line:
                            continue
                        parts = line.split(",")
                        # Binance kline: open_time_ms, open, high, low, close, volume, close_time_ms, ...
                        open_ts = int(parts[0]) // 1_000_000  # us -> s
                        bars[open_ts] = {
                            "open": float(parts[1]),
                            "high": float(parts[2]),
                            "low": float(parts[3]),
                            "close": float(parts[4]),
                            "volume": float(parts[5]),
                            "close_ts": int(parts[6]) // 1_000_000,
                        }
    return bars


# ---------------------------------------------------------------------------
# Trade parsing
# ---------------------------------------------------------------------------
def load_trades(trades_path: Path) -> List[Dict[str, Any]]:
    trades: List[Dict[str, Any]] = []
    opener = gzip.open if str(trades_path).endswith(".gz") else open
    with opener(trades_path, "rt") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            trades.append(json.loads(line))
    return trades


def trade_times(trade: Dict[str, Any]) -> Tuple[float, float, float]:
    """Return (entry_unix_ts, expiry_unix_ts, window_sec)."""
    market = trade.get("market", {})
    start_iso = market.get("start_date_iso")
    if start_iso:
        start_ts = datetime.fromisoformat(start_iso).timestamp()
    else:
        # Fallback: closed_at - opened_at approximates the end of window
        start_ts = trade["closed_at"] - WINDOW_SEC
    entry_ts = start_ts + float(trade["opened_at"])
    expiry_ts = start_ts + WINDOW_SEC
    return entry_ts, expiry_ts, WINDOW_SEC


# ---------------------------------------------------------------------------
# Mark model
# ---------------------------------------------------------------------------
def calibrate_sigma(trade: Dict[str, Any], entry_ts: float, expiry_ts: float) -> Optional[float]:
    """Return calibrated sigma for the logit mark model, or None if degenerate."""
    market = trade.get("market", {})
    reference = market.get("open_oracle_price") or trade.get("entry_spot")
    if reference is None or reference <= 0:
        return None

    entry_spot = float(trade.get("entry_spot", reference))
    entry_price = float(trade["entry_price"])
    direction = trade["direction"]
    tau_entry = max(1e-9, (expiry_ts - entry_ts) / WINDOW_SEC)

    x_entry = (entry_spot - reference) / reference
    if abs(x_entry) < 1e-12:
        return None

    lp = logit(entry_price)
    if abs(lp) < 1e-12:
        return None

    if direction == "YES":
        sigma = x_entry / (math.sqrt(tau_entry) * lp)
    else:  # NO
        sigma = -x_entry / (math.sqrt(tau_entry) * lp)

    if sigma <= 0 or not math.isfinite(sigma):
        return None
    return sigma


def mark_price(spot: float, reference: float, tau: float, sigma: float, direction: str) -> float:
    """Estimated binary contract price at a point in time."""
    if reference <= 0 or tau <= 0 or sigma <= 0:
        return 0.5
    x = (spot - reference) / reference
    z = x / (sigma * math.sqrt(max(tau, 1e-9)))
    if direction == "YES":
        return sigmoid(z)
    else:
        return sigmoid(-z)


# ---------------------------------------------------------------------------
# Exit simulation
# ---------------------------------------------------------------------------
def bars_for_trade(trade: Dict[str, Any], entry_ts: float, expiry_ts: float, bars: Dict[int, Dict[str, float]]) -> List[Tuple[float, Dict[str, float]]]:
    """Return sorted list of (bar_open_ts, bar) covering the trade window."""
    start_minute = int(entry_ts // 60) * 60
    # Include the bar that contains entry and the bar that contains expiry.
    out: List[Tuple[float, Dict[str, float]]] = []
    for ts in range(start_minute, int(expiry_ts) + 60, 60):
        b = bars.get(ts)
        if b is not None:
            out.append((float(ts), b))
    return out


def simulate_trade(trade: Dict[str, Any], bars: Dict[int, Dict[str, float]],
                   stop_pct: Optional[float] = None,
                   time_stop_sec: Optional[float] = None,
                   target: Optional[float] = None,
                   default_sigma: Optional[float] = None) -> Dict[str, Any]:
    """
    Simulate a single trade with optional early-exit rules.
    Returns a dict with the simulated exit price, reason, and pnl.
    """
    entry_ts, expiry_ts, _ = trade_times(trade)
    market = trade.get("market", {})
    reference = market.get("open_oracle_price") or trade.get("entry_spot")
    if reference is None or reference <= 0:
        reference = trade.get("entry_spot", 0.0)

    entry_price = float(trade["entry_price"])
    direction = trade["direction"]
    fee_schedule = market.get("fee_schedule")

    sigma = calibrate_sigma(trade, entry_ts, expiry_ts)
    if sigma is None:
        sigma = default_sigma if default_sigma is not None else 0.001

    gross_shares = float(trade.get("shares", 0.0))
    fee_shares = float(trade.get("fee_shares", 0.0))
    net_shares = max(0.0, gross_shares - fee_shares)
    entry_fee = float(trade.get("entry_fee", 0.0))

    stop_level = entry_price * (1.0 - stop_pct) if stop_pct is not None else None

    trade_bars = bars_for_trade(trade, entry_ts, expiry_ts, bars)
    exit_price = None
    exit_reason = "expiry_resolve"

    for bar_ts, bar in trade_bars:
        bar_end = bar_ts + 60.0
        if bar_end <= entry_ts:
            continue  # bar fully before entry
        if bar_ts >= expiry_ts:
            break

        # Fraction of the 5m window remaining at the middle of this bar.
        mid_ts = bar_ts + 30.0
        tau = max(0.0, (expiry_ts - mid_ts) / WINDOW_SEC)

        # Use high/low to check intrabar stop/target touches.
        if direction == "YES":
            mark_high = mark_price(bar["high"], reference, tau, sigma, direction)
            mark_low = mark_price(bar["low"], reference, tau, sigma, direction)
            mark_close = mark_price(bar["close"], reference, tau, sigma, direction)
        else:
            # For NO, high spot hurts (lower mark), low spot helps (higher mark).
            mark_high = mark_price(bar["high"], reference, tau, sigma, direction)
            mark_low = mark_price(bar["low"], reference, tau, sigma, direction)
            mark_close = mark_price(bar["close"], reference, tau, sigma, direction)

        elapsed = mid_ts - entry_ts

        # Check take-profit first (conservative: if touched, exit at target).
        if target is not None:
            if direction == "YES" and mark_high >= target:
                exit_price = target
                exit_reason = f"take_profit_{target:.2f}"
                break
            if direction == "NO" and mark_low <= (1.0 - target):
                exit_price = 1.0 - target
                exit_reason = f"take_profit_{target:.2f}"
                break

        # Check stop-loss (conservative: if touched, exit at stop level).
        if stop_level is not None:
            if direction == "YES" and mark_low <= stop_level:
                exit_price = stop_level
                exit_reason = f"stop_loss_{stop_pct:.3f}"
                break
            if direction == "NO" and mark_high >= stop_level:
                exit_price = stop_level
                exit_reason = f"stop_loss_{stop_pct:.3f}"
                break

        # Time-stop: exit if not profitable after N seconds.
        if time_stop_sec is not None and elapsed >= time_stop_sec:
            if mark_close < entry_price:
                exit_price = mark_close
                exit_reason = f"time_stop_{int(time_stop_sec)}s"
                break

    if exit_price is None:
        exit_price = float(trade.get("exit_price", 0.0))
        exit_reason = trade.get("exit_reason", "expiry_resolve")

    exit_fee = calculate_taker_fee(net_shares, exit_price, fee_schedule)
    pnl = net_shares * (exit_price - entry_price) - entry_fee - exit_fee

    return {
        **trade,
        "sim_exit_price": exit_price,
        "sim_exit_reason": exit_reason,
        "sim_pnl": pnl,
        "sim_sigma": sigma,
    }


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
def equity_curve(trades: List[Dict[str, Any]]) -> List[float]:
    """Cumulative PnL assuming sequential trades."""
    cap = 0.0
    curve = []
    for t in trades:
        cap += t.get("sim_pnl", t.get("pnl", 0.0))
        curve.append(cap)
    return curve


def max_drawdown(curve: List[float]) -> float:
    if not curve:
        return 0.0
    peak = curve[0]
    dd = 0.0
    for v in curve:
        if v > peak:
            peak = v
        dd = min(dd, v - peak)
    return dd


def win_rate(trades: List[Dict[str, Any]]) -> float:
    if not trades:
        return 0.0
    wins = sum(1 for t in trades if t.get("sim_pnl", t.get("pnl", 0.0)) > 0)
    return wins / len(trades)


def metrics(trades: List[Dict[str, Any]], key: str = "sim_pnl") -> Dict[str, Any]:
    curve = equity_curve(trades)
    pnls = [t.get(key, t.get("pnl", 0.0)) for t in trades]
    return {
        "trades": len(trades),
        "total_pnl": round(sum(pnls), 4),
        "avg_pnl": round(sum(pnls) / len(pnls), 6) if pnls else 0.0,
        "win_rate": round(win_rate(trades), 4),
        "max_dd": round(max_drawdown(curve), 4),
        "profit_factor": profit_factor(pnls),
    }


def profit_factor(pnls: List[float]) -> float:
    gains = sum(p for p in pnls if p > 0)
    losses = abs(sum(p for p in pnls if p < 0))
    return round(gains / losses, 4) if losses > 0 else float("inf")


# ---------------------------------------------------------------------------
# Rule definitions
# ---------------------------------------------------------------------------
def build_rules() -> List[Tuple[str, Dict[str, Optional[float]]]]:
    rules: List[Tuple[str, Dict[str, Optional[float]]]] = [
        ("baseline", {"stop_pct": None, "time_stop_sec": None, "target": None}),
    ]
    for pct in [0.01, 0.02, 0.03, 0.05]:
        rules.append((f"stop_{int(pct*100)}pct", {"stop_pct": pct}))
    for sec in [60, 120, 180]:
        rules.append((f"time_stop_{sec}s", {"time_stop_sec": float(sec)}))
    for tgt in [0.90, 0.93, 0.95, 0.97]:
        rules.append((f"tp_{int(tgt*100)}", {"target": tgt}))
    # Combinations: stop-loss + take-profit
    for pct in [0.02, 0.03, 0.05]:
        for tgt in [0.93, 0.95, 0.97]:
            rules.append((f"stop_{int(pct*100)}pct_tp_{int(tgt*100)}", {"stop_pct": pct, "target": tgt}))
    return rules


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run_leg(leg: str, bars: Dict[int, Dict[str, float]], rules: List[Tuple[str, Dict[str, Optional[float]]]], default_sigma: float) -> Dict[str, Any]:
    trades_path = TRADES_DIR / f"{leg}.trades.jsonl.gz"
    if not trades_path.exists():
        print(f"  WARN: missing {trades_path}", file=sys.stderr)
        return {}

    raw_trades = load_trades(trades_path)
    # Attach baseline pnl as sim_pnl for consistent metric code.
    for t in raw_trades:
        t["sim_pnl"] = float(t.get("pnl", 0.0))

    results: Dict[str, Dict[str, Any]] = {}
    baseline_metrics = metrics(raw_trades)
    results["baseline"] = baseline_metrics

    for name, kwargs in rules[1:]:
        simmed = [simulate_trade(t, bars, default_sigma=default_sigma, **kwargs) for t in raw_trades]
        results[name] = metrics(simmed, key="sim_pnl")

    return results


def print_table(leg: str, results: Dict[str, Dict[str, Any]]) -> str:
    lines = []
    lines.append(f"\n## {leg}")
    lines.append("| rule | trades | total_pnl | avg_pnl | win_rate | max_dd | profit_factor |")
    lines.append("|------|--------|-----------|---------|----------|--------|---------------|")
    baseline_pnl = results.get("baseline", {}).get("total_pnl", 0.0)
    baseline_dd = results.get("baseline", {}).get("max_dd", 0.0)

    for name in ["baseline"] + [n for n, _ in build_rules()[1:]]:
        if name not in results:
            continue
        m = results[name]
        pnl = m["total_pnl"]
        dd = m["max_dd"]
        pnl_delta = pnl - baseline_pnl
        dd_delta = dd - baseline_dd
        lines.append(
            f"| {name:28} | {m['trades']:>6} | "
            f"{pnl:>9.2f} ({pnl_delta:+.2f}) | {m['avg_pnl']:>7.4f} | "
            f"{m['win_rate']*100:>6.2f}% | {dd:>8.2f} ({dd_delta:+.2f}) | {m['profit_factor']:>13.4f} |"
        )
    return "\n".join(lines)


def best_rule(results: Dict[str, Dict[str, Any]]) -> Tuple[str, Dict[str, Any]]:
    """
    Pick the rule that improves both total PnL and max drawdown vs. baseline.
    Among those, prefer the highest PnL / |max_dd| ratio (Calmar-like).
    If no rule improves both, fall back to the rule with the highest total PnL.
    """
    baseline = results["baseline"]
    improved: List[Tuple[str, Dict[str, Any], float]] = []
    fallback: List[Tuple[str, Dict[str, Any], float]] = []
    for name, m in results.items():
        if name == "baseline":
            continue
        pnl_better = m["total_pnl"] >= baseline["total_pnl"]
        dd_better = m["max_dd"] >= baseline["max_dd"]  # less negative
        ratio = m["total_pnl"] / abs(m["max_dd"]) if m["max_dd"] != 0 else float("inf")
        if pnl_better and dd_better:
            improved.append((name, m, ratio))
        fallback.append((name, m, m["total_pnl"]))
    if improved:
        improved.sort(key=lambda x: -x[2])
        return improved[0][0], improved[0][1]
    fallback.sort(key=lambda x: -x[2])
    return fallback[0][0], fallback[0][1]


def main() -> int:
    parser = argparse.ArgumentParser(description="Simulate early exits on trend-family legs.")
    parser.add_argument("--legs", nargs="+", default=TOP_LEGS, help="Leg names to analyse.")
    parser.add_argument("--default-sigma", type=float, default=0.001, help="Fallback sigma for degenerate calibrations.")
    args = parser.parse_args()

    print("Loading reference bars...")
    bars = load_bars(BARS_DIR)
    print(f"  Loaded {len(bars)} 1m bars.")

    print("Loading trades and simulating exits...")
    rules = build_rules()
    all_results: Dict[str, Dict[str, Dict[str, Any]]] = {}
    summary_lines = []

    for leg in args.legs:
        print(f"  {leg}")
        results = run_leg(leg, bars, rules, args.default_sigma)
        if not results:
            continue
        all_results[leg] = results
        table = print_table(leg, results)
        print(table)
        summary_lines.append(table)

        best, best_m = best_rule(results)
        print(f"  -> best exit rule: {best}  PnL={best_m['total_pnl']:.2f}  maxDD={best_m['max_dd']:.2f}  trades={best_m['trades']}")

    # Write markdown report.
    report_path = REPORT_DIR / "trend_family_exit_report.md"
    with open(report_path, "w") as fh:
        fh.write("# Trend-Family Early-Exit Simulation Report\n\n")
        fh.write("Method: post-process hold-to-expiry trades with a per-trade calibrated logit mark model\n")
        fh.write("driven by 1m BTCUSDT reference bars.\n\n")
        fh.write("Rules tested:\n")
        fh.write("- Stop-loss: -1%, -2%, -3%, -5% of entry price\n")
        fh.write("- Time-stop: 60s, 120s, 180s if not profitable\n")
        fh.write("- Take-profit: 0.90, 0.93, 0.95, 0.97\n")
        fh.write("- Combinations: stop-loss + take-profit\n\n")
        fh.write("\n".join(summary_lines))
        fh.write("\n\n## Best rule per leg\n\n")
        fh.write("| leg | best_rule | total_pnl | max_dd | win_rate | profit_factor |\n")
        fh.write("|-----|-----------|-----------|--------|----------|---------------|\n")
        for leg in all_results:
            best, m = best_rule(all_results[leg])
            fh.write(
                f"| {leg} | {best} | {m['total_pnl']:.2f} | {m['max_dd']:.2f} | "
                f"{m['win_rate']*100:.2f}% | {m['profit_factor']:.4f} |\n"
            )

    print(f"\nReport written to {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
