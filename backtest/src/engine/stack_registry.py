# CHANGE_SUMMARY
# 2026-07-02  kilo
#   - Created engine/stack_registry.py with 10 combined strategy stacks.
#   - Stacks map directly to engine.strategy_registry.STRATEGIES keys.
# WHY: Runners and systemd generators need a single source of truth for which strategies belong to each stack.

"""Combined-stack definitions for paper trading."""

from __future__ import annotations

from engine.strategy_registry import STRATEGIES

# ---------------------------------------------------------------------------
# Stack builders
# ---------------------------------------------------------------------------

_BREAKOUT = [
    "breakout_pct_003",
    "breakout_pct_004",
    "breakout_pct_006",
    "breakout_pct_008",
    "breakout_z_1_5",
    "breakout_z_1_6",
    "breakout_z_2_0",
    "breakout_z_3_0",
    "breakout",
]

_MR = [
    "mean_reversion",
    "mean_reversion_opposite_exit",
    "mean_reversion_z_1_5",
    "mr_gamma_expiry_pin",
    "mr_heatmap_liq_fade",
    "mr_l2_ofi_delta_fade",
    "heatmap_expiry_drift_15m",
    "phase_2.ou_zscore_mr",
    "phase_2.ou_mean_reversion",
    "phase_2.vwap_stretch_fade",
]

_VELOCITY = [
    "snipe",
    "kinetic_velocity_breakout",
    "liquidation_spot_gap_fade",
]

_MICROSTRUCTURE = [
    "l2_absorption_spread_collapse",
    "mr_l2_ofi_delta_fade",
    "ofi_momentum_bo_15m",
    "phase_2.cvd_divergence",
    "phase_2.absorption_reversal",
    "phase_2.ob_imbalance_fade",
    "phase_2.clob_mispricing",
    "phase_2.cross_exchange_drift",
    "phase_2.logit_weighted_ensemble",
    "phase_2.confluence_vote",
]

_ORB = [
    "phase_2.pm_orb_v4_reentry",
    "phase_2.pm_orb_v4_reentry_50",
    "phase_2.pm_orb_v4_reentry_none",
    "phase_2.five_min_trend_breakthrough",
    "phase_2.btc_orb_1m_1re",
    "phase_2.btc_orb_1m_5re",
    "phase_2.btc_orb_1m_12re",
    "phase_2.btc_orb_1m_50re",
    "phase_2.btc_orb_1m_unl",
    "phase_2.btc_orb_3m_1re",
    "phase_2.btc_orb_3m_5re",
    "phase_2.btc_orb_3m_12re",
    "phase_2.btc_orb_3m_50re",
    "phase_2.btc_orb_3m_unl",
    "phase_2.btc_orb_5m_1re",
    "phase_2.btc_orb_5m_5re",
    "phase_2.btc_orb_5m_12re",
    "phase_2.btc_orb_5m_50re",
    "phase_2.btc_orb_5m_unl",
    "phase_2.btc_orb_15m_1re",
    "phase_2.btc_orb_15m_5re",
    "phase_2.btc_orb_15m_12re",
    "phase_2.btc_orb_15m_50re",
    "phase_2.btc_orb_15m_unl",
    "phase_2.btc_orb_30m_1re",
    "phase_2.btc_orb_30m_5re",
    "phase_2.btc_orb_30m_12re",
    "phase_2.btc_orb_30m_50re",
    "phase_2.btc_orb_30m_unl",
    "phase_2.btc_orb_1h_1re",
    "phase_2.btc_orb_1h_5re",
    "phase_2.btc_orb_1h_12re",
    "phase_2.btc_orb_1h_50re",
    "phase_2.btc_orb_1h_unl",
]

_HYBRID_A = [
    "phase_2.btc_orb_5m_5re",  # ORB representative
    "breakout",                # breakout representative
    "mean_reversion",          # MR representative
    "snipe",                   # velocity representative
    "phase_2.ob_imbalance_fade",  # microstructure representative
]

_HYBRID_B = [
    "phase_2.btc_orb_5m_5re",
    "phase_2.btc_orb_15m_1re",
    "breakout_pct_006",
    "breakout_z_2_0",
    "mean_reversion_z_1_5",
    "mr_l2_ofi_delta_fade",
    "snipe",
    "kinetic_velocity_breakout",
    "phase_2.ob_imbalance_fade",
    "phase_2.absorption_reversal",
]

_HIGH_FREQUENCY = [
    "phase_2.btc_orb_1m_1re",
    "phase_2.btc_orb_1m_5re",
    "phase_2.btc_orb_1m_12re",
    "phase_2.btc_orb_1m_50re",
    "phase_2.btc_orb_1m_unl",
    "snipe",
    "kinetic_velocity_breakout",
    "phase_2.rsi2_connors",
    "l2_absorption_spread_collapse",
    "ofi_momentum_bo_15m",
    "liquidation_spot_gap_fade",
]

_CONSERVATIVE = [
    # Highest in-sample win-rate style selection (mean reversion, ORB 15m/1h, snipe, MR L2).
    "mean_reversion",
    "mean_reversion_z_1_5",
    "snipe",
    "phase_2.rsi2_connors",
    "phase_2.btc_orb_15m_1re",
    "phase_2.btc_orb_1h_1re",
    "phase_2.pm_orb_v4_reentry_50",
    "mr_l2_ofi_delta_fade",
    "breakout_pct_003",
]

_ALL_STAR = [
    # Best IS PnL style selection across categories.
    "phase_2.btc_orb_5m_5re",
    "phase_2.btc_orb_15m_5re",
    "snipe",
    "kinetic_velocity_breakout",
    "breakout",
    "mean_reversion",
    "phase_2.confluence_vote",
    "phase_2.logit_weighted_ensemble",
    "phase_2.cvd_divergence",
    "ofi_momentum_bo_15m",
]

# ---------------------------------------------------------------------------
# Diverse cross-category stacks — approved after live/backtest/trade audit.
# Each picks top 1-2 strategies from different categories for hedging and
# high trade frequency. Losers and 0-trade names are excluded.
# ---------------------------------------------------------------------------

_DIVERSE_01 = [
    "phase_2.ob_imbalance_fade",
    "snipe",
    "phase_2.btc_up_coint",
    "breakout_z_3_0",
    "mr_gamma_expiry_pin",
    "phase_2.btc_orb_5m_5re",
    "phase_2.five_min_trend_breakthrough",
    "phase_2.logit_weighted_ensemble",
]

_DIVERSE_02 = [
    "phase_2.btc_orb_1m_unl",
    "phase_2.btc_orb_3m_1re",
    "phase_2.btc_orb_5m_5re",
    "phase_2.logit_weighted_ensemble",
    "phase_2.confluence_vote",
    "l2_absorption_spread_collapse",
    "ofi_momentum_bo_15m",
    "kinetic_velocity_breakout",
]

_DIVERSE_03 = [
    "breakout_z_3_0",
    "breakout_z_1_6",
    "breakout_z_2_0",
    "mr_gamma_expiry_pin",
    "phase_2.bollinger_squeeze_release",
    "mean_reversion_z_1_5",
    "phase_2.btc_up_coint",
]

_DIVERSE_04 = [
    "snipe",
    "kinetic_velocity_breakout",
    "phase_2.btc_up_coint",
    "phase_2.five_min_trend_breakthrough",
    "phase_2.ob_imbalance_fade",
    "phase_2.volatility_breakout_gate",
]

_DIVERSE_05 = [
    "phase_2.ob_imbalance_fade",
    "phase_2.confluence_vote",
    "phase_2.logit_weighted_ensemble",
    "l2_absorption_spread_collapse",
    "phase_2.absorption_reversal",
    "phase_2.cvd_divergence",
    "ofi_momentum_bo_15m",
]

_DIVERSE_06 = [
    "phase_2.btc_orb_1m_1re",
    "phase_2.btc_orb_1m_5re",
    "phase_2.btc_orb_1m_unl",
    "phase_2.btc_orb_3m_1re",
    "phase_2.btc_orb_3m_5re",
    "phase_2.btc_orb_5m_1re",
    "phase_2.btc_orb_5m_5re",
    "phase_2.pm_orb_v4_reentry",
]

_DIVERSE_07 = [
    "phase_2.logit_weighted_ensemble",
    "phase_2.five_min_trend_breakthrough",
    "phase_2.btc_orb_3m_1re",
    "phase_2.btc_orb_5m_5re",
    "phase_2.btc_orb_1m_unl",
    "snipe",
    "mr_gamma_expiry_pin",
]

_DIVERSE_08 = [
    "snipe",
    "phase_2.ob_imbalance_fade",
    "phase_2.btc_up_coint",
    "breakout_z_3_0",
    "phase_2.confluence_vote",
    "phase_2.logit_weighted_ensemble",
    "mr_gamma_expiry_pin",
]

_DIVERSE_09 = [
    "phase_2.btc_up_coint",
    "phase_2.ob_imbalance_fade",
    "breakout_z_3_0",
    "mr_gamma_expiry_pin",
    "phase_2.five_min_trend_breakthrough",
    "phase_2.volatility_breakout_gate",
    "phase_2.btc_orb_15m_1re",
    "snipe",
]

_DIVERSE_10 = [
    "phase_2.btc_up_coint",
    "snipe",
    "phase_2.ob_imbalance_fade",
    "phase_2.confluence_vote",
    "mr_gamma_expiry_pin",
    "phase_2.five_min_trend_breakthrough",
    "breakout_z_3_0",
]

STACKS: dict[str, dict[str, Any]] = {
    "orb_stack": {
        "strategies": _ORB,
        "description": "Best opening-range-breakout variants",
    },
    "breakout_stack": {
        "strategies": _BREAKOUT,
        "description": "All breakout strategies",
    },
    "mean_reversion_stack": {
        "strategies": _MR,
        "description": "Mean-reversion family",
    },
    "microstructure_stack": {
        "strategies": _MICROSTRUCTURE,
        "description": "OFI / CVD / absorption / orderbook imbalance",
    },
    "velocity_stack": {
        "strategies": _VELOCITY,
        "description": "Kinetic / snipe / liquidation",
    },
    "hybrid_a": {
        "strategies": _HYBRID_A,
        "description": "Top 1 strategy per category",
    },
    "hybrid_b": {
        "strategies": _HYBRID_B,
        "description": "Top 2 strategies per category",
    },
    "high_frequency_stack": {
        "strategies": _HIGH_FREQUENCY,
        "description": "Short OR + snipe + velocity + connors",
    },
    "conservative_stack": {
        "strategies": _CONSERVATIVE,
        "description": "Highest in-sample win-rate",
    },
    "all_star_stack": {
        "strategies": _ALL_STAR,
        "description": "Best IS PnL across categories",
    },
    "diverse_01_core_cross_section": {
        "strategies": _DIVERSE_01,
        "description": "Top-1 per category for regime diversity",
    },
    "diverse_02_high_frequency": {
        "strategies": _DIVERSE_02,
        "description": "Short-cycle ORB + microstructure for max trade velocity",
    },
    "diverse_03_breakout_mr_hedge": {
        "strategies": _DIVERSE_03,
        "description": "Breakout-z variants hedged with mean-reversion",
    },
    "diverse_04_velocity_coint_trend": {
        "strategies": _DIVERSE_04,
        "description": "Fast velocity + slower cointegration/trend signals",
    },
    "diverse_05_microstructure": {
        "strategies": _DIVERSE_05,
        "description": "Concentrated order-flow edge stack",
    },
    "diverse_06_short_orb_swarm": {
        "strategies": _DIVERSE_06,
        "description": "Live-profitable 1m/3m/5m ORB grid",
    },
    "diverse_07_conservative_winrate": {
        "strategies": _DIVERSE_07,
        "description": "Highest live win-rate strategies for lower drawdown",
    },
    "diverse_08_aggressive_pnl": {
        "strategies": _DIVERSE_08,
        "description": "Pure top-decile live performers",
    },
    "diverse_09_cross_regime": {
        "strategies": _DIVERSE_09,
        "description": "Trend + breakout + MR + micro across regimes",
    },
    "diverse_10_quality_over_quantity": {
        "strategies": _DIVERSE_10,
        "description": "Highest PnL-per-trade selections",
    },
}


def validate_stack_registry() -> dict[str, list[str]]:
    """Return any missing strategy keys per stack. Empty dict == all valid."""
    missing: dict[str, list[str]] = {}
    for name, meta in STACKS.items():
        bad = [k for k in meta["strategies"] if k not in STRATEGIES]
        if bad:
            missing[name] = bad
    return missing


__all__ = ["STACKS", "validate_stack_registry"]
