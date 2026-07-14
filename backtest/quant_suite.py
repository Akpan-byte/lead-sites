#!/usr/bin/env python3
# CHANGE_SUMMARY
# 2026-07-11  kimi
#   - Full quant suite for the BTC-5m backtest results (sandbox only, stdlib-only).
#     Per strategy: core trade stats, Sharpe/Sortino, PSR, DSR (N=113 trials,
#     cross-sectional), drawdown suite (maxDD $/%, durations, ulcer, recovery),
#     50k bootstrap CI of total pnl, 50k Monte-Carlo order-reshuffle (P(ruin),
#     maxDD distribution), Brownian drift/vol/hitting probabilities, 2-state
#     Markov win/loss chain, Bayesian Beta (win rate) + Normal-Gamma (mean pnl),
#     and lin/quad/cubic/poly/exp regressions of equity vs trade index with R^2.
#     Walk-forward intentionally omitted per user (to be added later).
#   - Parallel per strategy; reads results/is/*.trades.jsonl.gz; writes
#     results/quant/<name>.quant.json + leaderboard.{json,md}.
# WHY: user asked for the full quant battery over the real-fill backtest trades;
#      no numpy/pandas on this VM, so everything is closed-form stdlib.
"""Quant suite over backtest trade logs. Usage:
  python3 quant_suite.py            # all strategies in results/is
  python3 quant_suite.py --only NAME1,NAME2
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import math
import os
import random
import statistics
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from itertools import accumulate

HERE = os.path.dirname(os.path.abspath(__file__))
# env-var dirs (import-time) for forkserver safety — see run_is.py header.
RESULTS_IS = os.path.join(HERE, "results", os.environ.get("BT_IS_DIR", "is"))
RESULTS_Q = os.path.join(HERE, "results", os.environ.get("BT_Q_DIR", "quant"))
GDRIVE_Q = f"gdrive:trading_backtest/results/{os.environ.get('BT_Q_DIR', 'quant')}"

CAPITAL = 200.0
RUIN_LEVEL = 5.0        # equity below this cannot afford the 5-contract min trade
N_SIMS = 50_000
N_TRIALS = 113          # strategy count for DSR multiple-testing deflation
SEED = 20260711


# --------------------------------------------------------------------------- #
# small numeric helpers (stdlib)
# --------------------------------------------------------------------------- #
def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_ppf(p: float) -> float:
    """Inverse normal CDF (Acklam's algorithm, |err| < 1.15e-9)."""
    if p <= 0.0:
        return -math.inf
    if p >= 1.0:
        return math.inf
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    pl, ph = 0.02425, 1 - 0.02425
    if p < pl:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p <= ph:
        q = p - 0.5; r = q*q
        return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
               (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)
    q = math.sqrt(-2 * math.log(1-p))
    return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
             ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)


def _betacf(a: float, b: float, x: float) -> float:
    MAXIT, EPS, FPMIN = 200, 3.0e-12, 1.0e-30
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < FPMIN:
        d = FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        delh = d * c
        h *= delh
        if abs(delh - 1.0) < EPS:
            break
    return h


def beta_cdf(x: float, a: float, b: float) -> float:
    """Regularized incomplete beta I_x(a,b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    bt = math.exp(lbeta + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def beta_ppf(p: float, a: float, b: float) -> float:
    """Inverse of beta CDF via bisection (monotone, robust)."""
    lo, hi = 0.0, 1.0
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if beta_cdf(mid, a, b) < p:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def t_cdf(t: float, df: float) -> float:
    """Student-t CDF via incomplete beta."""
    x = df / (df + t * t)
    ib = beta_cdf(x, df / 2.0, 0.5)
    return 1.0 - 0.5 * ib if t > 0 else 0.5 * ib


def solve_linear(A: list[list[float]], b: list[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting (small n)."""
    n = len(A)
    M = [row[:] + [bb] for row, bb in zip(A, b)]
    for i in range(n):
        piv = max(range(i, n), key=lambda r: abs(M[r][i]))
        if abs(M[piv][i]) < 1e-12:
            return None
        M[i], M[piv] = M[piv], M[i]
        for j in range(i + 1, n):
            f = M[j][i] / M[i][i]
            for k in range(i, n + 1):
                M[j][k] -= f * M[i][k]
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        s = M[i][n] - sum(M[i][k] * x[k] for k in range(i + 1, n))
        x[i] = s / M[i][i]
    return x


def poly_fit(xs: list[float], ys: list[float], deg: int):
    """Least-squares polynomial fit. Returns (coeffs asc degree, r2) or None."""
    n = len(xs)
    m = deg + 1
    # normal equations
    xpow = [[sum(x ** (i + j) for x in xs) for j in range(m)] for i in range(m)]
    xty = [sum(y * (x ** i) for x, y in zip(xs, ys)) for i in range(m)]
    coeffs = solve_linear(xpow, xty)
    if coeffs is None:
        return None
    ybar = statistics.fmean(ys)
    ss_tot = sum((y - ybar) ** 2 for y in ys)
    ss_res = sum((y - sum(c * (x ** k) for k, c in enumerate(coeffs))) ** 2
                 for x, y in zip(xs, ys))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0
    return coeffs, r2


# --------------------------------------------------------------------------- #
# per-strategy analysis
# --------------------------------------------------------------------------- #
def _drawdowns(equity: list[float]):
    peak = equity[0]
    dds = []
    cur_peak_i = 0
    for i, e in enumerate(equity):
        if e >= peak:
            peak = e
            cur_peak_i = i
        dds.append((peak - e, i - cur_peak_i))
    abs_dd = [d[0] for d in dds]
    return abs_dd, dds


def analyze(name: str, trades: list[dict], n_sims: int = N_SIMS) -> dict:
    pnls = [float(t["pnl"]) for t in trades]
    n = len(pnls)
    out = {"strategy": name, "n_trades": n, "capital": CAPITAL}
    if n == 0:
        out["empty"] = True
        return out

    equity = [CAPITAL] + [CAPITAL + s for s in accumulate(pnls)]
    total_pnl = pnls and equity[-1] - CAPITAL
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    n_w, n_l = len(wins), len(losses)

    # ---- core stats
    gp, gl = sum(wins), -sum(losses)
    mean_p = statistics.fmean(pnls)
    sd_p = statistics.stdev(pnls) if n > 1 else 0.0
    med_p = statistics.median(pnls)
    out["core"] = {
        "total_pnl": round(total_pnl, 4),
        "final_equity": round(equity[-1], 4),
        "return_pct": round(100 * total_pnl / CAPITAL, 2),
        "wins": n_w, "losses": n_l, "win_rate": round(n_w / n, 4),
        "gross_profit": round(gp, 4), "gross_loss": round(gl, 4),
        "profit_factor": round(gp / gl, 4) if gl > 0 else None,
        "avg_win": round(gp / n_w, 4) if n_w else 0.0,
        "avg_loss": round(-gl / n_l, 4) if n_l else 0.0,
        "expectancy": round(mean_p, 4),
        "median_pnl": round(med_p, 4),
        "std_pnl": round(sd_p, 4),
        "best_trade": round(max(pnls), 4), "worst_trade": round(min(pnls), 4),
        "payoff_ratio": round((gp / n_w) / (gl / n_l), 4) if n_w and n_l and gl else None,
    }

    # ---- Sharpe / Sortino / PSR (per-trade)
    sr = mean_p / sd_p if sd_p > 0 else 0.0
    downside = [min(0.0, p) for p in pnls]
    dd_sd = statistics.stdev(downside) if n > 1 else 0.0
    sortino = mean_p / dd_sd if dd_sd > 0 else 0.0
    skew = (statistics.fmean([(p - mean_p) ** 3 for p in pnls]) / sd_p ** 3) if sd_p > 0 and n > 2 else 0.0
    kurt = (statistics.fmean([(p - mean_p) ** 4 for p in pnls]) / sd_p ** 4) if sd_p > 0 and n > 3 else 3.0
    sr_se = math.sqrt(max(1e-12, (1 - skew * sr + ((kurt - 1) / 4.0) * sr * sr) / max(1, n - 1)))
    psr = norm_cdf(sr / sr_se) if sr_se > 0 else 1.0
    out["risk"] = {
        "sharpe_per_trade": round(sr, 4),
        "sortino_per_trade": round(sortino, 4),
        "skew": round(skew, 4), "excess_kurtosis": round(kurt - 3.0, 4),
        "psr": round(psr, 4),
        # DSR filled in a second pass (needs cross-sectional SR variance)
        "dsr": None,
    }

    # ---- drawdown suite
    abs_dd, dd_pairs = _drawdowns(equity)
    max_dd = max(abs_dd)
    dd_pct = [d / CAPITAL for d in abs_dd]
    ulcer = math.sqrt(statistics.fmean([d * d for d in dd_pct]))
    # duration of the max DD episode and longest DD episode (in trades)
    i_max = abs_dd.index(max_dd)
    dur_max = dd_pairs[i_max][1]
    longest = max((d[1] for d in dd_pairs), default=0)
    # recovery: trades from maxDD trough until equity regains prior peak (or None)
    peak_at_trough = equity[i_max] + max_dd
    rec = None
    for j in range(i_max + 1, len(equity)):
        if equity[j] >= peak_at_trough:
            rec = j - i_max
            break
    out["drawdown"] = {
        "max_dd_usd": round(max_dd, 4),
        "max_dd_pct": round(100 * max_dd / CAPITAL, 2),
        "max_dd_duration_trades": dur_max,
        "longest_dd_trades": longest,
        "recovery_trades": rec,
        "ulcer_index": round(ulcer, 4),
        "pct_time_underwater": round(sum(1 for d in abs_dd if d > 0) / len(abs_dd), 4),
    }

    # ---- streaks
    streak_w = streak_l = cur_w = cur_l = 0
    for p in pnls:
        if p > 0:
            cur_w += 1; cur_l = 0
        elif p < 0:
            cur_l += 1; cur_w = 0
        streak_w = max(streak_w, cur_w); streak_l = max(streak_l, cur_l)
    out["streaks"] = {"max_win_streak": streak_w, "max_loss_streak": streak_l}

    # ---- 50k bootstrap CI (resample trades with replacement -> total pnl dist)
    rng = random.Random(SEED + hash(name) % 100_000)
    choices = rng.choices
    boot = []
    for _ in range(n_sims):
        boot.append(sum(choices(pnls, k=n)))
    boot.sort()
    q = lambda p: boot[min(n_sims - 1, int(p * n_sims))]
    out["bootstrap_50k"] = {
        "pnl_ci_2.5": round(q(0.025), 2), "pnl_ci_50": round(q(0.5), 2),
        "pnl_ci_97.5": round(q(0.975), 2),
        "p_pnl_positive": round(sum(1 for b in boot if b > 0) / n_sims, 4),
    }

    # ---- 50k Monte-Carlo order reshuffle: P(ruin) + maxDD distribution
    base = pnls[:]
    ruin = 0
    mc_dd = []
    for _ in range(n_sims):
        rng.shuffle(base)
        cum = list(accumulate(base))
        if CAPITAL + min(cum) < RUIN_LEVEL:
            ruin += 1
        peaks = list(accumulate(cum, max))
        dd = max(pk - c for pk, c in zip(peaks, cum))
        mc_dd.append(dd)
    mc_dd.sort()
    out["monte_carlo_50k"] = {
        "p_ruin": round(ruin / n_sims, 4),
        "ruin_level_usd": RUIN_LEVEL,
        "maxdd_ci_50": round(mc_dd[int(0.5 * n_sims)], 2),
        "maxdd_ci_95": round(mc_dd[int(0.95 * n_sims)], 2),
        "maxdd_ci_99": round(mc_dd[int(0.99 * n_sims)], 2),
        "note": "order-reshuffle MC; final equity invariant -> CI from bootstrap",
    }

    # ---- Brownian: drift/vol per trade + hitting probabilities over horizon n
    mu, sig = mean_p, sd_p
    brown = {"drift_per_trade": round(mu, 4), "vol_per_trade": round(sig, 4)}
    if sig > 0:
        z_ruin = (RUIN_LEVEL - CAPITAL - mu * n) / (sig * math.sqrt(n))
        z_target = (CAPITAL - mu * n) / (sig * math.sqrt(n))  # P(double) complement
        brown["p_below_ruin_at_n"] = round(norm_cdf(z_ruin), 4)
        brown["p_equity_double_at_n"] = round(1.0 - norm_cdf(z_target), 4)
        # gambler's-ruin style: P(hit ruin before +CAPITAL gain), ABM barrier formula
        a = CAPITAL - RUIN_LEVEL   # distance down
        b = CAPITAL                # distance up
        if abs(mu) > 1e-9:
            s2 = sig * sig
            p_hit_down = (math.exp(-2 * mu * b / s2) - 1.0) / (math.exp(-2 * mu * (a + b) / s2) - 1.0)
        else:
            p_hit_down = b / (a + b)
        brown["p_hit_ruin_before_double"] = round(max(0.0, min(1.0, p_hit_down)), 4)
        brown["expected_pnl_at_n"] = round(mu * n, 2)
    out["brownian"] = brown

    # ---- Markov 2-state (win/loss)
    states = [1 if p > 0 else 0 for p in pnls]
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
        "expected_win_streak": round(1 / (1 - p_ww), 2) if p_ww < 1 else None,
        "expected_loss_streak": round(1 / (1 - p_ll), 2) if p_ll < 1 else None,
    }

    # ---- Bayesian: Beta(win rate) + Normal-Gamma(mean pnl)
    a_b, b_b = 1 + n_w, 1 + n_l
    ng_df = n - 1
    se_mean = sd_p / math.sqrt(n) if n > 0 else 0.0
    out["bayesian"] = {
        "winrate_post_mean": round(a_b / (a_b + b_b), 4),
        "winrate_ci_2.5": round(beta_ppf(0.025, a_b, b_b), 4),
        "winrate_ci_97.5": round(beta_ppf(0.975, a_b, b_b), 4),
        "p_winrate_gt_0.5": round(1.0 - beta_cdf(0.5, a_b, b_b), 4),
        "meanpnl_post_mean": round(mean_p, 4),
        "p_meanpnl_gt_0": round(1.0 - t_cdf(-mean_p / se_mean, ng_df), 4) if se_mean > 0 else None,
    }

    # ---- regressions of equity vs trade index
    xs = list(range(1, n + 1))
    ys = equity[1:]
    regs = {}
    lin = poly_fit(xs, ys, 1)
    if lin:
        c, r2 = lin
        regs["linear"] = {"a0": round(c[0], 4), "a1_slope_per_trade": round(c[1], 4),
                          "r2": round(r2, 4), "equity_at_2n": round(c[0] + c[1] * 2 * n, 2)}
    qua = poly_fit(xs, ys, 2)
    if qua:
        c, r2 = qua
        regs["quadratic"] = {"a0": round(c[0], 4), "a1": round(c[1], 4), "a2": round(c[2], 6),
                             "r2": round(r2, 4),
                             "equity_at_2n": round(c[0] + c[1] * 2 * n + c[2] * 4 * n * n, 2)}
    cub = poly_fit(xs, ys, 3)
    if cub:
        c, r2 = cub
        regs["cubic"] = {"a0": round(c[0], 4), "a1": round(c[1], 4), "a2": round(c[2], 6),
                         "a3": round(c[3], 8), "r2": round(r2, 4),
                         "equity_at_2n": round(c[0] + c[1]*2*n + c[2]*4*n*n + c[3]*8*n**3, 2)}
        regs["polynomial"] = regs["cubic"]  # poly = cubic basis (deg 3)
    if all(y > 0 for y in ys):
        logy = [math.log(y) for y in ys]
        exf = poly_fit(xs, logy, 1)
        if exf:
            c, r2 = exf
            regs["exponential"] = {
                "base_per_trade": round(math.exp(c[1]), 6),
                "growth_pct_per_trade": round(100 * (math.exp(c[1]) - 1), 4),
                "r2_log": round(r2, 4),
                "equity_at_2n": round(math.exp(c[0] + c[1] * 2 * n), 2)}
    out["regressions"] = regs

    # ---- timing / frequency
    try:
        from datetime import datetime, timezone
        def _pdt(v):
            if v is None:
                return None
            if isinstance(v, (int, float)):
                return datetime.fromtimestamp(float(v), tz=timezone.utc)
            s = str(v)
            try:
                return datetime.fromisoformat(s.replace("Z", "+00:00"))
            except ValueError:
                return datetime.fromtimestamp(float(s), tz=timezone.utc)
        holds = []
        first_o = last_o = None
        for t in trades:
            o = _pdt(t.get("opened_at")); c = _pdt(t.get("closed_at"))
            if o and c:
                holds.append((c - o).total_seconds())
            if o:
                first_o = o if first_o is None or o < first_o else first_o
                last_o = o if last_o is None or o > last_o else last_o
        span_days = (last_o - first_o).total_seconds() / 86400 if first_o and last_o and last_o > first_o else None
        out["timing"] = {
            "avg_hold_sec": round(statistics.fmean(holds), 1) if holds else None,
            "median_hold_sec": round(statistics.median(holds), 1) if holds else None,
            "span_days": round(span_days, 2) if span_days else None,
            "trades_per_day": round(n / span_days, 2) if span_days else None,
        }
    except Exception:
        out["timing"] = None
    return out


def _load_trades(path: str) -> list[dict]:
    out = []
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def run_one(name: str) -> dict:
    path = os.path.join(RESULTS_IS, f"{name}.trades.jsonl.gz")
    trades = _load_trades(path) if os.path.exists(path) else []
    return analyze(name, trades)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--upload", action="store_true")
    ap.add_argument("--fill", choices=["maker", "instant", "taker"], default="maker",
                    help="which IS result set to analyze (results/is_{fill} -> results/quant_{fill})")
    args = ap.parse_args()
    if os.path.basename(RESULTS_IS) != f"is_{args.fill}" or \
       os.path.basename(RESULTS_Q) != f"quant_{args.fill}":
        print(f"WARN: dirs {RESULTS_IS}/{RESULTS_Q} != --fill {args.fill}", flush=True)
    os.makedirs(RESULTS_Q, exist_ok=True)
    paths = sorted(glob.glob(os.path.join(RESULTS_IS, "*.trades.jsonl.gz")))
    names = [os.path.basename(p).replace(".trades.jsonl.gz", "") for p in paths]
    if args.only:
        keep = set(args.only.split(","))
        names = [n for n in names if n in keep]
    print(f"quant over {len(names)} strategies", flush=True)

    t0 = time.time()
    results = {}
    # Checkpoint-skip: reuse existing per-strat quant.json so pre-runs and the
    # orchestrator's full pass compose instead of redoing 123s/strat of work.
    # The DSR second pass below still covers every strat (cached + fresh), so
    # cross-sectional DSR stays correct regardless of what was pre-computed.
    todo = []
    for n in names:
        cached = os.path.join(RESULTS_Q, f"{n}.quant.json")
        if os.path.exists(cached):
            try:
                with open(cached) as fh:
                    results[n] = json.load(fh)
                continue
            except Exception:
                pass  # corrupt cache -> recompute
        todo.append(n)
    print(f"quant: {len(names)} total, {len(todo)} to compute, {len(results)} cached", flush=True)
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(run_one, n): n for n in todo}
        for i, fut in enumerate(as_completed(futs), 1):
            n = futs[fut]
            try:
                res = fut.result()
            except Exception as e:
                res = {"strategy": n, "error": f"{type(e).__name__}: {e}"}
            results[n] = res
            if i % 20 == 0:
                print(f"  {i}/{len(names)} ({time.time()-t0:.0f}s)", flush=True)

    # second pass: DSR with cross-sectional SR variance, N_TRIALS strategies
    srs = [r["risk"]["sharpe_per_trade"] for r in results.values()
           if r.get("risk") and r["risk"]["sharpe_per_trade"] is not None]
    var_sr = statistics.variance(srs) if len(srs) > 1 else 0.0
    n_obs = {r["strategy"]: r.get("n_trades", 0) for r in results.values()}
    euler = 0.5772156649015329
    emax = (math.sqrt(var_sr) * ((1 - euler) * norm_ppf(1 - 1.0 / N_TRIALS)
                                 + euler * norm_ppf(1 - 1.0 / (N_TRIALS * math.e))))
    for r in results.values():
        if not r.get("risk"):
            continue
        sr = r["risk"]["sharpe_per_trade"]
        nn = max(2, n_obs.get(r["strategy"], 2))
        # PSR-style z of (sr - E[max sr]) with same variance shape
        skew = r["risk"]["skew"]; kurt_ex = r["risk"]["excess_kurtosis"]; kurt = kurt_ex + 3.0
        var_term = max(1e-12, 1 - skew * sr + ((kurt - 1) / 4.0) * sr * sr)
        z = (sr - emax) * math.sqrt(nn - 1) / math.sqrt(var_term)
        r["risk"]["dsr"] = round(norm_cdf(z), 4)

    # write per-strategy json + leaderboard
    for n, r in results.items():
        with open(os.path.join(RESULTS_Q, f"{n}.quant.json"), "w") as fh:
            json.dump(r, fh, indent=1)
    ranked = sorted(
        (r for r in results.values() if not r.get("empty") and not r.get("error")),
        key=lambda r: (r["core"]["total_pnl"]), reverse=True)
    with open(os.path.join(RESULTS_Q, "leaderboard.json"), "w") as fh:
        json.dump(ranked, fh, indent=1)
    lines = [f"# BTC-5m backtest leaderboard (in-sample, {args.fill}-fill, $200 start)\n",
             "| rank | strategy | trades | win% | pnl$ | pnl% | PF | Sharpe | maxDD% | PSR | DSR | P(ruin) | boot CI95 |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(ranked, 1):
        c, rk, dd, mc, bs = r["core"], r["risk"], r["drawdown"], r["monte_carlo_50k"], r["bootstrap_50k"]
        pf = c["profit_factor"] if c["profit_factor"] is not None else "inf"
        lines.append(
            f"| {i} | {r['strategy']} | {r['n_trades']} | {100*c['win_rate']:.1f} | "
            f"{c['total_pnl']:.2f} | {c['return_pct']:.1f} | {pf} | {rk['sharpe_per_trade']:.3f} | "
            f"{dd['max_dd_pct']:.1f} | {rk['psr']:.3f} | {rk['dsr']:.3f} | {mc['p_ruin']:.3f} | "
            f"[{bs['pnl_ci_2.5']:.0f},{bs['pnl_ci_97.5']:.0f}] |")
    with open(os.path.join(RESULTS_Q, "leaderboard.md"), "w") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"quant done in {time.time()-t0:.0f}s -> {RESULTS_Q}", flush=True)
    if args.upload:
        subprocess.run(["rclone", "copy", RESULTS_Q, GDRIVE_Q], capture_output=True)
        print("uploaded to", GDRIVE_Q, flush=True)


if __name__ == "__main__":
    main()
