# Polymarket Adapted Opening Range Breakout (ORB v4)

## Overview
This strategy is an adaptation of the futures Opening Range Breakout (ORB v4) strategy (`ym_orb_v4.py`) modified for the structural constraints of Polymarket binary contracts.

Unlike futures contracts where trades can be held indefinitely and re-entered frequently, Polymarket has a hard expiry and a heavy 2% taker fee. Therefore, this adapted version enforces a **Time-Decay Gate** and a high-conviction breakout buffer to optimize trade execution.

## Strategy Rules

1. **Candle-Open Alignment**:
   The merged tick stream contains pre-candle drift (e.g. 5m rem up to ~600s, 15m rem up to ~1800s). The strategy ignores all ticks with `rem_sec > duration` and only starts the Opening Range once `rem_sec <= duration`.

2. **Opening Range (OR) Calculation**:
   * **5m timeframe**: first 15 seconds of the candle (`rem_sec > 285`).
   * **15m timeframe**: first 60 seconds of the candle (`rem_sec > 840`).
   * We record the highest high (`or_high`) and lowest low (`or_low`) of the spot price during this window.

3. **Trigger Levels**:
   Once the OR window closes:
   $$
   \text{Buy YES Trigger} = \text{or\_high} \times (1.0 + \text{buffer})
   $$
   $$
   \text{Buy NO Trigger} = \text{or\_low} \times (1.0 - \text{buffer})
   $$
   * **5m buffer**: `0.0007` (~0.07%, roughly one standard deviation of observed 5m BTC spot noise).
   * **15m buffer**: `0.0012` (~0.12%, roughly one standard deviation of observed 15m BTC spot noise).
   These values were chosen from the price distribution, not from optimizing backtest PnL.

4. **Entry Conditions**:
   * **Buy YES (Long Breakout)**: Spot price breaks above `Buy YES Trigger`, and `YES Contract Price` $\le 0.85$.
   * **Buy NO (Short Breakout)**: Spot price breaks below `Buy NO Trigger`, and `NO Contract Price` $\le 0.85$.

5. **Re-Entry** (optional):
   After a first breakout, if spot pulls back inside the OR range and then re-breaks in the same direction, up to `MAX_REENTRIES=3` additional entries are allowed. Position size scales down on each re-entry (`1.0x, 0.75x, 0.5x, 0.33x`).

6. **Time-Decay Gate**:
   To prevent entering trades too close to expiry where theta decay is extreme:
   * No entries allowed if `rem_sec < 90` (5m timeframe).
   * No entries allowed if `rem_sec < 240` (15m timeframe).

7. **No Stop-Loss**:
   Binary contracts resolve to $0.01 or $0.99 at expiry, so the maximum loss is bounded by the entry price.

## Parameters
* `TF_BUFFER`: `{"5m": 0.0007, "15m": 0.0012}`
* `max_entry_price`: `0.85`
* `5m_or_window`: `15s`
* `15m_or_window`: `60s`
* `5m_time_gate`: `90s`
* `15m_time_gate`: `240s`
* `MAX_REENTRIES`: `3`
* `MIN_COOLDOWN_TICKS`: `3`
