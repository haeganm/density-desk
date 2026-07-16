# Density Desk — closing note

*2026-07-16, ~00:40 ET. Written at shutdown, by the operator's instruction.*

## What this was

A fully automated Kalshi trading desk, built 2026-07-12 to 2026-07-16, that tested
three distinct edge hypotheses against real markets with real (small) money and
pre-committed, unmovable gates:

| # | Thesis | Data | Verdict |
|---|--------|------|---------|
| 1 | Price Kalshi BTC/ETH binaries off Deribit's options-implied density | free, public | Edge vs market: +3.0% Brier, **95% CI includes zero**. Realized −$0.048/contract ≈ exactly spread+fees. Structural: BTC ticks in ms, we quoted in 30–120s. |
| 2 | Price Kalshi daily-high weather off calibrated NOAA ensembles | free, public | **Market beats our model by 14.1% Brier at market open** (widening to 54.7% near close). Gate: EV +0.71¢ vs +1.5¢ bar, CI includes zero. |
| 3 | Price Kalshi S&P range off the SPX options/SPY tape via Unusual Whales | **paid** (UW Startup) | **β > 0 out-of-sample — Kalshi's MM is measurably slow** — but the drift (0.29–0.69¢) is ~6× below the cost bar (4.25¢). Retired at Phase 1, before any build. |

Full evidence: `docs/weather-edge-test-result.txt`, `docs/gate1-index-result.txt`,
`docs/market-structure.md`, and the git log, whose commit messages are the true
project journal.

## The result

**Kalshi is priced efficiently to within less than one tick across every venue
reachable with retail-accessible data, free or paid.** The one measurable
inefficiency found (index MM lag) is real and uncollectable at retail costs.

Total realized trading P&L across all strategies: **≈ −$6**. Final balance $23.84,
plus one crypto position (1× NO KXBTCD-26JUL1617-T65749.99, cost $0.75) that
settles server-side at 2026-07-16 21:00Z regardless of this shutdown.

## What was learned (the part worth keeping)

1. **Pre-commit the gate before looking.** Three times this converted an open-ended
   money sink into a cheap, definitive answer. The weather and index strategies
   never traded a cent.
2. **Never trust a P&L or edge number until the cash moves.** The netting bug
   booked two winning trades as −$4.04 and tripped a halt while the balance never
   moved a penny.
3. **Mids are not tradable. All-contract Brier is not edge. Unconditional
   calibration is not fill-conditioned calibration.** Every mirage this project
   chased dissolved under one of those three lamps.
4. **A rail an LLM can override is not a rail.** And a rail scoped to a strategy
   epoch can forgive losses; the floor must be account-level.
5. Verify unix timestamps with python; Kalshi journals buy-no as `action='sell'`;
   settlement `revenue` excludes netted pairs; `liquidity_dollars` lies; the exact
   settlement STATION matters more than the model.

## State at shutdown

- Scheduled tasks **disabled** (not deleted): DensityDesk-Desk / -Dashboard /
  -Operator. Re-enable with `schtasks /change /tn <name> /enable`.
- Processes stopped; ports 8777/8778 free. Desk was DISARMED throughout the last
  two strategies; the ARMED file does not exist.
- DBs: `state/archive/desk-crypto-final.db` (the full crypto book),
  `state/backtest_cache.db` (weather + index backtest data), `desk.db` (weather-era
  scan journal).
- Secrets: `secrets/kalshi.pem`, `secrets/uw_token.txt` (UW key is shared with
  `C:\unusual` — still in active use there; do NOT revoke).
- Windows Update pause expires 2026-07-22 on its own.
- 112 tests green at shutdown. `git checkout 821ee0a` restores the crypto desk.

## Reopening condition (pre-committed, one line)

A structural change only: Kalshi fee reduction, visible MM withdrawal (spreads
blowing out on KXINX), or genuinely proprietary data. Not a new model on the same
inputs. `scripts/gate1_leadlag.py` reruns the decisive test in ~20 minutes.
