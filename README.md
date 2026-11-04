# Density Desk

An autonomous Kalshi trading desk that tested three edge hypotheses against real
markets with real (small) money and pre-committed gates. All three failed.
The desk was shut down on 2026-07-16 by its own measurements and is kept here
as the record of what was tested and why it died.

Total realized trading P&L across all strategies: about -$6. Two of the three
strategies never traded a cent, because the gate caught them first.

| # | Thesis | Data | Verdict |
|---|--------|------|---------|
| 1 | Price Kalshi BTC/ETH binaries off Deribit's options-implied density | free, public | +3.0% Brier vs market mid, 95% CI [-0.0012, +0.0101], includes zero. Realized -$0.048/contract, about exactly spread plus fees. Structural: BTC ticks in ms, the desk quoted every 30 to 120 s. |
| 2 | Price Kalshi daily-high weather off calibrated NOAA ensembles | free, public | Market beats the model by 14.1% Brier at market open, 54.7% near close. Gate: EV +0.71 cents vs a +1.5 cent bar, CI includes zero. Never armed. |
| 3 | Price Kalshi S&P range off the SPX options and SPY tape via Unusual Whales | paid | Beta > 0 out of sample, so Kalshi's market maker is measurably slow, but the capture (0.29 to 0.69 cents) is about 6x below the cost bar (4.25 cents). Retired before any build. |

The result: Kalshi is priced efficiently to within less than one tick across
every venue reachable with retail data, free or paid. The one real inefficiency
found (index market-maker lag) is uncollectable at retail costs.

Full closing note: [`docs/SHUTDOWN.md`](docs/SHUTDOWN.md). Evidence:
[`docs/weather-edge-test-result.txt`](docs/weather-edge-test-result.txt),
[`docs/gate1-index-result.txt`](docs/gate1-index-result.txt),
[`docs/market-structure.md`](docs/market-structure.md) (verified Kalshi API
facts), and the git log, whose commit messages are the project journal.

## What was learned

1. Pre-commit the gate before looking at the result. Three times this turned an
   open-ended money sink into a cheap, definitive answer.
2. Never trust a P&L or edge number until the cash moves. A netting bug booked
   two winning trades as -$4.04 and tripped a halt while the balance never moved.
3. Mids are not tradable. All-contract Brier is not edge. Unconditional
   calibration is not fill-conditioned calibration.
4. A rail an LLM can override is not a rail, and a rail scoped to a strategy
   epoch forgives losses. The floor must be account level.

## What is in here

`engine/` density math, scanner, maker quoting, executor and risk rails.
`kalshi/` signed REST client (V2 orders). `dashboard/` FastAPI dashboard with
an ARM/DISARM toggle. `ops/` operator brief, review and Windows scheduled
tasks. `scripts/` the edge tests and the backtests that produced the verdicts
above. `docs/` the verified market facts and the closing note.

Risk controls that stayed on for the whole life of the desk: $2 per order,
$10 per event, $12 total open exposure, a $5 daily realized-loss halt, a 65%
drawdown auto-disarm, a file-based kill switch, and arming only ever by a human
click on the dashboard.

## Run the tests

```
py -3.12 -m venv .venv
.venv\Scripts\pip install -e .
.venv\Scripts\python.exe -m pytest        # 112 tests, offline
```

The desk itself needs Kalshi API keys in `secrets/` (gitignored) and a funded
account. It is not recommended: see the table above.

## License

MIT.
