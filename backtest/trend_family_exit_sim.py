#!/usr/bin/env python3
"""
Post-process trend-family hold-to-expiry trades to simulate early exits.

Version 2 uses REAL Polymarket BTC 5m order-book snapshot data from
  gdrive:polybacktest_60d/polymarket/btc/5m/
cached locally under backtest/data/polybacktest_real/snapshots/.

For each market snapshot the script extracts the order-book mid price
(best_bid + best_ask) / 2 for the traded side (YES -> orderbook_up,
NO -> orderbook_down), falling back to price_up/price_down when the book
is empty.  Missing exact timestamps are filled by linear interpolation
between the two nearest snapshots for that market.

Exit rules tested:
  - Stop-loss: -1%, -2%, -3%, -5% of entry price
  - Time-stop: 60s, 120s, 180s if not profitable
  - Take-profit: 0.90, 0.93, 0.95, 0.97
  - Combinations: stop-loss + take-profit

Outputs comparison tables vs. the baseline hold-to-expiry result.

Usage:
    python3 trend_family_exit_sim.py
"""

# CHANGE_SUMMARY
# 2026-07-17  kilo_exit_test
#   - Rewrote trend_family_exit_sim.py to use real Polymarket snapshot data
#     from gdrive:polybacktest_60d/polymarket/btc/5m/ instead of modeled BTC bars.
#   - Reads orderbook_up / orderbook_down mid prices, with price_up/price_down fallback.
#   - Linear interpolation for timestamps between snapshots.
#   - Same exit rules and report format as v1.
# WHY: User requested only real polybacktest data, not a spot-bar proxy model.

from __future__ import annotations

import argparse
import gzip
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone
from multiprocessing import Pool
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data" / "trend_exits"
TRADES_DIR = DATA_DIR / "trades"
SNAPSHOT_DIR = ROOT / "data" / "polybacktest_real" / "snapshots"
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
# Snapshot parsing and price extraction
# ---------------------------------------------------------------------------
def parse_iso_ts(iso: str) -> float:
    """Convert ISO-8601 timestamp (with optional Z) to Unix seconds."""
    if iso.endswith("Z"):
        iso = iso[:-1] + "+00:00"
    return datetime.fromisoformat(iso).timestamp()


def best_bid_ask(orderbook: Optional[Dict[str, Any]]) -> Tuple[Optional[float], Optional[float]]:
    """Return (best_bid, best_ask) from an orderbook dict, or (None, None)."""
    if not orderbook:
        return None, None
    bids = orderbook.get("bids") or []
    asks = orderbook.get("asks") or []
    best_bid = bids[0]["price"] if bids else None
    best_ask = asks[0]["price"] if asks else None
    return best_bid, best_ask


def side_mid_price(snap: Dict[str, Any], direction: str) -> Optional[float]:
    """
    Return the mid price for the traded side from a snapshot.
    YES  -> orderbook_up mid, fallback to price_up.
    NO   -> orderbook_down mid, fallback to price_down.
    """
    if direction == "YES":
        bid, ask = best_bid_ask(snap.get("orderbook_up"))
        if bid is not None and ask is not None:
            return (bid + ask) / 2.0
        return snap.get("price_up")
    else:
        bid, ask = best_bid_ask(snap.get("orderbook_down"))
        if bid is not None and ask is not None:
            return (bid + ask) / 2.0
        return snap.get("price_down")


def load_snapshot_series(market_id: str, snapshot_dir: Path) -> Optional[Dict[str, Any]]:
    """
    Load the snapshot time series for a market.
    Returns dict with 'ts' (np.array), 'yes' (np.array), 'no' (np.array) or None.
    """
    path = snapshot_dir / f"{market_id}.json.gz"
    if not path.exists():
        return None
    with gzip.open(path, "rt") as fh:
        snaps = json.load(fh)
    if not snaps:
        return None

    ts_list: List[float] = []
    yes_list: List[Optional[float]] = []
    no_list: List[Optional[float]] = []
    for snap in snaps:
        ts_list.append(parse_iso_ts(snap["time"]))
        yes_list.append(side_mid_price(snap, "YES"))
        no_list.append(side_mid_price(snap, "NO"))

    return {
        "ts": np.array(ts_list, dtype=float),
        "yes": np.array(yes_list, dtype=float),
        "no": np.array(no_list, dtype=float),
    }


def interpolate_price(ts: np.ndarray, prices: np.ndarray, t: float) -> Optional[float]:
    """
    Linearly interpolate price at time t.
    - If t is before first snapshot, use first price.
    - If t is after last snapshot, use last price.
    - If nearest surrounding snapshots are NaN, fallback to nearest valid price.
    """
    if len(ts) == 0:
        return None
    if t <= ts[0]:
        return float(prices[0]) if not math.isnan(prices[0]) else None
    if t >= ts[-1]:
        return float(prices[-1]) if not math.isnan(prices[-1]) else None

    idx = np.searchsorted(ts, t)
    # ts[idx-1] < t <= ts[idx]
    t0, p0 = ts[idx - 1], prices[idx - 1]
    t1, p1 = ts[idx], prices[idx]

    if not math.isnan(p0) and not math.isnan(p1):
        if t1 == t0:
            return float(p0)
        return float(p0 + (p1 - p0) * (t - t0) / (t1 - t0))

    # If one side is NaN, use the other side if valid.
    if not math.isnan(p0):
        return float(p0)
    if not math.isnan(p1):
        return float(p1)

    # Both surrounding prices are NaN: scan outward for nearest valid price.
    for offset in range(1, max(idx, len(ts) - idx) + 1):
        lo = idx - 1 - offset
        hi = idx + offset
        if lo >= 0 and not math.isnan(prices[lo]):
            return float(prices[lo])
        if hi < len(ts) and not math.isnan(prices[hi]):
            return float(prices[hi])
    return None


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
        start_ts = trade["closed_at"] - WINDOW_SEC
    entry_ts = start_ts + float(trade["opened_at"])
    expiry_ts = start_ts + WINDOW_SEC
    return entry_ts, expiry_ts, WINDOW_SEC


# ---------------------------------------------------------------------------
# Exit simulation
# ---------------------------------------------------------------------------
def close_trade(trade: Dict[str, Any], exit_price: float, exit_reason: str) -> Dict[str, Any]:
    """Return a copy of the trade with simulated PnL for the given exit."""
    entry_price = float(trade["entry_price"])
    fee_schedule = trade.get("market", {}).get("fee_schedule")
    gross_shares = float(trade.get("shares", 0.0))
    fee_shares = float(trade.get("fee_shares", 0.0))
    net_shares = max(0.0, gross_shares - fee_shares)
    entry_fee = float(trade.get("entry_fee", 0.0))
    exit_fee = calculate_taker_fee(net_shares, exit_price, fee_schedule)
    pnl = net_shares * (exit_price - entry_price) - entry_fee - exit_fee
    return {
        **trade,
        "sim_exit_price": exit_price,
        "sim_exit_reason": exit_reason,
        "sim_pnl": pnl,
    }


def simulate_trade_all_rules(
    trade: Dict[str, Any],
    series: Dict[str, Any],
    rules: List[Tuple[str, Dict[str, Optional[float]]]],
) -> Dict[str, Dict[str, Any]]:
    """
    Simulate a single trade once, applying all exit rules in a single forward scan.
    Returns {rule_name: sim_trade_result}.  The baseline rule is named "baseline".
    """
    entry_ts, expiry_ts, _ = trade_times(trade)
    direction = trade["direction"]
    entry_price = float(trade["entry_price"])

    ts = series["ts"]
    prices = series["yes"] if direction == "YES" else series["no"]

    # Results holder: each rule starts unset.
    results: Dict[str, Optional[Tuple[float, str]]] = {name: None for name, _ in rules}

    # Helper to resolve a snapshot price.
    def price_at(i: int) -> Optional[float]:
        p = prices[i]
        if not math.isnan(p):
            return float(p)
        return interpolate_price(ts, prices, ts[i])

    # Build rule-specific levels once.
    rule_specs: List[Tuple[str, Optional[float], Optional[float], Optional[float], Optional[float]]] = []
    for name, kwargs in rules:
        stop_pct = kwargs.get("stop_pct")
        time_stop_sec = kwargs.get("time_stop_sec")
        target = kwargs.get("target")
        stop_level = entry_price * (1.0 - stop_pct) if stop_pct is not None else None
        rule_specs.append((name, stop_level, time_stop_sec, target, stop_pct))

    start_idx = int(np.searchsorted(ts, entry_ts))
    for i in range(start_idx, len(ts)):
        t_i = ts[i]
        if t_i > expiry_ts:
            break

        p_i = price_at(i)
        if p_i is None:
            continue
        elapsed = t_i - entry_ts

        for name, stop_level, time_stop_sec, target, stop_pct in rule_specs:
            if results[name] is not None:
                continue

            # Take-profit.
            if target is not None:
                if direction == "YES" and p_i >= target:
                    results[name] = (target, f"take_profit_{target:.2f}")
                    continue
                if direction == "NO" and p_i <= (1.0 - target):
                    results[name] = (1.0 - target, f"take_profit_{target:.2f}")
                    continue

            # Stop-loss.
            if stop_level is not None:
                if direction == "YES" and p_i <= stop_level:
                    results[name] = (stop_level, f"stop_loss_{stop_pct:.3f}")
                    continue
                if direction == "NO" and p_i >= stop_level:
                    results[name] = (stop_level, f"stop_loss_{stop_pct:.3f}")
                    continue

            # Time-stop.
            if time_stop_sec is not None and elapsed >= time_stop_sec:
                if p_i < entry_price:
                    results[name] = (p_i, f"time_stop_{int(time_stop_sec)}s")
                    continue

        # Early exit from the snapshot scan once every active rule has triggered.
        if all(v is not None for v in results.values()):
            break

    # Any rule that never triggered uses the original expiry resolution.
    expiry_exit = float(trade.get("exit_price", 0.0))
    expiry_reason = trade.get("exit_reason", "expiry_resolve")

    out: Dict[str, Dict[str, Any]] = {}
    for name, _ in rules:
        exit_price, exit_reason = results[name] if results[name] is not None else (expiry_exit, expiry_reason)
        out[name] = close_trade(trade, exit_price, exit_reason)
    return out


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
def equity_curve(trades: List[Dict[str, Any]]) -> List[float]:
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


def profit_factor(pnls: List[float]) -> float:
    gains = sum(p for p in pnls if p > 0)
    losses = abs(sum(p for p in pnls if p < 0))
    return round(gains / losses, 4) if losses > 0 else float("inf")


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
    for pct in [0.02, 0.03, 0.05]:
        for tgt in [0.93, 0.95, 0.97]:
            rules.append((f"stop_{int(pct*100)}pct_tp_{int(tgt*100)}", {"stop_pct": pct, "target": tgt}))
    return rules


# ---------------------------------------------------------------------------
# Multiprocessing worker
# ---------------------------------------------------------------------------
def _process_market_chunk(
    chunk: List[Tuple[str, List[Tuple[str, Dict[str, Any]]]]],
    snapshot_dir: Path,
    rules: List[Tuple[str, Dict[str, Optional[float]]]],
    worker_id: int,
) -> Tuple[Dict[str, Dict[str, List[Dict[str, Any]]]], Dict[str, int]]:
    """
    Process a chunk of (market_id, labelled_trades) in a subprocess.
    Returns partial accumulators and snapshot coverage counts per leg.
    """
    accum: Dict[str, Dict[str, List[Dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    coverage: Dict[str, int] = defaultdict(int)
    total = len(chunk)
    for idx, (market_id, labelled_trades) in enumerate(chunk, 1):
        if idx % 200 == 0:
            print(f"    worker {worker_id}: {idx}/{total} markets...", file=sys.stderr)
        series = load_snapshot_series(market_id, snapshot_dir)
        if series is None:
            for leg, t in labelled_trades:
                for name, _ in rules:
                    accum[leg][name].append(t)
            continue
        for leg, t in labelled_trades:
            coverage[leg] += 1
            sim_results = simulate_trade_all_rules(t, series, rules)
            for name, sim_trade in sim_results.items():
                accum[leg][name].append(sim_trade)
    return dict(accum), dict(coverage)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def process_all_legs(
    legs: List[str],
    snapshot_dir: Path,
    rules: List[Tuple[str, Dict[str, Optional[float]]]],
    workers: int = 1,
) -> Tuple[Dict[str, Dict[str, Dict[str, Any]]], Dict[str, Tuple[int, int]]]:
    """
    Process all legs together.  When workers > 1, split markets across a pool.
    Returns (results_by_leg, coverage_by_leg).
    """
    # Load all trades, keeping leg label.
    all_trades_by_leg: Dict[str, List[Dict[str, Any]]] = {}
    trades_by_market: Dict[str, List[Tuple[str, Dict[str, Any]]]] = defaultdict(list)
    coverage: Dict[str, Tuple[int, int]] = {}

    for leg in legs:
        trades_path = TRADES_DIR / f"{leg}.trades.jsonl.gz"
        if not trades_path.exists():
            print(f"  WARN: missing {trades_path}", file=sys.stderr)
            coverage[leg] = (0, 0)
            continue
        raw_trades = load_trades(trades_path)
        for t in raw_trades:
            t["sim_pnl"] = float(t.get("pnl", 0.0))
            trades_by_market[t["condition_id"]].append((leg, t))
        all_trades_by_leg[leg] = raw_trades
        coverage[leg] = (len(raw_trades), 0)

    # Accumulators per leg per rule.
    accum: Dict[str, Dict[str, List[Dict[str, Any]]]] = {
        leg: {name: [] for name, _ in rules}
        for leg in all_trades_by_leg
    }

    market_items = list(trades_by_market.items())

    if workers <= 1:
        partial_accum, partial_cov = _process_market_chunk(market_items, snapshot_dir, rules, 0)
        for leg, rule_accum in partial_accum.items():
            for name, trades in rule_accum.items():
                accum[leg][name].extend(trades)
        for leg, count in partial_cov.items():
            coverage[leg] = (coverage[leg][0], coverage[leg][1] + count)
    else:
        # Split markets into workers chunks.
        chunks: List[List[Tuple[str, List[Tuple[str, Dict[str, Any]]]]]] = [[] for _ in range(workers)]
        for idx, item in enumerate(market_items):
            chunks[idx % workers].append(item)

        with Pool(workers) as pool:
            args = [(chunk, snapshot_dir, rules, i) for i, chunk in enumerate(chunks) if chunk]
            results = pool.starmap(_process_market_chunk, args)

        for partial_accum, partial_cov in results:
            for leg, rule_accum in partial_accum.items():
                for name, trades in rule_accum.items():
                    accum[leg][name].extend(trades)
            for leg, count in partial_cov.items():
                coverage[leg] = (coverage[leg][0], coverage[leg][1] + count)

    results_by_leg: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for leg in all_trades_by_leg:
        results_by_leg[leg] = {}
        for name, _ in rules:
            results_by_leg[leg][name] = metrics(accum[leg][name])

    return results_by_leg, coverage


def print_table(leg: str, results: Dict[str, Dict[str, Any]]) -> str:
    lines = []
    lines.append(f"\n## {leg}")
    lines.append("| rule | trades | total_pnl | avg_pnl | win_rate | max_dd | profit_factor |")
    lines.append("|------|--------|-----------|---------|----------|--------|---------------|")
    baseline_pnl = results.get("baseline", {}).get("total_pnl", 0.0)
    baseline_dd = results.get("baseline", {}).get("max_dd", 0.0)

    rule_names = ["baseline"] + [n for n, _ in build_rules()[1:]]
    for name in rule_names:
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
    parser = argparse.ArgumentParser(description="Simulate early exits on trend-family legs using real Polybacktest snapshots.")
    parser.add_argument("--legs", nargs="+", default=TOP_LEGS, help="Leg names to analyse.")
    parser.add_argument("--snapshot-dir", type=Path, default=SNAPSHOT_DIR, help="Directory of market snapshot .json.gz files.")
    parser.add_argument("--output-json", type=Path, default=None, help="Write raw results to JSON for later combination.")
    parser.add_argument("--workers", type=int, default=1, help="Number of parallel processes for market processing.")
    args = parser.parse_args()

    if not args.snapshot_dir.exists():
        print(f"Snapshot directory not found: {args.snapshot_dir}", file=sys.stderr)
        print("Pull snapshots from gdrive:polybacktest_60d/polymarket/btc/5m/ first.", file=sys.stderr)
        return 1

    print(f"Using snapshots from: {args.snapshot_dir}")
    print(f"Mark price = order-book mid (best_bid + best_ask) / 2; fallback to price_up/price_down.")
    print("Loading trades and simulating exits...")

    rules = build_rules()
    all_results, coverage = process_all_legs(args.legs, args.snapshot_dir, rules, workers=args.workers)

    summary_lines = []
    coverage_rows = [(leg, coverage[leg][0], coverage[leg][1]) for leg in all_results]

    for leg in args.legs:
        if leg not in all_results:
            continue
        print(f"  {leg}")
        results = all_results[leg]
        table = print_table(leg, results)
        print(table)
        summary_lines.append(table)

        best, best_m = best_rule(results)
        print(f"  -> best exit rule: {best}  PnL={best_m['total_pnl']:.2f}  maxDD={best_m['max_dd']:.2f}  trades={best_m['trades']}")

    # Write markdown report.
    report_path = REPORT_DIR / "trend_family_exit_report.md"
    with open(report_path, "w") as fh:
        fh.write("# Trend-Family Early-Exit Simulation Report (Real Polybacktest Snapshots)\n\n")
        fh.write("Method: post-process hold-to-expiry trades using real Polymarket BTC 5m order-book\n")
        fh.write("snapshots from `gdrive:polybacktest_60d/polymarket/btc/5m/`.\n\n")
        fh.write("Mark price: order-book mid `(best_bid + best_ask) / 2` for the traded side;\n")
        fh.write("fallback to `price_up` (YES) or `price_down` (NO) when the book is empty.\n")
        fh.write("Missing exact timestamps are linearly interpolated between nearest snapshots.\n\n")
        fh.write("Snapshot coverage:\n\n")
        fh.write("| leg | total_trades | trades_with_snapshots | coverage |\n")
        fh.write("|-----|--------------|-----------------------|----------|\n")
        for leg, n_total, n_with_snap in coverage_rows:
            pct = n_with_snap / n_total * 100 if n_total else 0.0
            fh.write(f"| {leg} | {n_total} | {n_with_snap} | {pct:.1f}% |\n")
        fh.write("\n")
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

    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        with open(args.output_json, "w") as fh:
            json.dump({
                "legs": list(all_results.keys()),
                "results": all_results,
                "coverage": {leg: list(cov) for leg, cov in coverage.items()},
            }, fh, indent=2)
        print(f"Raw results written to {args.output_json}")

    print(f"\nReport written to {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
