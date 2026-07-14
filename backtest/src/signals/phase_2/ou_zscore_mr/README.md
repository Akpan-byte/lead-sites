# Ornstein-Uhlenbeck Z-Score Mean Reversion Strategy (ou_zscore_mr)

## Overview
The Ornstein-Uhlenbeck (OU) Z-Score strategy is the most mathematically rigorous mean reversion model. Unlike naive Z-score strategies that use simple rolling means (which can lag and fail during trends), this strategy calibrates a mean-reverting stochastic process (Ornstein-Uhlenbeck) to price deviation data to identify statistical extremes.

## Mathematical Model

1. **Stochastic Process**:
   $$dX_t = \theta (\mu - X_t) dt + \sigma dW_t$$
   Where:
   * $X_t = S_t - K$ is the price deviation from the strike $K$.
   * $\theta$: Speed of mean reversion.
   * $\mu$: Long-term mean deviation.
   * $\sigma$: Volatility of the process.
   * $dW_t$: Wiener process (Brownian motion).

2. **Discrete OLS Calibration**:
   We fit the historical bar close deviations to the discrete autoregressive model:
   $$x_i = a x_{i-1} + b + \epsilon_i$$
   Using Ordinary Least Squares (OLS) over the last 40 bars:
   * $a = e^{-\theta \Delta t} \implies \theta = -\ln(a)$ (for $\Delta t = 1$ period)
   * $b = \mu (1 - a) \implies \mu = \frac{b}{1 - a}$
   * $\text{Var}(\epsilon) = \text{Residual Variance}$
   * $\text{Half-life of reversion}: t_{1/2} = \frac{\ln(2)}{\theta}$

3. **Dynamic Z-Score**:
   The standardized deviation relative to its stationary variance:
   $$Z_t = \frac{x_t - \mu}{\sqrt{\frac{\text{Var}(\epsilon)}{1 - a^2}}}$$

## Strategy Rules

### Entry Rules
*   **Buy YES (Fade Down)**:
    *   The process is confirmed mean-reverting ($0 < a < 0.99$, indicating $\theta > 0$).
    *   $\text{Z-Score} < -2.0$ (spot price is significantly below the strike relative to OU volatility).
    *   `YES Contract Price` $\le 0.80$.
*   **Buy NO (Fade Up)**:
    *   The process is confirmed mean-reverting ($0 < a < 0.99$, indicating $\theta > 0$).
    *   $\text{Z-Score} > 2.0$ (spot price is significantly above the strike relative to OU volatility).
    *   `NO Contract Price` $\le 0.80$.

## Parameters
*   `lookback`: 40 (calibration window size)
*   `z_threshold`: 2.0
*   `max_price`: 0.80
