# CHANGE_SUMMARY
# 2026-07-03  kilo
#   - Replaced the hard-coded time_buf (295/895) with a duration-aware guard so
#     5m/15m/1h markets all trade in the interior (5s margin) instead of only the
#     last 5 minutes of 1h markets.
#   - Lowered basis threshold from 0.0005 to 0.0003 and z_score threshold from 0.5
#     to 0.3 to relax the entry filter.
#   - Raised entry-price cap from 0.75 to 0.85 to match the runner default.
# WHY: spot_perp_basis never traded because its time guard was backwards for 1h
#      markets and the combined basis/z-score filters were too strict for the
#      short-duration Polymarket feed.


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


def spot_perp_basis_signal(
    spot_price, strike, yp, np_val, rem_sec, z_score, tf_hint="5m", **kwargs
) -> dict:
    triggered = False
    direction = None
    confidence = 0.0
    entry_price = 0.0
    reason = ""

    duration = _duration_seconds(tf_hint)
    if rem_sec <= 5 or rem_sec >= duration - 5:
        return {
            "triggered": False,
            "direction": None,
            "confidence": 0.0,
            "signal_price": spot_price,
            "entry_price": 0.0,
            "source": "SPOT_PERP_BASIS",
            "reason": "Time guard",
        }

    basis = (spot_price - strike) / strike if strike > 0 else 0.0
    basis_abs = abs(basis)

    if basis_abs < 0.0003:
        return {
            "triggered": False,
            "direction": None,
            "confidence": 0.0,
            "signal_price": spot_price,
            "entry_price": 0.0,
            "source": "SPOT_PERP_BASIS",
            "reason": f"Basis too small ({basis:.4f})",
        }

    if basis > 0.0003 and z_score > 0.3 and np_val <= 0.85:
        triggered = True
        direction = "NO"
        confidence = min(0.85, basis_abs / 0.002)
        entry_price = np_val
        reason = f"Positive basis ({basis:.4f}) mean-reverting NO"
    elif basis < -0.0003 and z_score < -0.3 and yp <= 0.85:
        triggered = True
        direction = "YES"
        confidence = min(0.85, basis_abs / 0.002)
        entry_price = yp
        reason = f"Negative basis ({basis:.4f}) mean-reverting YES"

    return {
        "triggered": triggered,
        "direction": direction,
        "confidence": confidence,
        "signal_price": spot_price,
        "entry_price": entry_price,
        "source": "SPOT_PERP_BASIS",
        "reason": reason,
    }


__all__ = ["spot_perp_basis_signal"]
