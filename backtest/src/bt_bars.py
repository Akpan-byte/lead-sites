# CHANGE_SUMMARY
# 2026-07-11  kimi
#   - Continuous-bars harness for the 4 REST-indicator strats (rsi2_connors,
#     bbkc_squeeze, five_min_trend_breakthrough, ou_zscore_mr). In live these call
#     pre_populate_bars(asset, tf_min) over REST to seed a 200+ bar history per
#     (asset, strike); offline that returns [] and they never signal. We monkey-
#     patch pre_populate_bars at runtime to serve CAUSAL Binance bars resampled
#     from the recorded 1m feed (bt_reference.resample_upto) up to each market's
#     open time. Live signal/Portfolio logic is untouched; only the bar source
#     and the clock are swapped. Each market bootstraps from its own open time
#     (state is keyed by strike), so per-market replay is faithful.
# WHY: make the last un-backtestable family runnable with no look-ahead.
"""Continuous-bars backtest for REST-indicator strats. Sandbox-only."""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import driver
import bt_reference
from engine.portfolio import Portfolio

ET = ZoneInfo("America/New_York")

# module -> bootstrap shape expected from pre_populate_bars
_INDICATOR_SHAPE = {
    "phase_2.rsi2_connors": "closes",
    "phase_2.ou_zscore_mr": "closes",
    "phase_2.bbkc_squeeze": "ohlc",
    "phase_2.five_min_trend_breakthrough": "ohlc",
}
BOOTSTRAP_LIMIT = 400  # >= every strat's internal cap/need (rsi2 caps 400)


def is_indicator(module: str) -> bool:
    return module in _INDICATOR_SHAPE


def _load_module_by_path(module: str):
    """Load a phase_2 signal.py by file path (bypass the eager package __init__)."""
    driver._install_requests_stub()
    parts = module.split(".")
    base = os.path.join(driver.SRC, "signals", *parts, "signal.py")
    if not os.path.isfile(base):
        raise ImportError(f"no signal.py for {module}: {base}")
    spec = importlib.util.spec_from_file_location("_bt_ind_" + module.replace(".", "_"), base)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _patch_bootstrap(mod, module: str, clock_holder: list) -> None:
    """Replace mod.pre_populate_bars with a causal Binance-1m-resampled version.

    clock_holder[0] is the current market's open time in epoch ms (set by the
    driver before each market). Bars returned are complete tf candles strictly
    before that time -> no look-ahead."""
    shape = _INDICATOR_SHAPE[module]

    def _bt_pre_populate(asset="BTC", timeframe_min=5):
        tf = int(timeframe_min) if timeframe_min else 5
        bars = bt_reference.resample_upto(asset.upper(), clock_holder[0], tf, BOOTSTRAP_LIMIT)
        if shape == "closes":
            return [c for (_o, _h, _l, c) in bars]
        return [{"high": h, "low": lo, "close": c} for (_o, h, lo, c) in bars]

    mod.pre_populate_bars = _bt_pre_populate


def run_indicator(reg_entry: dict, files: list[str], oos_start: "date | None" = None,
                  compact: bool = False, fill: str = "maker") -> dict:
    """Replay one REST-indicator strategy over all IS markets with one persistent
    $200 wallet. Each market bootstraps its bar history from Binance at its own
    open time (causal). Returns aggregate trades + equity (same shape as
    bt_orb.run_daily_orb). compact=True reads pkl.gz arrays via
    driver.run_market_maker_arr (identical semantics to the dict path)."""
    bt_reference.load()
    module = reg_entry["module"]
    mod = _load_module_by_path(module)
    fn = getattr(mod, reg_entry["fn"])
    clock_holder = [0]
    _patch_bootstrap(mod, module, clock_holder)
    pf = Portfolio(name=f"btind:{module}", capital=driver.CAPITAL)
    n_markets = 0; n_triggered = 0; t0 = time.time()
    for f in files:
        if compact:
            data = driver.load_compact_file(f)
            if not data or not data.get("t"):
                continue
            t_first_ms = data["t"][0]
        else:
            data = driver.load_market_file(f)
            if not data:
                continue
            t_first_ms = int(driver._parse_ts(data[0]["time"]).timestamp() * 1000)
        d = datetime.fromtimestamp(t_first_ms / 1000.0, tz=ZoneInfo("UTC")).astimezone(ET).date()
        if oos_start is not None and d >= oos_start:
            del data
            continue
        # bootstrap clock = this market's open (first tick calls pre_populate_bars)
        clock_holder[0] = int(t_first_ms)
        n_markets += 1
        if compact and fill == "taker":
            r = driver.run_market_taker_arr(data, reg_entry, fn, pf=pf)
        elif compact and fill == "instant":
            r = driver.run_market_instant_arr(data, reg_entry, fn, pf=pf)
        elif compact:
            r = driver.run_market_maker_arr(data, reg_entry, fn, pf=pf)
        else:
            r = driver.run_market_maker(data, reg_entry, fn, pf=pf)
        n_triggered += r.get("n_triggered", 0)
        del data
    closed = pf.closed_trades
    total_pnl = sum(t.pnl for t in closed)
    committed = sum(t.entry_notional for t in pf.active_trades.values())
    return {"trades": [t.to_dict() for t in closed], "n_closed": len(closed),
            "n_active_left": len(pf.active_trades), "n_triggered": n_triggered,
            "n_markets": n_markets, "cash": round(pf.cash, 4),
            "committed": round(committed, 4),
            "total_pnl": round(total_pnl, 4),
            "equity": round(pf.cash + committed, 4),
            "runtime_s": round(time.time() - t0, 1)}


if __name__ == "__main__":
    import glob
    name = sys.argv[1] if len(sys.argv) > 1 else "phase_2.rsi2_connors"
    nfiles = int(sys.argv[2]) if len(sys.argv) > 2 else 400
    files = sorted(glob.glob("/tmp/btc5m_all/*.json.gz"))[:nfiles]
    reg = driver.STRATEGIES[name]
    out = run_indicator(reg, files, oos_start=date(2026, 7, 1))
    print({k: v for k, v in out.items() if k != "trades"})
