import math


def get_spot_z_score(strike: float, spot: float, spot_history: list) -> float:
    if len(spot_history) < 20:
        return 0.0

    prices = list(spot_history)
    mean_price = sum(prices) / len(prices)
    variance = sum((p - mean_price) ** 2 for p in prices) / (len(prices) - 1)
    std_dev = math.sqrt(variance)

    if std_dev == 0:
        return 0.0

    return (spot - strike) / std_dev


def breakout_z_1_6_signal(
    spot_price, strike, z_score, rem_sec, yp=None, np_val=None
) -> dict:
    z_threshold = 1.6

    if rem_sec <= 5 or rem_sec >= 295:
        return {
            "triggered": False,
            "direction": None,
            "confidence": 0.0,
            "signal_price": spot_price,
            "entry_price": 0.0,
            "source": "BREAKOUT_Z_1.6",
            "reason": f"Time guard active: {rem_sec}s remaining",
        }

    triggered = False
    direction = None
    entry_price = 0.0
    confidence = 0.0
    reason = ""

    if z_score >= z_threshold:
        if yp is None or yp <= 0.80:
            triggered = True
            direction = "YES"
            entry_price = yp if yp is not None else 0.0
            conf = min(1.0, abs(z_score) / 3.0)
            reason = f"Breakout UP: Z-score {z_score:.2f} >= {z_threshold}"

    elif z_score <= -z_threshold:
        if np_val is None or np_val <= 0.80:
            triggered = True
            direction = "NO"
            entry_price = np_val if np_val is not None else 0.0
            conf = min(1.0, abs(z_score) / 3.0)
            reason = f"Breakout DOWN: Z-score {z_score:.2f} <= -{z_threshold}"

    return {
        "triggered": triggered,
        "direction": direction if triggered else None,
        "confidence": round(min(1.0, abs(z_score) / 3.0), 4) if triggered else 0.0,
        "signal_price": spot_price,
        "entry_price": entry_price,
        "source": "BREAKOUT_Z_1.6",
        "reason": reason,
    }
