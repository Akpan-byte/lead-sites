# CHANGE_SUMMARY
# 2026-07-03  kilo
#   - Added tf_hint support and made the rem_sec window duration-aware, so the
#     signal works on 5m and 15m markets instead of only a fixed 45-120s slice.
#   - Lowered the pct_diff threshold from 0.03% to 0.01% and raised the price cap
#     from 0.80 to 0.85.
# WHY: heatmap_expiry_drift_15m had zero trades because the 45-120s window was
#      mismatched to market durations and the 0.03% drift threshold was too high
#      for the live spot feed.


def _duration_seconds(tf_hint):
    if tf_hint == "5m":
        return 300
    if tf_hint == "15m":
        return 900
    if tf_hint == "30m":
        return 1800
    if tf_hint == "1h":
        return 3600
    if tf_hint == "4h":
        return 14400
    if tf_hint == "1d":
        return 86400
    return 300


def heatmap_expiry_drift_15m_signal(spot_price, strike, yp, np_val, rem_sec, tf_hint="5m") -> dict:
    signal = {
        "triggered": False,
        "direction": None,
        "confidence": 0.0,
        "signal_price": 0.0,
        "entry_price": 0.0,
        "source": "HEATMAP_EXPIRY_DRIFT_15M",
        "reason": "",
    }
    duration = _duration_seconds(tf_hint)
    # Trade after the first 15s and until 5s before expiry, regardless of duration.
    if rem_sec < 15 or rem_sec > duration - 5:
        return signal
    pct_diff = abs(spot_price - strike) / strike if strike > 0 else 0.0
    if pct_diff <= 0.0001:
        return signal
    direction = "YES" if spot_price > strike else "NO"
    price = yp if direction == "YES" else np_val
    if price > 0.85:
        return signal
    signal["triggered"] = True
    signal["direction"] = direction
    signal["confidence"] = min(1.0, pct_diff / 0.001)
    signal["signal_price"] = price
    signal["entry_price"] = price
    signal["reason"] = (
        f"Heatmap expiry drift {direction}: rem_sec={rem_sec}, pct_diff={pct_diff:.4f}"
    )
    return signal
