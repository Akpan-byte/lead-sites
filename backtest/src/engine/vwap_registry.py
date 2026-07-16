# CHANGE_SUMMARY
# 2026-07-15  kilo
#   - Generated 70 VWAP-based strategy registry entries for BTC 5m UP/DOWN.
#   - Tuned for 5m market microstructure: BTC spot moves only ~0.1%/5m, so
#     BTC spot VWAP strategies use tight basis-point thresholds. PM midpoint
#     and orderbook strategies use wider thresholds because they move more.
# WHY: 5m BTC spot-VWAP deviations are tiny; PM price/book VWAP is where the
#      tradeable variance lives.

VWAP_STRATEGIES = {}

_BASE_PARAMS = [
    "spot_price", "yp", "np_val", "yes_ask", "no_ask", "rem_sec",
    "elapsed_sec", "duration_sec", "z_score", "spread",
    "orderbook_up", "orderbook_down", "yp_history", "np_history",
    "book_imbalance_val", "config",
]


def _add(name, mode, lookback=60, threshold=1.0, entry_max=0.85, **extra):
    cfg = {
        "module": "phase_2.vwap_factory",
        "fn": "vwap_factory_signal",
        "params": _BASE_PARAMS,
        "mode": mode,
        "lookback": lookback,
        "threshold": threshold,
        "entry_max": entry_max,
        "name": name,
    }
    cfg.update(extra)
    VWAP_STRATEGIES[name] = cfg


# ===================== A. BTC spot VWAP families (12) =====================
# BTC spot barely moves in 5m (~0.1% max range); use tight bp-level thresholds.
# A1. btc_vwap_fade (4) — threshold in percent (0.03% = 3bp)
for lb, th in [(20, 0.03), (30, 0.03), (30, 0.05), (60, 0.05)]:
    _add(f"phase_2.vwap_btc_fade_lb{lb}_th{int(th*1000)}", "btc_vwap_fade",
         lookback=lb, threshold=th)

# A2. btc_vwap_trend (4) — follow tiny VWAP crosses
for lb, conf in [(20, 2), (30, 2), (30, 3), (60, 2)]:
    _add(f"phase_2.vwap_btc_trend_lb{lb}_cf{conf}", "btc_vwap_trend",
         lookback=lb, threshold=0.03, confirmation=conf)

# A3. btc_vwap_breakout (4) — break micro bands
for lb, bn in [(20, 0.3), (30, 0.3), (30, 0.5), (60, 0.5)]:
    _add(f"phase_2.vwap_btc_break_lb{lb}_bn{int(bn*100)}", "btc_vwap_breakout",
         lookback=lb, band_n=bn)

# ===================== B. Polymarket price VWAP families (20) =====================
# PM midpoint moves ~10% vs its VWAP, so wider thresholds work.
# B1. pm_vwap_fade (6)
for lb, th in [(20, 1.0), (30, 1.0), (30, 1.5), (60, 1.5), (60, 2.0), (120, 2.5)]:
    _add(f"phase_2.vwap_pm_fade_lb{lb}_th{int(th*10)}", "pm_vwap_fade",
         lookback=lb, threshold=th, use_pm=True)

# B2. pm_vwap_trend (6)
for lb, conf in [(20, 2), (30, 2), (30, 3), (60, 2), (60, 3), (120, 3)]:
    _add(f"phase_2.vwap_pm_trend_lb{lb}_cf{conf}", "pm_vwap_trend",
         lookback=lb, threshold=1.0, confirmation=conf, use_pm=True)

# B3. pm_vwap_divergence (4) — BTC spot vs PM midpoint VWAP
for lb, th in [(30, 0.5), (60, 0.75), (120, 1.0), (180, 1.5)]:
    _add(f"phase_2.vwap_pm_div_lb{lb}_th{int(th*100)}", "pm_vwap_divergence",
         lookback=lb, threshold=th)

# B4. pm_vwap_momentum (4)
for lb, th in [(20, 1.0), (30, 1.5), (60, 2.0), (120, 2.5)]:
    _add(f"phase_2.vwap_pm_mom_lb{lb}_th{int(th*10)}", "pm_vwap_momentum",
         lookback=lb, threshold=th, use_pm=True)

# ===================== C. Orderbook / orderflow VWAP families (24) =====================
# C1. book_vwap_fade (6)
for lb, th in [(20, 0.5), (30, 0.75), (60, 1.0), (60, 1.5), (120, 1.5), (180, 2.0)]:
    _add(f"phase_2.vwap_book_fade_lb{lb}_th{int(th*100)}", "book_vwap_fade",
         lookback=lb, threshold=th, use_book=True)

# C2. book_imbalance_vwap (6) — threshold in tenths (e.g. 3 = 0.3 imbalance)
for lb, th in [(20, 2), (30, 3), (60, 3), (60, 4), (120, 5), (180, 6)]:
    _add(f"phase_2.vwap_book_imb_lb{lb}_th{th}", "book_imbalance_vwap",
         lookback=lb, threshold=th, use_book=True)

# C3. vwap_ofi (6)
for lb, th in [(20, 2), (30, 3), (60, 3), (60, 4), (120, 5), (180, 6)]:
    _add(f"phase_2.vwap_ofi_lb{lb}_th{th}", "vwap_ofi",
         lookback=lb, threshold=th, use_book=True)

# C4. vwap_ladder (6)
for lb, th in [(20, 0.5), (30, 0.75), (60, 1.0), (60, 1.5), (120, 1.5), (180, 2.0)]:
    _add(f"phase_2.vwap_ladder_lb{lb}_th{int(th*100)}", "vwap_ladder",
         lookback=lb, threshold=th)

# ===================== D. Cross-concept VWAP families (14) =====================
# D1. vwap_orb (4) — ORB confirmed by VWAP slope
for orb, lb in [(30, 20), (60, 30), (120, 60), (180, 60)]:
    _add(f"phase_2.vwap_orb_orb{orb}_lb{lb}", "vwap_orb",
         lookback=lb, orb_window=orb, threshold=1.0)

# D2. vwap_mr_combo (4)
for lb, th in [(20, 1.0), (30, 1.5), (60, 1.5), (120, 2.0)]:
    _add(f"phase_2.vwap_mrcombo_lb{lb}_th{int(th*10)}", "vwap_mr_combo",
         lookback=lb, threshold=th)

# D3. vwap_time_slice (3) — first half short lookback, second half long lookback
for short, long, th in [(20, 60, 0.75), (30, 120, 1.0), (60, 180, 1.5)]:
    _add(f"phase_2.vwap_timeslice_s{short}_l{long}_th{int(th*100)}", "vwap_time_slice",
         lookback=short, threshold=th, time_slice=(short, long))

# D4. vwap_regime_flip (3)
for lb, th in [(30, 0.75), (60, 1.0), (120, 1.5)]:
    _add(f"phase_2.vwap_regime_lb{lb}_th{int(th*100)}", "vwap_regime_flip",
         lookback=lb, threshold=th, flip_threshold=0.0001)


assert len(VWAP_STRATEGIES) == 70, f"expected 70 VWAP strategies, got {len(VWAP_STRATEGIES)}"
