#!/usr/bin/env python3
# Scenario replay on the REAL taker-fill trade logs (results/is_taker/*.trades.jsonl.gz).
# Same entries, exits, timestamps, and proportional fees as the recorded backtest trades;
# only position sizing or direction changes per scenario. No simulated prices.
import json, gzip, math
from datetime import datetime, timezone

STRATS = ["btc_orb_5m_5re", "btc_orb_3m_5re"]

def load(s):
    tr = []
    with gzip.open(f"results/is_taker/phase_2.{s}.trades.jsonl.gz") as f:
        for line in f:
            tr.append(json.loads(line))
    tr.sort(key=lambda t: t["closed_at"])
    return tr

def maxdd(eqs):
    peak, dd = eqs[0], 0.0
    for e in eqs:
        peak = max(peak, e)
        dd = max(dd, (peak - e) / peak)
    return dd * 100

def replay(trades, start, mode="base", invert=False, switch=None):
    """switch=None: all trades one direction. switch='perfect': per-day pick better of orig/inverse (look-ahead upper bound)."""
    eq, eqs, m, locked = start, [start], 1.0, 0.0
    for t in trades:
        ep = (1 - t["entry_price"]) if invert else t["entry_price"]
        base = max(5.0, 0.005 * eq / ep)         # 0.5% risk, 5-contract minimum (matches engine/execution.py)
        if mode == "half_after_loss":
            shares = max(5.0, base * m)
        elif mode == "lock":
            shares = max(base, locked)
        else:
            shares = base
        per_share = t["pnl"] / t["shares"]        # recorded per-contract pnl incl. fees
        if invert:
            per_share = -per_share
        pnl = shares * per_share
        eq = max(0.0, eq + pnl)
        eqs.append(eq)
        if pnl > 0:
            locked = max(locked, shares)          # ratchet locks only after wins
            m = 1.0
        else:
            m *= 0.5
    return eqs

def daily_pnl(trades, invert=False):
    d = {}
    for t in trades:
        day = datetime.fromtimestamp(t["closed_at"], timezone.utc).date().isoformat()
        d[day] = d.get(day, 0.0) + (-t["pnl"] if invert else t["pnl"])
    return d

for s in STRATS:
    tr = load(s)
    print(f"\n################ {s} — {len(tr)} real taker trades ################")
    for start in (200, 2000, 10000):
        for mode in ("base", "half_after_loss", "lock"):
            eqs = replay(tr, start, mode)
            print(f"  start=${start:>6} mode={mode:<15} final=${eqs[-1]:>10.2f}  pnl=${eqs[-1]-start:>+9.2f}  maxDD={maxdd(eqs):>6.2f}%")
    # inverse always-on
    for start in (200, 2000):
        eqs = replay(tr, start, "base", invert=True)
        print(f"  start=${start:>6} INVERSE always-on    final=${eqs[-1]:>10.2f}  pnl=${eqs[-1]-start:>+9.2f}  maxDD={maxdd(eqs):>6.2f}%")
    # regime analysis: original vs inverse daily
    do, di = daily_pnl(tr, False), daily_pnl(tr, True)
    days = sorted(do)
    import statistics as st
    ov = [do[d] for d in days]; iv = [di[d] for d in days]
    corr = st.correlation(ov, iv) if len(days) > 2 else float("nan")
    orig_losing_days = [d for d in days if do[d] < 0]
    inv_on_orig_losing = sum(di[d] for d in orig_losing_days)
    orig_on_losing = sum(do[d] for d in orig_losing_days)
    print(f"  regime: {len(days)} days, {len(orig_losing_days)} losing days for original")
    print(f"    original PnL on its losing days: ${orig_on_losing:+.2f} | inverse PnL on those SAME days: ${inv_on_orig_losing:+.2f}")
    print(f"    daily PnL correlation original vs inverse: {corr:.3f}")
    # perfect-switch upper bound (look-ahead): each day take the better side, 1% sizing approx via scaled daily pnl
    combo = sum(max(do[d], di[d]) for d in days)
    orig_total = sum(ov); inv_total = sum(iv)
    print(f"    totals @ recorded sizing: original ${orig_total:+.2f} | inverse ${inv_total:+.2f} | perfect daily switch (look-ahead upper bound) ${combo:+.2f}")
