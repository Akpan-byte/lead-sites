import math


def snipe_signal(
    spot_price, strike, yp, np_val, rem_sec, v_t, std_v, velocity_history
) -> dict:
    """
    Returns signal dict with: triggered (bool), direction (YES/NO), confidence (float), signal_price (float),
    entry_price (float), source (str - SNIPE), reason (str)

    Extracted from FINAL_GOLDEN_BOT.py run_strategy_signals().
    Implements both ORACLE_SNIPING (10% edge) and GOLDEN_SNIPE (12% edge) logic.
    """

    # --- Logic from FINAL_GOLDEN_BOT.py ---

    # Core B-S probability model parameters (5m specific in source)
    vol = 0.00045
    pct_diff = (spot_price - strike) / strike
    # Source uses 300.0 for 5m timeframe: time_fraction = max(0.01, rem_sec / 300.0)
    time_fraction = max(0.01, rem_sec / 300.0)

    # Calculate z-score for the probability model
    z = pct_diff / (vol * math.sqrt(time_fraction))

    try:
        # B-S fair probability approximation using the source's coefficient (2.2)
        fair_p = 1.0 / (1.0 + math.exp(-2.2 * z))
    except (OverflowError, ZeroDivisionError):
        fair_p = 0.5

    signal = {
        "triggered": False,
        "direction": None,
        "confidence": 0.0,
        "signal_price": spot_price,
        "entry_price": 0.0,
        "source": "SNIPE",
        "reason": "",
    }

    # Signal Gate: rem_sec check (from run_strategy_signals loop)
    # Source: "if rem_sec <= 5 or rem_sec >= 295: continue"
    if rem_sec <= 5 or rem_sec >= 295:
        return signal

    # 1. GOLDEN SNIPE Check (12% edge)
    # Source: "if true_prob > yp + 0.12 and yp <= 0.80:"
    edge_yes = fair_p - yp
    edge_no = (1.0 - fair_p) - np_val

    if edge_yes > 0.12 and yp <= 0.80:
        signal["triggered"] = True
        signal["direction"] = "YES"
        signal["confidence"] = edge_yes
        signal["entry_price"] = yp
        signal["source"] = "SNIPE"
        signal["reason"] = (
            f"🎯 [GOLDEN SNIPE] Market YES underpriced ({yp:.2f} vs fair {fair_p:.2f})"
        )
        return signal
    elif edge_no > 0.12 and np_val <= 0.80:
        signal["triggered"] = True
        signal["direction"] = "NO"
        signal["confidence"] = edge_no
        signal["entry_price"] = np_val
        signal["source"] = "SNIPE"
        signal["reason"] = (
            f"🎯 [GOLDEN SNIPE] Market NO underpriced ({np_val:.2f} vs fair {1.0 - fair_p:.2f})"
        )
        return signal

    # 2. ORACLE_SNIPING Check (10% edge)
    # Source: "if fair_p > yp + 0.10 and yp <= 0.80:"
    if edge_yes > 0.10 and yp <= 0.80:
        signal["triggered"] = True
        signal["direction"] = "YES"
        signal["confidence"] = edge_yes
        signal["entry_price"] = yp
        signal["source"] = "ORACLE_SNIPING"
        signal["reason"] = (
            f"🎯 [ORACLE SNIPE] Market YES underpriced ({yp:.2f} vs fair {fair_p:.2f})"
        )
        return signal
    elif edge_no > 0.10 and np_val <= 0.80:
        signal["triggered"] = True
        signal["direction"] = "NO"
        signal["confidence"] = edge_no
        signal["entry_price"] = np_val
        signal["source"] = "ORACLE_SNIPING"
        signal["reason"] = (
            f"🎯 [ORACLE SNIPE] Market NO underpriced ({np_val:.2f} vs fair {1.0 - fair_p:.2f})"
        )
        return signal

    return signal
