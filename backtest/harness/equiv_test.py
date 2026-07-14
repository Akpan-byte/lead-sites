# CHANGE_SUMMARY
# 2026-07-11  kimi
#   - Created harness/equiv_test.py: proves the replay driver is behaviorally
#     identical to the real generated runner (AssetRunner) by feeding both the
#     same snapshots through a BacktestFeed and diffing every trade.
# WHY: The backtest is only credible if the feed-swap reproduces the live code
#      path exactly. Any divergence in entries/exits/fills shows up here.
"""Equivalence test: real AssetRunner (feed-swapped) vs harness driver."""

import glob
import sys

sys.path.insert(0, "/config/backtest")
sys.path.insert(0, "/config/backtest_repo")

from harness import btclock
btclock.install()
clock = btclock.Clock()

# Patch the Redis-backed pool BEFORE importing the runner so it binds our mirror.
import engine.capital
from harness.pool import InMemoryPool
engine.capital.GlobalCapitalPool = InMemoryPool

from harness.feed import BacktestFeed, normalize_snapshot_book
from harness.markets import load_market_file
from harness.refdata import BinanceReference
from harness.driver import StrategyContext, replay_day, TICK_SECONDS

STRATEGY_RUNNER = "runners.run_phase_2_pm_orb_v4_reentry"
STRATEGY_NAME = "phase_2.pm_orb_v4_reentry"
N_MARKETS = 40


class CaptureLogger:
    """Stand-in for StrategyLogger: captures trades, no-ops the rest."""

    _state_path = "/dev/null"

    def __init__(self):
        self.trades = []

    def log_trade(self, d):
        self.trades.append(d)

    def log_event(self, *a, **k):
        pass

    def write_state(self, *a, **k):
        pass

    def write_perf(self, *a, **k):
        pass


class CaptureRecorder:
    def __init__(self):
        self.closed = []
        self.opened = []

    def on_trade_closed(self, ctx, t):
        self.closed.append(t)

    def on_trade_opened(self, ctx, t):
        self.opened.append(t)

    def on_event(self, *a):
        pass

    def flush(self):
        pass


def main():
    import importlib
    from engine.strategy_registry import STRATEGIES

    ref = BinanceReference()
    feed = BacktestFeed(ref, clock)

    # --- A) real runner, feed-swapped ---------------------------------------
    R = importlib.import_module(STRATEGY_RUNNER)
    # BOOK_WAIT re-poll uses time.sleep; advance the fake clock + feed on sleep.
    real_market_holder = {"m": None}

    def fake_sleep(seconds):
        clock.set(clock() + seconds)
        m = real_market_holder["m"]
        if m is not None:
            snap = m.advance(clock())
            feed.set_book(m.market["token_id_yes"], normalize_snapshot_book(snap.get("orderbook_up")) if snap else None)
            feed.set_book(m.market["token_id_no"], normalize_snapshot_book(snap.get("orderbook_down")) if snap else None)

    R.time.sleep = fake_sleep
    entry = STRATEGIES[STRATEGY_NAME]
    runner = R.AssetRunner("BTC", entry)
    runner.logger = CaptureLogger()  # capture trades instead of disk
    runner.global_pool.reconcile(200.0, 0.0)

    paths = sorted(glob.glob("/config/bt_data/5m/*.json.gz"))[3000:3000 + N_MARKETS]
    markets = [load_market_file(p, ref) for p in paths]
    markets = [m for m in markets if m]
    for m in markets:
        m.reset()

    t_ms = int(min(m.window_start for m in markets)) * 1000
    end_ms = int(max(m.window_end for m in markets)) * 1000
    step = int(TICK_SECONDS * 1000)
    while t_ms <= end_ms:
        t = t_ms / 1000.0
        clock.set(t)
        active = [m for m in markets if m.window_start <= t <= m.window_end]
        runner.markets = [m.market for m in active]
        real_market_holder["m"] = active[0] if active else None
        for m in active:
            snap = m.advance(t)
            feed.set_book(m.market["token_id_yes"], normalize_snapshot_book(snap.get("orderbook_up")) if snap else None)
            feed.set_book(m.market["token_id_no"], normalize_snapshot_book(snap.get("orderbook_down")) if snap else None)
        runner.update_spot(feed)
        runner.update_oracle(feed)
        runner.process_markets(feed)
        t_ms += step

    real_trades = [t for t in runner.logger.trades if t.get("exit_price") is not None]
    print(f"real runner: {len(real_trades)} closed trades, "
          f"pnl={sum(t['pnl'] for t in real_trades):+.4f}")

    # --- B) harness driver ----------------------------------------------------
    feed2 = BacktestFeed(ref, clock)
    mod = importlib.import_module(f"signals.{entry['module']}")
    fn = getattr(mod, entry["fn"])
    rec = CaptureRecorder()
    ctx = StrategyContext(STRATEGY_NAME, entry, fn, mod, feed2, clock, rec)
    for m in markets:
        m.reset()
    replay_day([ctx], markets, ref, clock, feed2)
    print(f"harness:     {len(rec.closed)} closed trades, "
          f"pnl={sum(t.pnl for t in rec.closed):+.4f}")

    # --- diff -----------------------------------------------------------------
    def key_real(t):
        return (round(t["opened_at"], 1), t["direction"], round(t["entry_price"], 4),
                round(t["shares"], 2), round(t["exit_price"], 4), round(t["pnl"], 4))

    def key_bt(t):
        return (round(t.opened_at, 1), t.direction, round(t.entry_price, 4),
                round(t.shares, 2), round(t.exit_price, 4), round(t.pnl, 4))

    a = sorted(key_real(t) for t in real_trades)
    b = sorted(key_bt(t) for t in rec.closed)
    if a == b:
        print(f"EQUIVALENT: {len(a)} trades identical (ts to 0.1s, dir, px, shares, exit, pnl)")
        return 0
    print(f"DIVERGENCE: real={len(a)} harness={len(b)}")
    for i in range(max(len(a), len(b))):
        ra = a[i] if i < len(a) else None
        rb = b[i] if i < len(b) else None
        if ra != rb:
            print(f"  [{i}] real={ra}\n       bt  ={rb}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
