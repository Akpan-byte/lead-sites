# Bollinger-Keltner Channel Squeeze Breakout Strategy (BBKC Squeeze)

## Overview
The BBKC Squeeze strategy identifies periods of low volatility (consolidation) and trades the explosive breakouts that follow. By comparing Bollinger Bands (representing asset volatility) to Keltner Channels (representing standard price range), we can mathematically define when volatility is unusually compressed (a "squeeze") and enter in the direction of the expansion.

## Mathematical Model

1. **Bollinger Bands**:
   * **Basis**: 20-period SMA of bar close prices
   * **Upper Band**: $\text{Basis} + 2.0 \times \text{StdDev}$
   * **Lower Band**: $\text{Basis} - 2.0 \times \text{StdDev}$

2. **Keltner Channels**:
   * **Basis**: 20-period SMA of bar close prices
   * **True Range (TR)**: $\max(\text{High} - \text{Low}, |\text{High} - \text{Prev\_Close}|, |\text{Low} - \text{Prev\_Close}|)$
   * **ATR (20)**: 20-period SMA of True Range
   * **Upper Channel**: $\text{Basis} + 1.5 \times \text{ATR}$
   * **Lower Channel**: $\text{Basis} - 1.5 \times \text{ATR}$

3. **Squeeze Condition**:
   * Active when Bollinger Bands contract inside the Keltner Channels:
     $$\text{StdDev} < 0.75 \times \text{ATR}$$

## Strategy Rules

### Entry Rules
*   **Buy YES (Long Breakout)**:
    *   Price has been in a **Squeeze** within the last 5 bars.
    *   Current spot price breaks above the **Bollinger Upper Band**.
    *   `YES Contract Price` $\le 0.80$.
*   **Buy NO (Short Breakout)**:
    *   Price has been in a **Squeeze** within the last 5 bars.
    *   Current spot price breaks below the **Bollinger Lower Band**.
    *   `NO Contract Price` $\le 0.80$.

## Parameters
*   `period`: 20 (Bollinger / Keltner Basis length)
*   `bb_mult`: 2.0 (Bollinger multiplier)
*   `kc_mult`: 1.5 (Keltner multiplier)
*   `max_price`: 0.80
