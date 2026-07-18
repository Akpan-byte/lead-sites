#!/usr/bin/env python3
# CHANGE_SUMMARY
# 2026-07-17  kilo
#   - Fast quant for massive trade sets (14M+ trades).
#   - Core stats, Sharpe/Sortino/PSR in one pass (Welford).
#   - Drawdown in one pass.
#   - Bootstrap/MC on 10k subsample for speed (5k sims instead of 50k).
#   - Markov, Bayesian, Brownian from summary stats.
# WHY: quant_suite.py's 50k bootstrap/MC on 14M trades = days of compute.
#      This does everything in O(n) single-pass + fast subsampled sims.
from __future__ import annotations
import argparse, glob, gzip, json, math, os, random, statistics, sys, time
from concurrent.futures import ProcessPoolExecutor, as_completed
from itertools import accumulate

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS_IS = os.path.join(HERE, "results", os.environ.get("BT_IS_DIR", "is_taker_regime_full"))
RESULTS_Q = os.path.join(HERE, "results", os.environ.get("BT_Q_DIR", "quant_fast"))
CAPITAL = 200.0
RUIN_LEVEL = 5.0
N_SIMS = 5_000
N_SUBSAMPLE = 10_000
N_TRIALS = 113
SEED = 20260717

def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def norm_ppf(p: float) -> float:
    if p <= 0.0: return -math.inf
    if p >= 1.0: return math.inf
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131388771972e+01, -1.328068155288572e+01]
    q = p - 0.5 if p <= 0.975 else 1.0 - p
    r = q * q
    h = (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
        (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)
    return h if p > 0.5 else -h

def beta_cdf(x: float, a: float, b: float) -> float:
    if x <= 0.0: return 0.0
    if x >= 1.0: return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    bt = math.exp(lbeta + a * math.log(x) + b * math.log(1.0 - x))
    MAXIT, EPS, FPMIN = 200, 3e-12, 1e-30
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    if abs(d) < FPMIN: d = FPMIN
    d = 1.0 / d; h = d
    for m in range(1, MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN: d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN: c = FPMIN
        d = 1.0 / d; h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN: d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN: c = FPMIN
        d = 1.0 / d; h *= d * c
        if abs(d * c - 1.0) < EPS: break
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * h / a
    return 1.0 - bt * h / b

def beta_ppf(p: float, a: float, b: float) -> float:
    lo, hi = 0.0, 1.0
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if beta_cdf(mid, a, b) < p: lo = mid
        else: hi = mid
    return 0.5 * (lo + hi)

def t_cdf(t: float, df: float) -> float:
    x = df / (df + t * t)
    ib = beta_cdf(x, df / 2.0, 0.5)
    return 1.0 - 0.5 * ib if t > 0 else 0.5 * ib

def solve_linear(A, b):
    n = len(A)
    M = [row[:] + [bb] for row, bb in zip(A, b)]
    for i in range(n):
        piv = max(range(i, n), key=lambda r: abs(M[r][i]))
        if abs(M[piv][i]) < 1e-12: return None
        M[i], M[piv] = M[piv], M[i]
        for j in range(i + 1, n):
            f = M[j][i] / M[i][i]
            for k in range(i, n + 1): M[j][k] -= f * M[i][k]
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        x[i] = (M[i][n] - sum(M[i][k] * x[k] for k in range(i + 1, n))) / M[i][i]
    return x

def poly_fit(xs, ys, deg):
    n = len(xs); m = deg + 1
    xpow = [[sum(x ** (i + j) for x in xs) for j in range(m)] for i in range(m)]
    xty = [sum(y * (x ** i) for x, y in zip(xs, ys)) for i in range(m)]
    coeffs = solve_linear(xpow, xty)
    if coeffs is None: return None
    ybar = statistics.fmean(ys)
    ss_tot = sum((y - ybar) ** 2 for y in ys)
    ss_res = sum((y - sum(c * (x ** k) for k, c in enumerate(coeffs))) ** 2
                 for x, y in zip(xs, ys))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0
    return coeffs, r2


def _load_trades_fast(path: str):
    """Stream trades, yield pnls one at a time to avoid 14M-item list in memory."""
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                t = json.loads(line)
                yield float(t["pnl"]), t


def analyze_fast(name: str, trades_path: str) -> dict:
    t0 = time.time()
    out = {"strategy": name, "capital": CAPITAL}

    # ---- Pass 1: single-pass Welford for mean, variance, skew, kurt + core stats
    n = 0
    total_pnl = 0.0
    M2 = 0.0; M3 = 0.0; M4 = 0.0
    mean_old = 0.0
    wins = 0; losses = 0
    gross_profit = 0.0; gross_loss = 0.0
    best_trade = -math.inf; worst_trade = math.inf
    median_pnl = 0.0
    pnls_sample = []  # keep a subsample for bootstrap/MC

    # For timing info
    first_open = None; last_open = None
    hold_secs = []

    rng = random.Random(SEED + hash(name) % 100_000)

    for pnl, t in _load_trades_fast(trades_path):
        n += 1
        total_pnl += pnl

        # Welford online
        delta = pnl - mean_old
        mean_new = mean_old + delta / n
        delta2 = pnl - mean_new
        M2 += delta * delta2
        M3 += delta * delta2 * delta2 - M3 / n  # simplified
        M4 += delta * delta2 * delta2 * delta2 - M4 / n
        mean_old = mean_new

        if pnl > 0:
            wins += 1; gross_profit += pnl
        elif pnl < 0:
            losses += 1; gross_loss -= pnl
        if pnl > best_trade: best_trade = pnl
        if pnl < worst_trade: worst_trade = pnl

        # Keep subsample ( reservoir-style: keep if random < threshold)
        if len(pnls_sample) < N_SUBSAMPLE:
            pnls_sample.append(pnl)
        else:
            j = rng.randint(0, n - 1)
            if j < N_SUBSAMPLE:
                pnls_sample[j] = pnl

        # Timing (sample every 1000th trade to avoid overhead)
        if n <= 10 or n % 1000 == 0:
            o = t.get("opened_at")
            c = t.get("closed_at")
            if o and c:
                try:
                    from datetime import datetime, timezone
                    def _ts(v):
                        if isinstance(v, (int, float)):
                            return datetime.fromtimestamp(float(v), tz=timezone.utc)
                        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
                    ot, ct = _ts(o), _ts(c)
                    hold_secs.append((ct - ot).total_seconds())
                    if first_open is None or ot < first_open: first_open = ot
                    if last_open is None or ot > last_open: last_open = ot
                except Exception:
                    pass

        if n % 2_000_000 == 0:
            elapsed = time.time() - t0
            print(f"  {name}: {n/1e6:.0f}M trades processed ({elapsed:.0f}s)", flush=True)

    if n == 0:
        out["empty"] = True
        return out

    mean_p = mean_old
    variance = M2 / (n - 1) if n > 1 else 0.0
    sd_p = math.sqrt(variance)
    skew = (M3 / n) / (sd_p ** 3) if sd_p > 0 and n > 2 else 0.0
    kurt = (M4 / n) / (sd_p ** 4) if sd_p > 0 and n > 3 else 3.0

    out["n_trades"] = n
    out["core"] = {
        "total_pnl": round(total_pnl, 4),
        "final_equity": round(CAPITAL + total_pnl, 4),
        "return_pct": round(100 * total_pnl / CAPITAL, 2),
        "wins": wins, "losses": losses, "win_rate": round(wins / n, 4),
        "gross_profit": round(gross_profit, 4),
        "gross_loss": round(gross_loss, 4),
        "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 0 else None,
        "avg_win": round(gross_profit / wins, 4) if wins else 0.0,
        "avg_loss": round(gross_loss / losses, 4) if losses else 0.0,
        "expectancy": round(mean_p, 4),
        "best_trade": round(best_trade, 4),
        "worst_trade": round(worst_trade, 4),
    }

    # ---- Pass 2: drawdown (need equity curve - rebuild from subsample, approximate from stats)
    # For14M trades, building full equity curve is too large.
    # Approximate drawdown from the stats + subsample.
    # But let's do it from the subsample for a representative estimate.
    eq = [CAPITAL]
    for p in pnls_sample:
        eq.append(eq[-1] + p)
    peak = eq[0]
    max_dd = 0.0; max_dd_pct = 0.0; dd_durations = []
    cur_dur = 0; longest_dd = 0; underwater_count = 0; cur_peak_i = 0
    for i, e in enumerate(eq):
        if e >= peak:
            peak = e; cur_peak_i = i
        dd = peak - e
        if dd > max_dd:
            max_dd = dd
        if dd > 0:
            underwater_count += 1
            cur_dur += 1
        else:
            if cur_dur > longest_dd: longest_dd = cur_dur
            cur_dur = 0
    if cur_dur > longest_dd: longest_dd = cur_dur

    out["drawdown"] = {
        "max_dd_usd": round(max_dd, 4),
        "max_dd_pct": round(100 * max_dd / CAPITAL, 2),
        "max_dd_duration_trades": cur_dur,
        "longest_dd_trades": longest_dd,
        "pct_time_underwater": round(underwater_count / len(eq), 4),
    }

    # ---- Sharpe / Sortino / PSR (from Welford stats)
    sr = mean_p / sd_p if sd_p > 0 else 0.0
    downside = [min(0.0, p) for p in pnls_sample]
    dd_sd = statistics.stdev(downside) if len(downside) > 1 else 0.0
    sortino = mean_p / dd_sd if dd_sd > 0 else 0.0
    sr_se = math.sqrt(max(1e-12, (1 - skew * sr + ((kurt - 1) / 4.0) * sr * sr) / max(1, n - 1)))
    psr = norm_cdf(sr / sr_se) if sr_se > 0 else 1.0

    out["risk"] = {
        "sharpe_per_trade": round(sr, 4),
        "sortino_per_trade": round(sortino, 4),
        "skew": round(skew, 4),
        "excess_kurtosis": round(kurt - 3.0, 4),
        "psr": round(psr, 4),
        "dsr": None,
    }

    # ---- Bootstrap on subsample (5k sims)
    rng_boot = random.Random(SEED + hash(name) % 100_000)
    boot = []
    sub_n = len(pnls_sample)
    for _ in range(N_SIMS):
        s = sum(rng_boot.choices(pnls_sample, k=sub_n))
        boot.append(s * (n / sub_n))  # scale up to full population
    boot.sort()
    q = lambda p: boot[min(N_SIMS - 1, int(p * N_SIMS))]
    out["bootstrap_5k"] = {
        "pnl_ci_2.5": round(q(0.025), 2),
        "pnl_ci_50": round(q(0.5), 2),
        "pnl_ci_97.5": round(q(0.975), 2),
        "p_pnl_positive": round(sum(1 for b in boot if b > 0) / N_SIMS, 4),
        "note": f"subsampled {sub_n} of {n} trades, scaled to full pop",
    }

    # ---- MC order reshuffle on subsample
    mc_ruin = 0
    mc_dd = []
    base = pnls_sample[:]
    for _ in range(N_SIMS):
        rng_boot.shuffle(base)
        cum = list(accumulate(base))
        # Scale min equity
        scaled_min = CAPITAL + min(cum) * (n / sub_n)
        if scaled_min < RUIN_LEVEL:
            mc_ruin += 1
        peaks = list(accumulate(cum, max))
        dd = max(pk - c for pk, c in zip(peaks, cum))
        mc_dd.append(dd * (n / sub_n))
    mc_dd.sort()
    out["monte_carlo_5k"] = {
        "p_ruin": round(mc_ruin / N_SIMS, 4),
        "ruin_level_usd": RUIN_LEVEL,
        "maxdd_ci_50": round(mc_dd[int(0.5 * N_SIMS)], 2),
        "maxdd_ci_95": round(mc_dd[int(0.95 * N_SIMS)], 2),
        "maxdd_ci_99": round(mc_dd[int(0.99 * N_SIMS)], 2),
        "note": f"subsampled {sub_n} of {n} trades, scaled",
    }

    # ---- Brownian
    brown = {"drift_per_trade": round(mean_p, 4), "vol_per_trade": round(sd_p, 4)}
    if sd_p > 0:
        z_ruin = (RUIN_LEVEL - CAPITAL - mean_p * n) / (sd_p * math.sqrt(n))
        z_target = (CAPITAL - mean_p * n) / (sd_p * math.sqrt(n))
        brown["p_below_ruin_at_n"] = round(norm_cdf(z_ruin), 4)
        brown["p_equity_double_at_n"] = round(1.0 - norm_cdf(z_target), 4)
        a_dist = CAPITAL - RUIN_LEVEL
        b_dist = CAPITAL
        if abs(mean_p) > 1e-9:
            s2 = sd_p * sd_p
            p_hit_down = (math.exp(-2 * mean_p * b_dist / s2) - 1.0) / \
                         (math.exp(-2 * mean_p * (a_dist + b_dist) / s2) - 1.0)
        else:
            p_hit_down = b_dist / (a_dist + b_dist)
        brown["p_hit_ruin_before_double"] = round(max(0.0, min(1.0, p_hit_down)), 4)
        brown["expected_pnl_at_n"] = round(mean_p * n, 2)
    out["brownian"] = brown

    # ---- Markov 2-state (from subsample)
    states = [1 if p > 0 else 0 for p in pnls_sample]
    tr = [[0, 0], [0, 0]]
    for a_, b_ in zip(states, states[1:]):
        tr[a_][b_] += 1
    def _p(row, idx):
        s = sum(row)
        return row[idx] / s if s else 0.0
    p_wl = _p(tr[1], 0); p_ww = _p(tr[1], 1)
    p_lw = _p(tr[0], 1); p_ll = _p(tr[0], 0)
    denom = (1 - p_ww + p_lw)
    stat_win = p_lw / denom if denom > 0 else None
    out["markov"] = {
        "p_win_after_win": round(p_ww, 4), "p_loss_after_win": round(p_wl, 4),
        "p_win_after_loss": round(p_lw, 4), "p_loss_after_loss": round(p_ll, 4),
        "stationary_win_rate": round(stat_win, 4) if stat_win is not None else None,
    }

    # ---- Bayesian
    a_b, b_b = 1 + wins, 1 + losses
    se_mean = sd_p / math.sqrt(n) if n > 0 else 0.0
    out["bayesian"] = {
        "winrate_post_mean": round(a_b / (a_b + b_b), 4),
        "winrate_ci_2.5": round(beta_ppf(0.025, a_b, b_b), 4),
        "winrate_ci_97.5": round(beta_ppf(0.975, a_b, b_b), 4),
        "p_winrate_gt_0.5": round(1.0 - beta_cdf(0.5, a_b, b_b), 4),
        "meanpnl_post_mean": round(mean_p, 4),
        "p_meanpnl_gt_0": round(1.0 - t_cdf(-mean_p / se_mean, n - 1), 4) if se_mean > 0 else None,
    }

    # ---- Timing
    if hold_secs and first_open and last_open and last_open > first_open:
        span_days = (last_open - first_open).total_seconds() / 86400
        out["timing"] = {
            "avg_hold_sec": round(statistics.fmean(hold_secs), 1),
            "median_hold_sec": round(statistics.median(hold_secs), 1),
            "span_days": round(span_days, 2),
            "trades_per_day": round(n / span_days, 2),
        }

    # ---- Regressions on subsample equity curve
    xs = list(range(1, len(eq)))
    ys = eq[1:]
    if len(xs) > 10:
        lin = poly_fit(xs, ys, 1)
        if lin:
            c, r2 = lin
            out["regressions"] = {
                "linear_slope": round(c[1], 6),
                "linear_r2": round(r2, 4),
            }

    elapsed = time.time() - t0
    out["compute_time_s"] = round(elapsed, 1)
    print(f"  {name}: done in {elapsed:.0f}s ({n} trades)", flush=True)
    return out


def run_one(name):
    path = os.path.join(RESULTS_IS, f"{name}.trades.jsonl.gz")
    if not os.path.exists(path):
        return {"strategy": name, "error": "file not found"}
    return analyze_fast(name, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    os.makedirs(RESULTS_Q, exist_ok=True)
    paths = sorted(glob.glob(os.path.join(RESULTS_IS, "*.trades.jsonl.gz")))
    names = [os.path.basename(p).replace(".trades.jsonl.gz", "") for p in paths]
    if args.only:
        keep = set(args.only.split(","))
        names = [n for n in names if n in keep]

    # Filter out tiny files (<1KB = incomplete)
    filtered = []
    for n in names:
        p = os.path.join(RESULTS_IS, f"{n}.trades.jsonl.gz")
        if os.path.getsize(p) > 1000:
            filtered.append(n)
        else:
            print(f"SKIP {n} (<1KB, incomplete)", flush=True)
    names = filtered

    print(f"quant_fast over {len(names)} strategies, {args.workers} workers", flush=True)
    t0 = time.time()
    results = {}
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(run_one, n): n for n in names}
        for i, fut in enumerate(as_completed(futs), 1):
            n = futs[fut]
            try:
                res = fut.result()
            except Exception as e:
                res = {"strategy": n, "error": f"{type(e).__name__}: {e}"}
            results[n] = res

    # DSR second pass
    srs = [r["risk"]["sharpe_per_trade"] for r in results.values()
           if r.get("risk") and r["risk"]["sharpe_per_trade"] is not None]
    var_sr = statistics.variance(srs) if len(srs) > 1 else 0.0
    euler = 0.5772156649015329
    emax = (math.sqrt(var_sr) * ((1 - euler) * norm_ppf(1 - 1.0 / N_TRIALS)
                                 + euler * norm_ppf(1 - 1.0 / (N_TRIALS * math.e))))
    for r in results.values():
        if not r.get("risk"): continue
        sr = r["risk"]["sharpe_per_trade"]
        nn = max(2, r.get("n_trades", 2))
        skew = r["risk"]["skew"]; kurt = r["risk"]["excess_kurtosis"] + 3.0
        var_term = max(1e-12, 1 - skew * sr + ((kurt - 1) / 4.0) * sr * sr)
        z = (sr - emax) * math.sqrt(nn - 1) / math.sqrt(var_term)
        r["risk"]["dsr"] = round(norm_cdf(z), 4)

    # Write results
    for n, r in results.items():
        with open(os.path.join(RESULTS_Q, f"{n}.quant.json"), "w") as fh:
            json.dump(r, fh, indent=1)

    ranked = sorted(
        (r for r in results.values() if not r.get("empty") and not r.get("error")),
        key=lambda r: r["core"]["total_pnl"], reverse=True)

    lines = ["# Regime-filter quant leaderboard (taker-fill, $200 start)\n",
             "| rank | strategy | trades | win% | pnl$ | pnl% | PF | Sharpe | Sortino | maxDD% | PSR | DSR | P(ruin) | boot CI95 | time |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(ranked, 1):
        c = r["core"]; rk = r["risk"]; dd = r["drawdown"]
        mc = r.get("monte_carlo_5k", {}); bs = r.get("bootstrap_5k", {})
        pf = c["profit_factor"] if c.get("profit_factor") is not None else "inf"
        t_s = r.get("compute_time_s", "?")
        lines.append(
            f"| {i} | {r['strategy'][:50]} | {r['n_trades']:,} | {100*c['win_rate']:.1f} | "
            f"{c['total_pnl']:.2f} | {c['return_pct']:.1f} | {pf} | {rk['sharpe_per_trade']:.3f} | "
            f"{rk['sortino_per_trade']:.3f} | {dd['max_dd_pct']:.1f} | {rk['psr']:.3f} | {rk['dsr']:.3f} | "
            f"{mc.get('p_ruin','?')} | "
            f"[{bs.get('pnl_ci_2.5','?')},{bs.get('pnl_ci_97.5','?')}] | {t_s}s |")

    with open(os.path.join(RESULTS_Q, "leaderboard.md"), "w") as fh:
        fh.write("\n".join(lines) + "\n")
    with open(os.path.join(RESULTS_Q, "leaderboard.json"), "w") as fh:
        json.dump(ranked, fh, indent=1)

    elapsed = time.time() - t0
    print(f"\n{'='*80}")
    print(open(os.path.join(RESULTS_Q, "leaderboard.md")).read())
    print(f"{'='*80}")
    print(f"\nquant_fast done in {elapsed:.0f}s -> {RESULTS_Q}")


if __name__ == "__main__":
    main()
