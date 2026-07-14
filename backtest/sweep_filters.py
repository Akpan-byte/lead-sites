#!/usr/bin/env python3
# Regime-filter sweep for the ORB breakout<->fade switch.
# Replays REAL IS taker trades (results/is_taker, May 8 - Jun 30 only).
# Filters decide per trade: BREAKOUT (as recorded), FADE (mirror), or SKIP.
# Family A: past-trade stats (rolling WR, rolling PnL, loss streaks).
# Family B: BTC 1m market data at trade time (ATR/RV/BB-width/momentum/range percentiles, trailing window only).
# No look-ahead: every filter input is strictly before the trade's opened_at.
import json, gzip, math, zipfile, io, csv, bisect, statistics
from datetime import datetime, timezone

CAP_START = 200.0
RISK = 0.005
MINC = 5.0
IS_END = datetime(2026, 7, 2, tzinfo=timezone.utc).timestamp()  # IS window ends ~Jul 1

# ---------- load BTC 1m reference bars (Binance kline zips) ----------
bars = []
import glob
for zp in sorted(glob.glob("/tmp/ref_btc_1m/BTCUSDT-1m-*.zip")):
    with zipfile.ZipFile(zp) as z:
        for name in z.namelist():
            with z.open(name) as f:
                for row in csv.reader(io.TextIOWrapper(f)):
                    ts = int(row[0]) / 1_000_000.0  # Binance kline open_time is microseconds here
                    if ts > IS_END:
                        continue
                    bars.append((ts, float(row[2]), float(row[3]), float(row[4])))  # ts, high, low, close
bars.sort()
BTS = [b[0] for b in bars]
print(f"ref bars: {len(bars)} ({datetime.fromtimestamp(bars[0][0],timezone.utc).date()} -> {datetime.fromtimestamp(bars[-1][0],timezone.utc).date()})")

# ---------- per-bar indicators ----------
N = len(bars)
def atr(i, n=14):
    if i < n: return None
    trs = []
    for j in range(i - n + 1, i + 1):
        h, l, pc = bars[j][1], bars[j][2], bars[j - 1][3]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / n

def rv(i, n):
    if i < n: return None
    rets = [bars[j][3] / bars[j - 1][3] - 1 for j in range(i - n + 1, i + 1)]
    return statistics.pstdev(rets) * 100  # pct

def bbw(i, n=20, k=2):
    if i < n: return None
    closes = [bars[j][3] for j in range(i - n + 1, i + 1)]
    mu = sum(closes) / n
    sd = statistics.pstdev(closes)
    return (2 * k * sd) / mu * 100 if mu else None  # width % of price

def mom(i, n):
    if i < n: return None
    return (bars[i][3] / bars[i - n][3] - 1) * 100  # pct

def rng1h(i):
    if i < 60: return None
    hi = max(bars[j][1] for j in range(i - 59, i + 1))
    lo = min(bars[j][2] for j in range(i - 59, i + 1))
    return (hi - lo) / bars[i][3] * 100

IND = {"atr14": [], "rv30": [], "rv60": [], "bbw20": [], "mom15": [], "mom30": [], "mom60": [], "rng1h": []}
for i in range(N):
    IND["atr14"].append(atr(i))
    IND["rv30"].append(rv(i, 30))
    IND["rv60"].append(rv(i, 60))
    IND["bbw20"].append(bbw(i))
    IND["mom15"].append(mom(i, 15))
    IND["mom30"].append(mom(i, 30))
    IND["mom60"].append(mom(i, 60))
    IND["rng1h"].append(rng1h(i))

WIN = 10080  # trailing 7d percentile window

def pctile_at(i, key):
    """percentile rank of indicator at bar i within the trailing WIN bars (no look-ahead)."""
    v = IND[key][i]
    if v is None: return None
    lo = max(0, i - WIN)
    vals = [x for x in IND[key][lo:i + 1] if x is not None]
    if len(vals) < 50: return None
    return bisect.bisect_left(sorted(vals), v) / len(vals) * 100

# ---------- load trades ----------
def load_trades(s):
    tr = []
    with gzip.open(f"results/is_taker/phase_2.{s}.trades.jsonl.gz") as f:
        for line in f:
            tr.append(json.loads(line))
    tr.sort(key=lambda t: t["opened_at"])
    return tr

# ---------- feature snapshot per trade (computed once) ----------
def trade_features(trades):
    feats = []
    for t in trades:
        i = bisect.bisect_left(BTS, t["opened_at"]) - 1
        f = {"bar": i}
        for k in IND:
            f[k] = IND[k][i] if i >= 0 else None
        # percentiles (trailing window)
        for k in ("atr14", "rv30", "rv60", "bbw20", "rng1h"):
            f[k + "_pct"] = pctile_at(i, k) if i >= 0 else None
        feats.append(f)
    return feats

# ---------- replay ----------
def replay(trades, decide):
    eq, peak, dd = CAP_START, CAP_START, 0.0
    n_taken = n_fade = 0
    m = 1.0; locked = 0.0
    for t, act in zip(trades, decide):
        if act == "skip":
            continue
        fade = act == "fade"
        ep = (1 - t["entry_price"]) if fade else t["entry_price"]
        shares = max(MINC, RISK * eq / ep)
        ps = t["pnl"] / t["shares"]
        if fade: ps = -ps
        eq = max(0.0, eq + shares * ps)
        peak = max(peak, eq); dd = max(dd, peak - eq)
        n_taken += 1; n_fade += fade
    return eq, dd, n_taken, n_fade

# ---------- filter builders ----------

# (Family A implemented inline below for clarity)

def run_suite(name, trades):
    feats = trade_features(trades)
    results = []

    def eval_decide(label, dec):
        eq, dd, nt, nf = replay(trades, dec)
        results.append((label, eq - CAP_START, dd, nt, nf))

    # baselines
    eval_decide("BASE always-breakout", ["brk"] * len(trades))
    eval_decide("always-fade", ["fade"] * len(trades))

    # ---- Family A: past-trade filters ----
    closed_so_far = []
    def famA(kind, N_, thr, action, cool_m=0):
        dec = []
        closed = []
        streak = 0
        cooldown = 0
        for t in trades:
            closed = [c for c in closed if c[0] < t["opened_at"]]
            act = "brk"
            if cooldown > 0:
                act = action; cooldown -= 1
            elif kind == "wr" and len(closed) >= N_:
                wr = sum(1 for _, p in closed[-N_:] if p > 0) / N_
                if wr < thr: act = action
            elif kind == "pnl" and len(closed) >= N_:
                if sum(p for _, p in closed[-N_:]) < thr: act = action
            elif kind == "streak":
                if streak >= N_:
                    act = action
                    if cool_m: cooldown = cool_m - 1
            dec.append(act)
            # update state with THIS trade's recorded outcome (known only after close; used for later trades)
            closed.append((t["closed_at"], t["pnl"]))
            streak = 0 if t["pnl"] > 0 else streak + 1
        return dec

    for action in ("fade", "skip"):
        for N_ in (10, 20, 30):
            for thr in (0.50, 0.55, 0.60, 0.65):
                eval_decide(f"A_wr{N_}<{thr}->{action}", famA("wr", N_, thr, action))
        for N_ in (10, 20):
            eval_decide(f"A_pnl{N_}<0->{action}", famA("pnl", N_, 0.0, action))
        for K in (2, 3, 4):
            for M in (0, 2, 4):
                eval_decide(f"A_streak{K}cool{M}->{action}", famA("streak", K, None, action, M))

    # ---- Family B: market-data percentile filters ----
    # low-vol/compression -> fade (chop); high -> breakout
    for action in ("fade", "skip"):
        for key in ("atr14_pct", "rv30_pct", "rv60_pct", "bbw20_pct", "rng1h_pct"):
            for thr in (20, 30, 40, 50):
                def dec(key=key, thr=thr, action=action):
                    out = []
                    for f in feats:
                        v = f[key]
                        if v is None: out.append("brk")
                        elif v < thr: out.append(action)
                        else: out.append("brk")
                    return out
                eval_decide(f"B_{key}<{thr}->{action}", dec())
        # momentum: |mom| small -> fade, large -> breakout
        for key in ("mom15", "mom30", "mom60"):
            for thr in (0.05, 0.10, 0.20, 0.30):
                def dec(key=key, thr=thr, action=action):
                    out = []
                    for f in feats:
                        v = f[key]
                        if v is None: out.append("brk")
                        elif abs(v) < thr: out.append(action)
                        else: out.append("brk")
                    return out
                eval_decide(f"B_|{key}|<{thr}->{action}", dec())
        # high-vol -> fade (exhaustion hypothesis), opposite direction
        for key in ("rv30_pct", "atr14_pct"):
            for thr in (70, 80, 90):
                def dec(key=key, thr=thr, action=action):
                    out = []
                    for f in feats:
                        v = f[key]
                        if v is None: out.append("brk")
                        elif v > thr: out.append(action)
                        else: out.append("brk")
                    return out
                eval_decide(f"B_{key}>{thr}->{action}", dec())

    results.sort(key=lambda r: -r[1])
    print(f"\n================ {name}: {len(results)} configs, top 20 by IS pnl ================")
    print(f"{'config':34} {'pnl':>9} {'maxDD$':>8} {'trades':>7} {'fades':>6}")
    for label, pnl, dd, nt, nf in results[:20]:
        print(f"{label:34} {pnl:>+9.2f} {dd:>8.2f} {nt:>7} {nf:>6}")
    print("--- worst 3 ---")
    for label, pnl, dd, nt, nf in results[-3:]:
        print(f"{label:34} {pnl:>+9.2f} {dd:>8.2f} {nt:>7} {nf:>6}")
    return results

for s in ("btc_orb_5m_5re", "btc_orb_3m_5re"):
    tr = load_trades(s)
    print(f"\n##### {s}: {len(tr)} IS trades #####")
    run_suite(s, tr)
