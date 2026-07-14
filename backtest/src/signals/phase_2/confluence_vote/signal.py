def confluence_vote_signal(
    spot_price,
    strike,
    yp,
    np_val,
    rem_sec,
    z_score,
    v_t,
    std_v,
    a_t,
    spread,
    tick_change,
    tf_hint="5m",
    **kwargs,
) -> dict:
    triggered = False
    direction = None
    confidence = 0.0
    entry_price = 0.0
    reason = ""

    time_buf = 895 if tf_hint == "15m" else 295
    if rem_sec <= 5 or rem_sec >= time_buf:
        return {
            "triggered": False,
            "direction": None,
            "confidence": 0.0,
            "signal_price": spot_price,
            "entry_price": 0.0,
            "source": "CONFLUENCE_VOTE",
            "reason": "Time guard",
        }

    if std_v <= 0:
        return {
            "triggered": False,
            "direction": None,
            "confidence": 0.0,
            "signal_price": spot_price,
            "entry_price": 0.0,
            "source": "CONFLUENCE_VOTE",
            "reason": "Zero std_v",
        }

    v_sigma = v_t / std_v if std_v > 0 else 0.0
    votes_yes = 0
    votes_no = 0

    if z_score < -1.5:
        votes_yes += 1
    elif z_score > 1.5:
        votes_no += 1

    if v_sigma > 1.5 and a_t < 0:
        votes_no += 1
    elif v_sigma < -1.5 and a_t > 0:
        votes_yes += 1

    if spread > 0.05 and z_score > 0:
        votes_no += 1
    elif spread > 0.05 and z_score < 0:
        votes_yes += 1

    total_votes = votes_yes + votes_no
    if total_votes >= 2:
        if votes_yes > votes_no and yp <= 0.75:
            triggered = True
            direction = "YES"
            confidence = votes_yes / (total_votes + 0.0)
            entry_price = yp
            reason = f"Confluence YES ({votes_yes}/{total_votes} indicators agree)"
        elif votes_no > votes_yes and np_val <= 0.75:
            triggered = True
            direction = "NO"
            confidence = votes_no / (total_votes + 0.0)
            entry_price = np_val
            reason = f"Confluence NO ({votes_no}/{total_votes} indicators agree)"

    return {
        "triggered": triggered,
        "direction": direction,
        "confidence": confidence,
        "signal_price": spot_price,
        "entry_price": entry_price,
        "source": "CONFLUENCE_VOTE",
        "reason": reason,
    }


__all__ = ["confluence_vote_signal"]
