# BTC Spot vs. Polymarket UP Cointegration Strategy (btc_up_coint)

## Overview
This strategy is a statistical arbitrage / relative-value options strategy that trades the spread between the underlying BTC spot price and the implied probability of the Polymarket contract. 

Since a Polymarket Up/Down contract resolves to $1.00$ or $0.00$ based on whether the spot price is above the strike at expiry, the contract is mathematically equivalent to a **binary call option** (or digital call). 

By applying options pricing theory, we calculate the theoretical probability of the option expiring in-the-money and compare it to the live contract trading price on Polymarket to exploit pricing spreads.

## Mathematical Model

1. **Theoretical Option Price (Black-Scholes Binary Call)**:
   $$\text{Price}_{\text{Theoretical}} = e^{-rT} N(d_2)$$
   Where:
   * $r$: Risk-free interest rate (assumed to be $0$ for short timeframes).
   * $T$: Time to expiry in years:
     $$T = \frac{\max(10, \text{rem\_sec})}{31,536,000}$$
   * $N(d_2)$: Standard cumulative normal distribution function:
     $$N(x) = \Phi(x) = \frac{1 + \text{erf}(x / \sqrt{2})}{2}$$
   * $d_2$ (probability factor):
     $$d_2 = \frac{\ln(S/K) - \frac{1}{2}\sigma^2 T}{\sigma \sqrt{T}}$$
   * $S$: Spot BTC price.
   * $K$: Contract strike price.
   * $\sigma$: Annualized volatility of BTC (assumed to be a rolling average or static baseline of $0.45$, i.e. $45\%$).

2. **Spread Calculation**:
   $$\text{Spread} = \text{Price}_{\text{Polymarket}} - \text{Price}_{\text{Theoretical}}$$
   * $\text{Price}_{\text{Polymarket}}$ is the live price of the YES contract.

## Strategy Rules

### Entry Rules
*   **Buy YES (Underpriced Contract)**:
    *   $\text{Spread} < -0.06$ (Polymarket YES is trading at a discount of $>6\%$ relative to theoretical odds).
    *   $\text{YES Contract Price} \le 0.80$.
*   **Buy NO (Overpriced Contract)**:
    *   $\text{Spread} > 0.06$ (Polymarket YES is trading at a premium of $>6\%$ relative to theoretical odds, meaning NO is underpriced).
    *   $\text{NO Contract Price} \le 0.80$.

## Parameters
*   `volatility`: 0.45 (annualized BTC volatility)
*   `spread_threshold`: 0.06 (6% pricing mismatch)
*   `max_price`: 0.80
