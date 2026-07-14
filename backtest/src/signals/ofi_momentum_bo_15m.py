def ofi_momentum_bo_15m_signal(
    spot_price, strike, v_t, std_v, spread_val, yes_ask, no_ask, rem_sec, yp, np_val
) -> dict:
    signal = {
        "triggered": False,
        "direction": None,
        "confidence": 0.0,
        "signal_price": 0.0,
        "entry_price": 0.0,
        "source": "OFI_MOMENTUM_BO_15M",
        "reason": "",
    }
    if abs(v_t) <= 1.5 * std_v or spread_val > 0.04:
        return signal
    if v_t > 0:
        entry_price = yp if yp is not None else yes_ask
        if entry_price and entry_price <= 0.80:
            signal["triggered"] = True
            signal["direction"] = "YES"
            signal["confidence"] = (
                min(1.0, abs(v_t) / (2.0 * std_v)) if std_v > 0 else 0.5
            )
            signal["signal_price"] = spot_price
            signal["entry_price"] = entry_price
            signal["reason"] = (
                f"OFI momentum breakout YES: v_t={v_t:.2f}, spread={spread_val:.3f}"
            )
    elif v_t < 0:
        entry_price = np_val if np_val is not None else no_ask
        if entry_price and entry_price <= 0.80:
            signal["triggered"] = True
            signal["direction"] = "NO"
            signal["confidence"] = (
                min(1.0, abs(v_t) / (2.0 * std_v)) if std_v > 0 else 0.5
            )
            signal["signal_price"] = spot_price
            signal["entry_price"] = entry_price
            signal["reason"] = (
                f"OFI momentum breakout NO: v_t={v_t:.2f}, spread={spread_val:.3f}"
            )
    return signal
