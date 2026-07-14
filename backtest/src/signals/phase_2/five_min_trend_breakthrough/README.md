# Five-Minute Trend Breakthrough Strategy

## Overview
The Five-Minute Trend Breakthrough strategy is a high-confluence trend continuation strategy. It seeks to enter breakouts in the direction of the dominant trend, using a combination of moving average channels, momentum (RSI), and trend strength (ADX) filters.

## Mathematical Model

1. **Trend Anchor**:
   * **EMA 50**: Exponential Moving Average of close prices over 50 periods.
     $$\text{EMA}_{50, t} = \text{Close}_t \times \alpha + \text{EMA}_{50, t-1} \times (1 - \alpha), \quad \alpha = \frac{2}{51}$$

2. **Channel Triggers**:
   * **SMA 21 High**: 21-period Simple Moving Average of high prices.
   * **SMA 21 Low**: 21-period Simple Moving Average of low prices.

3. **Momentum Filter**:
   * **RSI 14**: 14-period Relative Strength Index.
     * Long threshold: $\text{RSI}_{14} > 60$
     * Short threshold: $\text{RSI}_{14} < 40$

4. **Trend Strength Filter**:
   * **ADX 14**: 14-period Average Directional Index (Wilder's formulation).
     * Trend strength: $\text{ADX}_{14} > 25$
     * Directional validation: $+\text{DI} > -\text{DI}$ (Long), $-\text{DI} > +\text{DI}$ (Short)

## Strategy Rules

### Entry Rules
*   **Buy YES (Bull Breakthrough)**:
    *   $\text{Spot Price} > \text{SMA}_{21}(\text{High})$
    *   $\text{Spot Price} > \text{EMA}_{50}$
    *   $\text{RSI}_{14} > 60$
    *   $\text{ADX}_{14} > 25$ and $+\text{DI} > -\text{DI}$
    *   `YES Contract Price` $\le 0.80$.
*   **Buy NO (Bear Breakthrough)**:
    *   $\text{Spot Price} < \text{SMA}_{21}(\text{Low})$
    *   $\text{Spot Price} < \text{EMA}_{50}$
    *   $\text{RSI}_{14} < 40$
    *   $\text{ADX}_{14} > 25$ and $-\text{DI} > +\text{DI}$
    *   `NO Contract Price` $\le 0.80$.

## Parameters
*   `ema_period`: 50
*   `sma_period`: 21
*   `rsi_period`: 14
*   `adx_period`: 14
*   `adx_threshold`: 25
*   `max_price`: 0.80
