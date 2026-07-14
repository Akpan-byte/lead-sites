# Larry Connors RSI(2) Mean Reversion Strategy

## Overview
The Larry Connors RSI(2) strategy is a professional-grade mean reversion strategy. It seeks to buy short-term oversold assets during an uptrend, and sell short-term overbought assets during a downtrend. 

By applying a long-term trend filter (200 SMA), this strategy prevents "fighting the trend" (catching falling knives or shorting vertical breakouts), which is the primary reason naive mean reversion strategies fail.

## Mathematical Model

1. **Long-Term Trend Filter**:
   $$\text{SMA}_{200} = \frac{1}{200} \sum_{i=0}^{199} \text{Close}_{t-i}$$
   
2. **Short-Term Momentum Oscillator (2-Period RSI)**:
   $$\text{RSI}_2 = 100 - \frac{100}{1 + \text{RS}}$$
   $$\text{RS} = \frac{\text{Wilder's Smoothed Average Gain}_2}{\text{Wilder's Smoothed Average Loss}_2}$$

## Strategy Rules

### Entry Rules
*   **Bull Trend (Buy YES / Fade Down)**:
    *   $\text{Spot Price} > \text{SMA}_{200}$ (on 5m/15m bars)
    *   $\text{RSI}_2 < 10.0$ (oversold condition)
    *   $\text{YES Contract Ask Price} \le 0.80$
*   **Bear Trend (Buy NO / Fade Up)**:
    *   $\text{Spot Price} < \text{SMA}_{200}$ (on 5m/15m bars)
    *   $\text{RSI}_2 > 90.0$ (overbought condition)
    *   $\text{NO Contract Ask Price} \le 0.80$

### Exit Rules
*   Standard binary expiry resolution (trades automatically hold until expiry or terminal FAK exits).

## Parameters
*   `period`: 2 (RSI length)
*   `trend_period`: 200 (SMA length)
*   `oversold_threshold`: 10
*   `overbought_threshold`: 90
*   `max_price`: 0.80

## Polymarket Integration
*   To prevent waiting 16 hours for 200 bars to accumulate at startup, the live trading module pre-populates its history using Coinbase's public REST candles API.
*   In backtesting, the signal function chronologically constructs bars from tick data.
