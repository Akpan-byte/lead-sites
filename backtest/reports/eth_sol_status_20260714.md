# ETH/SOL Backtest Sweep — Status Report 2026-07-14

## Compute Integration Status

| Backend | Status | Detail |
|---|---|---|
| **Modal** | Active, ~90% done | 4 windows × ~116 batches: IS ETH 109/116, IS SOL 90/116, OOS ETH 116/116, OOS SOL 114/116 |
| **Render** | Live | `https://backtest-worker-9qxy.onrender.com` /health OK; keepalive cron every 10 min; smoke tests passed |
| **VM (3 cores)** | Running | 2-worker ETH IS local runner active; load currently moderate |
| **Laptop** | Sync active | SSH tar sync of backtest code seen in process list |
| **Beam / Lightning** | Jobs visible | Beam has 3 running + completed tasks; Lightning job inspect timed out but app exists |

## Phase 1 Results

Promotion criteria used: IS PnL > 0, OOS PnL > 0, maxDD < 25%, ≥50 trades, equity > $1.

### ETH
- **5 strategies pass crude criteria**, all from `daily_orb_v5` family:
  - `eth_phase_2.daily_orb_v5_1m_12re`
  - `eth_phase_2.daily_orb_v5_any_12re`
  - `eth_phase_2.daily_orb_v5_any_scale3`
  - `eth_phase_2.daily_orb_v5_any_scale5`
  - `eth_phase_2.daily_orb_v5_any_scaleunl`
- **However, quant suite reveals the edge is tiny and drawdown is high:**
  - PnL: +$3.35 to +$4.89 over 51 days on $200
  - Max DD: 24.5%–29.2%
  - Sharpe ≈ 0.002–0.005
  - PSR ≈ 0.52, DSR ≈ 0.49
  - Bootstrap 95% CI spans negative
- **Root cause:** the summary JSONs do not include `max_dd_pct` or `win_rate`, so the promotion script saw DD = 0 and approved strategies that would otherwise fail the 25% DD gate.
- **Conclusion:** no ETH strategy in this sweep is economically meaningful for live trading.

### SOL
- **0 strategies promoted.**
- Top IS performers lose heavily in OOS. Example:
  - `sol_phase_2.btc_up_coint`: IS +$91.52, OOS -$200.09
  - `sol_phase_2.daily_orb_v5_15m_5re`: IS +$49.95, OOS -$27.14
  - `phase_2.sol_orb_3m_5re`: IS +$46.93, OOS -$8.69
- Most SOL OOS runs show 0 trades or negative PnL.
- **Conclusion:** SOL 5m up/down markets are not profitable with the current 116-strategy set under taker fill.

## VPS Paper Trading Status

- `poly-orb-fade@*` systemd units are **not loaded** on `dublin-vps`.
- VPS load average is extremely high (39–54), so the instance is under severe stress.
- It is unclear whether paper traders were moved, renamed, or stopped. This needs investigation before any live deployment.

## Data Collectors

- Backups to `akpanbrain:` are healthy:
  - `poly_updown`: 22,731 files
  - `hyperliquid`: 15,946 files
  - `binance`: active, recent files present

## Recommended Next Steps

1. **Fix the promotion filter** to compute `max_dd_pct` and `win_rate` from trades if missing from summary JSON, then re-run promotion.
2. **Decide on ETH/SOL:** either abandon these markets for now or run a targeted diagnostic (data quality, OOS window length, reference klines) to explain the failure.
3. **Investigate VPS paper traders** — find why units are missing and whether the live stack is actually running.
4. **Proceed with BTC VWAP strategy design** (70 strategies) since BTC was the only market showing real edge.
5. **Do not deploy ETH/SOL live** until a strategy shows a real risk-adjusted edge.

## Files of Note

- Promotion outputs: `results/phase1/eth_promoted.json`, `results/phase1/sol_promoted.json`
- Quant outputs: `results/quant_taker_eth/leaderboard.md`
- Render worker: `/config/render_backtest_worker/`
