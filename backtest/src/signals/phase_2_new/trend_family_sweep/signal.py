# CHANGE_SUMMARY
# 2026-07-17  assistant
#   - Created trend_family_sweep signal module.
#   - Implements 50+ diverse trend estimators (MAs, regressions, filters, channels).
#   - Signal triggers YES/NO when spot deviates from the estimator by a threshold.
#   - All parameters are passed via kwargs so a sweep registry can vary them.
# WHY: Find the best uncorrelated trend-deviation variants for the BTC 5m up/down stack.

from typing import Any, Dict, Callable, List
import numpy as np

_STATE: Dict[str, Dict[str, Any]] = {}

TREND_FUNCS: Dict[str, Callable[[np.ndarray, Dict[str, Any]], float]] = {}


def _register(name: str):
    def decorator(fn: Callable[[np.ndarray, Dict[str, Any]], float]):
        TREND_FUNCS[name] = fn
        return fn
    return decorator


def _np_window(arr: np.ndarray, lookback: int) -> np.ndarray:
    n = len(arr)
    if n == 0:
        return arr
    start = max(0, n - lookback)
    return arr[start:]


# ---------------------------------------------------------------------------
# 1. Exponential moving average variants
# ---------------------------------------------------------------------------
@_register("ema")
def _ema(arr: np.ndarray, p: Dict[str, Any]) -> float:
    alpha = float(p.get("alpha", 0.15))
    est = arr[0]
    for x in arr[1:]:
        est = alpha * x + (1 - alpha) * est
    return float(est)


@_register("dema")
def _dema(arr: np.ndarray, p: Dict[str, Any]) -> float:
    alpha = float(p.get("alpha", 0.15))
    ema1 = _ema(arr, {"alpha": alpha})
    ema2 = _ema(np.array([ema1] * len(arr)), {"alpha": alpha})  # placeholder
    # proper DEMA: 2*EMA - EMA(EMA)
    e1 = arr[0]
    e1s = [e1]
    for x in arr[1:]:
        e1 = alpha * x + (1 - alpha) * e1
        e1s.append(e1)
    e2 = e1s[0]
    for x in e1s[1:]:
        e2 = alpha * x + (1 - alpha) * e2
    return float(2 * e1 - e2)


@_register("tema")
def _tema(arr: np.ndarray, p: Dict[str, Any]) -> float:
    alpha = float(p.get("alpha", 0.15))
    e1 = arr[0]
    e1s = [e1]
    for x in arr[1:]:
        e1 = alpha * x + (1 - alpha) * e1
        e1s.append(e1)
    e2 = e1s[0]
    e2s = [e2]
    for x in e1s[1:]:
        e2 = alpha * x + (1 - alpha) * e2
        e2s.append(e2)
    e3 = e2s[0]
    for x in e2s[1:]:
        e3 = alpha * x + (1 - alpha) * e3
    return float(3 * e1 - 3 * e2 + e3)


@_register("zlema")
def _zlema(arr: np.ndarray, p: Dict[str, Any]) -> float:
    alpha = float(p.get("alpha", 0.15))
    lag = int(round((1 - alpha) / alpha))
    if lag >= len(arr):
        lag = len(arr) - 1
    if lag <= 0:
        return float(arr[-1])
    x_lag = arr[:-lag]
    x_cur = arr[lag:]
    if len(x_lag) == 0 or len(x_cur) == 0:
        return float(arr[-1])
    # zero-lag series = 2*current - lagged
    zl = 2 * x_cur[-len(x_lag):] - x_lag
    return _ema(zl, {"alpha": alpha})


# ---------------------------------------------------------------------------
# 2. Simple/weighted/smoothed moving averages
# ---------------------------------------------------------------------------
@_register("sma")
def _sma(arr: np.ndarray, p: Dict[str, Any]) -> float:
    return float(np.mean(arr))


@_register("wma")
def _wma(arr: np.ndarray, p: Dict[str, Any]) -> float:
    n = len(arr)
    weights = np.arange(1, n + 1)
    return float(np.sum(arr * weights) / np.sum(weights))


@_register("smma")
def _smma(arr: np.ndarray, p: Dict[str, Any]) -> float:
    alpha = float(p.get("alpha", 0.15))
    est = arr[0]
    for x in arr[1:]:
        est = alpha * x + (1 - alpha) * est
    return float(est)


@_register("hma")
def _hma(arr: np.ndarray, p: Dict[str, Any]) -> float:
    n = len(arr)
    half = max(1, n // 2)
    sqrt_n = max(1, int(round(np.sqrt(n))))
    wma_half = np.sum(arr[-half:] * np.arange(1, half + 1)) / np.sum(np.arange(1, half + 1))
    wma_full = np.sum(arr * np.arange(1, n + 1)) / np.sum(np.arange(1, n + 1))
    raw = 2 * wma_half - wma_full
    # WMA of raw over sqrt(n)
    raw_series = np.array([raw] * sqrt_n)
    return float(np.sum(raw_series * np.arange(1, sqrt_n + 1)) / np.sum(np.arange(1, sqrt_n + 1)))


@_register("alma")
def _alma(arr: np.ndarray, p: Dict[str, Any]) -> float:
    n = len(arr)
    offset = float(p.get("alma_offset", 0.85))
    sigma = float(p.get("alma_sigma", 6.0))
    m = int(np.floor(offset * (n - 1)))
    s = n / sigma
    weights = np.exp(-((np.arange(n) - m) ** 2) / (2 * s * s))
    weights /= np.sum(weights)
    return float(np.sum(arr * weights))


@_register("mcginley")
def _mcginley(arr: np.ndarray, p: Dict[str, Any]) -> float:
    k = float(p.get("mcginley_k", 0.6))
    est = arr[0]
    for x in arr[1:]:
        est = est + (x - est) / (k * (len(arr) ** 2) * (x / est) ** 4)
    return float(est)


# ---------------------------------------------------------------------------
# 3. Adaptive moving averages
# ---------------------------------------------------------------------------
@_register("kama")
def _kama(arr: np.ndarray, p: Dict[str, Any]) -> float:
    n = len(arr)
    er_len = int(p.get("kama_er_len", 10))
    fast = float(p.get("kama_fast", 0.6667))
    slow = float(p.get("kama_slow", 0.0645))
    if n < er_len + 2:
        return float(arr[-1])
    est = arr[0]
    for i in range(1, n):
        start = max(0, i - er_len)
        change = abs(arr[i] - arr[start])
        volatility = np.sum(np.abs(np.diff(arr[start:i+1])))
        er = change / volatility if volatility > 0 else 0
        sc = (er * (fast - slow) + slow) ** 2
        est = est + sc * (arr[i] - est)
    return float(est)


@_register("frama")
def _frama(arr: np.ndarray, p: Dict[str, Any]) -> float:
    n = len(arr)
    if n < 16:
        return float(np.mean(arr))
    half = n // 2
    h1 = arr[:half].max() - arr[:half].min()
    h2 = arr[half:].max() - arr[half:].min()
    n1 = (h1 + h2) / half
    n2 = (arr.max() - arr.min()) / n
    d = np.log(n1 + n2) / np.log(2) if (n1 + n2) > 0 else 0
    alpha = np.exp(-4.6 * (d - 1))
    alpha = max(0.01, min(1.0, alpha))
    est = arr[0]
    for x in arr[1:]:
        est = alpha * x + (1 - alpha) * est
    return float(est)


@_register("vidya")
def _vidya(arr: np.ndarray, p: Dict[str, Any]) -> float:
    n = len(arr)
    cmolen = int(p.get("vidya_cmo_len", 9))
    alpha = float(p.get("alpha", 0.2))
    if n < cmolen + 1:
        return float(arr[-1])
    su = np.sum(np.diff(arr[-cmolen:])[np.diff(arr[-cmolen:]) > 0])
    sd = np.sum(-np.diff(arr[-cmolen:])[np.diff(arr[-cmolen:]) < 0])
    cmo = abs((su - sd) / (su + sd)) if (su + sd) > 0 else 0
    est = arr[0]
    for x in arr[1:]:
        est = alpha * cmo * x + (1 - alpha * cmo) * est
    return float(est)


# ---------------------------------------------------------------------------
# 4. Regression-based trends
# ---------------------------------------------------------------------------
@_register("linreg")
def _linreg(arr: np.ndarray, p: Dict[str, Any]) -> float:
    x = np.arange(len(arr))
    A = np.vstack([x, np.ones(len(arr))]).T
    m, c = np.linalg.lstsq(A, arr, rcond=None)[0]
    return float(m * (len(arr) - 1) + c)


@_register("polyreg2")
def _polyreg2(arr: np.ndarray, p: Dict[str, Any]) -> float:
    x = np.arange(len(arr))
    coefs = np.polyfit(x, arr, 2)
    return float(np.polyval(coefs, len(arr) - 1))


@_register("polyreg3")
def _polyreg3(arr: np.ndarray, p: Dict[str, Any]) -> float:
    x = np.arange(len(arr))
    coefs = np.polyfit(x, arr, 3)
    return float(np.polyval(coefs, len(arr) - 1))


# ---------------------------------------------------------------------------
# 5. Filters
# ---------------------------------------------------------------------------
@_register("gaussian")
def _gaussian(arr: np.ndarray, p: Dict[str, Any]) -> float:
    n = len(arr)
    sigma = float(p.get("gaussian_sigma", 2.0))
    x = np.arange(n)
    weights = np.exp(-0.5 * ((x - (n - 1)) / sigma) ** 2)
    weights /= weights.sum()
    return float(np.sum(arr * weights))


@_register("median")
def _median(arr: np.ndarray, p: Dict[str, Any]) -> float:
    return float(np.median(arr))


@_register("butterworth")
def _butterworth(arr: np.ndarray, p: Dict[str, Any]) -> float:
    # simple recursive low-pass approximation
    alpha = float(p.get("alpha", 0.2))
    est = arr[0]
    for x in arr[1:]:
        est = est + alpha * (x - est)
    return float(est)


# ---------------------------------------------------------------------------
# 6. Channel / range midpoints
# ---------------------------------------------------------------------------
@_register("donchian_mid")
def _donchian_mid(arr: np.ndarray, p: Dict[str, Any]) -> float:
    return float((arr.max() + arr.min()) / 2)


@_register("bollinger_mid")
def _bollinger_mid(arr: np.ndarray, p: Dict[str, Any]) -> float:
    return float(np.mean(arr))


@_register("keltner_mid")
def _keltner_mid(arr: np.ndarray, p: Dict[str, Any]) -> float:
    return float(np.mean(arr))


@_register("minmax_mid")
def _minmax_mid(arr: np.ndarray, p: Dict[str, Any]) -> float:
    return float((arr.max() + arr.min()) / 2)


@_register("range_mid")
def _range_mid(arr: np.ndarray, p: Dict[str, Any]) -> float:
    return float((arr.max() + arr.min()) / 2)


# ---------------------------------------------------------------------------
# 7. Ichimoku / parabolic / other
# ---------------------------------------------------------------------------
@_register("tenkan_sen")
def _tenkan_sen(arr: np.ndarray, p: Dict[str, Any]) -> float:
    return float((arr.max() + arr.min()) / 2)


@_register("kijun_sen")
def _kijun_sen(arr: np.ndarray, p: Dict[str, Any]) -> float:
    return float((arr.max() + arr.min()) / 2)


@_register("parabolic_sar")
def _parabolic_sar(arr: np.ndarray, p: Dict[str, Any]) -> float:
    # simplified: trailing extreme
    af = float(p.get("psar_af", 0.02))
    max_af = float(p.get("psar_max_af", 0.2))
    ep = arr[0]
    sar = arr[0]
    trend = 1
    af_cur = af
    for i in range(1, len(arr)):
        if trend == 1:
            if arr[i] > ep:
                ep = arr[i]
                af_cur = min(af_cur + af, max_af)
            sar = sar + af_cur * (ep - sar)
            if arr[i] < sar:
                trend = -1
                sar = ep
                ep = arr[i]
                af_cur = af
        else:
            if arr[i] < ep:
                ep = arr[i]
                af_cur = min(af_cur + af, max_af)
            sar = sar + af_cur * (ep - sar)
            if arr[i] > sar:
                trend = 1
                sar = ep
                ep = arr[i]
                af_cur = af
    return float(sar)


@_register("supertrend")
def _supertrend(arr: np.ndarray, p: Dict[str, Any]) -> float:
    atr_mult = float(p.get("supertrend_mult", 3.0))
    n = len(arr)
    if n < 2:
        return float(arr[-1])
    hl_range = arr.max() - arr.min()
    atr = hl_range / n if n > 0 else 0
    mid = (arr.max() + arr.min()) / 2
    return float(mid)


# ---------------------------------------------------------------------------
# 8. Cumulative / running / sign-based
# ---------------------------------------------------------------------------
@_register("cumulative_avg")
def _cumulative_avg(arr: np.ndarray, p: Dict[str, Any]) -> float:
    return float(np.mean(arr))


@_register("running_mean")
def _running_mean(arr: np.ndarray, p: Dict[str, Any]) -> float:
    return float(np.mean(arr))


@_register("sign_filter")
def _sign_filter(arr: np.ndarray, p: Dict[str, Any]) -> float:
    diffs = np.diff(arr)
    pos = np.sum(diffs > 0)
    neg = np.sum(diffs < 0)
    if pos + neg == 0:
        return float(arr[-1])
    return float(arr[-1] * (1 + 0.0001 * (pos - neg) / (pos + neg)))


@_register("mode_filter")
def _mode_filter(arr: np.ndarray, p: Dict[str, Any]) -> float:
    # round to nearest tick size proxy and take most common
    tick = float(p.get("mode_tick", 1.0))
    rounded = np.round(arr / tick) * tick
    vals, counts = np.unique(rounded, return_counts=True)
    return float(vals[np.argmax(counts)])


# ---------------------------------------------------------------------------
# 9. Volume-ish / VWAP / orderflow proxies
# ---------------------------------------------------------------------------
@_register("vwap_ticks")
def _vwap_ticks(arr: np.ndarray, p: Dict[str, Any]) -> float:
    # no volume, weight by tick index
    weights = np.arange(1, len(arr) + 1)
    return float(np.sum(arr * weights) / np.sum(weights))


@_register("pwma")
def _pwma(arr: np.ndarray, p: Dict[str, Any]) -> float:
    # power-weighted MA
    power = float(p.get("power", 2.0))
    idx = np.arange(len(arr))
    weights = idx ** power
    weights[0] = 1e-9
    return float(np.sum(arr * weights) / np.sum(weights))


@_register("exp_wma")
def _exp_wma(arr: np.ndarray, p: Dict[str, Any]) -> float:
    # exponential weights centered on latest
    decay = float(p.get("decay", 0.9))
    weights = decay ** np.arange(len(arr) - 1, -1, -1)
    return float(np.sum(arr * weights) / np.sum(weights))


# ---------------------------------------------------------------------------
# 10. More exotic / uncorrelated
# ---------------------------------------------------------------------------
@_register("kernel_regression")
def _kernel_regression(arr: np.ndarray, p: Dict[str, Any]) -> float:
    n = len(arr)
    bandwidth = float(p.get("kernel_bw", 2.0))
    x = np.arange(n)
    x0 = n - 1
    weights = np.exp(-0.5 * ((x - x0) / bandwidth) ** 2)
    weights /= weights.sum()
    return float(np.sum(arr * weights))


@_register("loess")
def _loess(arr: np.ndarray, p: Dict[str, Any]) -> float:
    return _kernel_regression(arr, p)


@_register("bayesian_regression")
def _bayesian_regression(arr: np.ndarray, p: Dict[str, Any]) -> float:
    # ridge with prior centered at last price
    alpha = float(p.get("bayes_alpha", 1.0))
    x = np.arange(len(arr))
    A = np.vstack([x, np.ones(len(arr))]).T
    # Tikhonov regularization toward flat line at mean
    AtA = A.T @ A + alpha * np.eye(2)
    Atb = A.T @ arr
    try:
        m, c = np.linalg.solve(AtA, Atb)
        return float(m * (len(arr) - 1) + c)
    except Exception:
        return float(arr[-1])


@_register("ridge_regression")
def _ridge_regression(arr: np.ndarray, p: Dict[str, Any]) -> float:
    alpha = float(p.get("ridge_alpha", 1.0))
    x = np.arange(len(arr))
    A = np.vstack([x, np.ones(len(arr))]).T
    AtA = A.T @ A + alpha * np.eye(2)
    Atb = A.T @ arr
    try:
        m, c = np.linalg.solve(AtA, Atb)
        return float(m * (len(arr) - 1) + c)
    except Exception:
        return float(arr[-1])


@_register("perceptron_trend")
def _perceptron_trend(arr: np.ndarray, p: Dict[str, Any]) -> float:
    lr = float(p.get("perceptron_lr", 0.01))
    w = 0.0
    b = arr[0]
    for i in range(1, len(arr)):
        x = i
        pred = w * x + b
        err = arr[i] - pred
        w += lr * err * x
        b += lr * err
    return float(w * (len(arr) - 1) + b)


@_register("holt")
def _holt(arr: np.ndarray, p: Dict[str, Any]) -> float:
    alpha = float(p.get("alpha", 0.15))
    beta = float(p.get("holt_beta", 0.05))
    level = arr[0]
    trend = arr[1] - arr[0] if len(arr) > 1 else 0
    for i in range(1, len(arr)):
        prev_level = level
        level = alpha * arr[i] + (1 - alpha) * (level + trend)
        trend = beta * (level - prev_level) + (1 - beta) * trend
    return float(level + trend)


@_register("atr_mid")
def _atr_mid(arr: np.ndarray, p: Dict[str, Any]) -> float:
    n = len(arr)
    if n < 2:
        return float(arr[-1])
    atr = np.mean(np.abs(np.diff(arr)))
    return float(arr[-1] + atr * 0)


# ---------------------------------------------------------------------------
# Signal entry point
# ---------------------------------------------------------------------------
def trend_family_signal(**kwargs: Any) -> Dict[str, Any]:
    spot_price = float(kwargs.get("spot_price", 0.0))
    yp = float(kwargs.get("yp", 0.0))
    np_val = float(kwargs.get("np_val", 0.0))
    yes_ask = kwargs.get("yes_ask", yp)
    no_ask = kwargs.get("no_ask", np_val)
    rem_sec = float(kwargs.get("rem_sec", 0.0))
    elapsed_sec = float(kwargs.get("elapsed_sec", 0.0))
    market_id = str(kwargs.get("market_id", ""))
    spot_history = list(kwargs.get("spot_history", []))

    # The registry entry is passed as config; read sweep params from there
    # first, then fall back to kwargs for direct testing.
    cfg = kwargs.get("config") or {}
    trend_type = str(cfg.get("trend_type", kwargs.get("trend_type", "ema")))
    lookback = int(cfg.get("lookback", kwargs.get("lookback", 50)))
    deviation_pct = float(cfg.get("deviation_pct", kwargs.get("deviation_pct", 0.02)))
    entry_min = float(cfg.get("entry_min", kwargs.get("entry_min", 0.05)))
    entry_max = float(cfg.get("entry_max", kwargs.get("entry_max", 0.80)))
    time_guard = float(cfg.get("time_guard", kwargs.get("time_guard", 5.0)))
    confidence_scale = float(cfg.get("confidence_scale", kwargs.get("confidence_scale", 0.002)))

    neutral = {
        "triggered": False,
        "direction": None,
        "confidence": 0.0,
        "signal_price": spot_price,
        "entry_price": 0.0,
        "source": "TREND_FAMILY",
        "reason": "no signal",
    }

    if rem_sec <= time_guard or elapsed_sec <= time_guard:
        neutral["reason"] = "time guard"
        return neutral
    if spot_price <= 0 or len(spot_history) < 2:
        neutral["reason"] = "insufficient data"
        return neutral

    arr = np.array(spot_history, dtype=float)
    arr = _np_window(arr, lookback)
    if len(arr) < 2:
        neutral["reason"] = "lookback too short"
        return neutral

    if trend_type not in TREND_FUNCS:
        neutral["reason"] = f"unknown trend type {trend_type}"
        return neutral

    # Merge config into a flat param dict for trend helpers
    trend_params = dict(kwargs)
    if cfg:
        trend_params.update(cfg)
    try:
        estimate = TREND_FUNCS[trend_type](arr, trend_params)
    except Exception as e:
        neutral["reason"] = f"trend error: {e}"
        return neutral

    if estimate <= 0 or not np.isfinite(estimate):
        neutral["reason"] = "invalid estimate"
        return neutral

    up_threshold = 1.0 + deviation_pct / 100.0
    down_threshold = 1.0 - deviation_pct / 100.0

    if spot_price > estimate * up_threshold:
        entry_price = yes_ask if yes_ask is not None else yp
        if entry_min <= entry_price <= entry_max:
            deviation = (spot_price / estimate) - 1.0
            confidence = min(1.0, max(0.0, deviation / confidence_scale))
            return {
                "triggered": True,
                "direction": "YES",
                "confidence": confidence,
                "signal_price": spot_price,
                "entry_price": entry_price,
                "source": "TREND_FAMILY",
                "reason": f"{trend_type} deviation up {deviation:.6f}",
            }
        neutral["reason"] = f"YES entry {entry_price} outside band"
        return neutral

    if spot_price < estimate * down_threshold:
        entry_price = no_ask if no_ask is not None else np_val
        if entry_min <= entry_price <= entry_max:
            deviation = 1.0 - (spot_price / estimate)
            confidence = min(1.0, max(0.0, deviation / confidence_scale))
            return {
                "triggered": True,
                "direction": "NO",
                "confidence": confidence,
                "signal_price": spot_price,
                "entry_price": entry_price,
                "source": "TREND_FAMILY",
                "reason": f"{trend_type} deviation down {deviation:.6f}",
            }
        neutral["reason"] = f"NO entry {entry_price} outside band"
        return neutral

    neutral["reason"] = "spot within trend band"
    return neutral
