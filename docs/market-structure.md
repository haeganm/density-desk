# Kalshi weather markets — verified facts

Everything here was verified against the live API or Kalshi's own docs on
2026-07-15. Trust this file over any prior assumption. Re-verify before changing
anything API-adjacent.

## Series & structure

- One series per city. We trade **KXHIGHNY** only (NYC daily high).
- **Settlement**: "the highest temperature recorded in **Central Park, New York**
  ... as reported by the **National Weather Service's Climatological Report
  (Daily)**". An **integer °F**, on the **local calendar day**. Hence the model
  works in reported-integer space with a 0.5 continuity correction, and every
  forecast must be aggregated on `America/New_York`, not UTC.
- Events close ~04:59Z the following day; `expiration_time` is ~a week later.

## The partition (the single most important structural fact)

An event is an **exhaustive, mutually exclusive partition** of integer °F.
Live KXHIGHNY-26JUL16:

| ticker | strike_type | floor | cap | subtitle | pays on |
|---|---|---|---|---|---|
| T89 | less | – | 89 | "88° or below" | reported **< 89** |
| B89.5 | between | 89 | 90 | "89° to 90°" | **89 ≤ x ≤ 90** |
| B91.5 | between | 91 | 92 | "91° to 92°" | 91 ≤ x ≤ 92 |
| B93.5 | between | 93 | 94 | "93° to 94°" | 93 ≤ x ≤ 94 |
| B95.5 | between | 95 | 96 | "95° to 96°" | 95 ≤ x ≤ 96 |
| T96 | greater | 96 | – | "97° or above" | reported **> 96** |

Exactly one leg pays → **probabilities sum to exactly 1**. Events carry
`"mutually_exclusive": true`; 408 settled NY markets = 68 events = exactly 68
`result:"yes"`.

⚠️ **These semantics are NOT the crypto ones.** Crypto's continuous conventions
were `floor < x <= cap` for `between` and `x <= cap` for `less`. On weather that
misprices every `less` leg by a full degree and drops the bottom integer of every
bracket. See `engine/weather._wins`.

## Fees — identical to crypto, and maker is FREE

From the official Fee Schedule PDF (eff. 2026-07-07):
- Taker: `round_up(M × 0.07 × C × P × (1−P))`, **M defaults to 1**.
- Maker: `round_up(M × 0.0175 × C × P × (1−P))`, **M defaults to 0** — a maker fee
  applies only to series in the "Non-Standard Fees" table. **No `KXHIGH*` series
  appears there. No weather series appears at all.**
- "**There is no settlement fee.**"
- All **288** "Climate and Weather" series report `fee_type: quadratic,
  fee_multiplier: 1` — zero exceptions, byte-identical to KXBTC.
- ⚠️ Fee data lives **only on the series object**, never on `/markets`.

**$0 maker fee + a fair bid + a ~6¢ spread is the entire thesis**: a resting ask
sells above fair with no fee drag.

## What is NOT an edge

**Complete-set arbitrage does not exist.** Across 14 open events / 7 cities,
`sum(bids)` mean **0.987** — the bid side is already fair. The "overround" in the
mids is exactly half the six-leg spread and nothing more. And for any exhaustive
partition the taker cost of a set is `0.07 × (1 − Σpᵢ²)` — **structurally largest
exactly where the apparent overround is biggest**. 0/14 events clear; best case
−2.5¢. Several events have a leg with no bid at all: the set is unsellable.

## Liquidity

- 🚩 **`liquidity_dollars` is `"0.0000"` on every weather market.** Any depth gate
  reading it blocks every trade. Use `orderbook_fp` only.
- Depth is fine at our size: top-of-book $5–38 per leg, $20–125 across 3 levels on
  D+1 books. ~500× thinner than crypto — irrelevant at $25, fatal above ~$100/leg.
- **D+0 books go degenerate** (one-sided at 0.01/0.99 once the day's high is
  effectively known). **Trade D+1 and beyond only.**
- Events are `collateral_return_type: "MECNET"` — offsetting legs within one event
  net their collateral, so a gross-cost `PER_EVENT_CAP` overstates capital at risk.

## Forecast data (all free, no key)

- **Ensemble** — `ensemble-api.open-meteo.com/v1/ensemble`. Verified members:
  `gfs025` 31, `ecmwf_ifs025` 51, `icon_seamless` 40, `gem_global` 21.
  🚩 **Never `ecmwf_ifs04`** — deprecated, returns 1 member, would silently
  collapse the pooled spread. `timezone=America/New_York` is mandatory.
  Free tier is licensed **non-commercial** — revisit if this ever earns.
- **Archived forecasts** — `previous-runs-api.open-meteo.com/v1/forecast`,
  `temperature_2m_previous_day1..7`, back to ≥2023. **Hourly only** (the daily-max
  variables 400 out), so aggregate the local-day max yourself — which is *more*
  faithful to the NWS midnight-to-midnight day anyway.
- **No archived ENSEMBLE exists.** Per-member history returns 100% nulls. Fit the
  spread from the deterministic run's realised error; production pools a
  multi-model ensemble for the centre, which is strictly better — so the fit is
  conservative w.r.t. what we trade.
- **Actuals** — Iowa State IEM ASOS:
  `mesonet.agron.iastate.edu/cgi-bin/request/daily.py?network=NY_ASOS&stations=NYC`.
  🚩 **NEVER use Open-Meteo's ERA5 archive as "actual"**: +1.90°F bias vs KNYC,
  22% exact-integer agreement over 67 days. Buckets are 2°F wide — ERA5 is off by
  ~a full bucket, systematically warm, and would corrupt the fit invisibly.

## Model calibration (measured, KNYC, 2023-07→2026-07, n=1076)

- **Bias is strongly SEASONAL**: lead-1d runs **Mar −2.72°F → Jul +2.42°F**, a
  5.1°F swing = **2.5 buckets**. The annual mean (−0.51°F) is **2.41°F wrong every
  summer day** while annual PIT coverage still looks fine because seasons cancel.
  Fitted per month (±30d windows), interpolated by day-of-year.
- Lead-1d sd ≈ 3.45°F annual; summer ≈ 2.8°F.
- **Raw ensemble spread is underdispersed ~1.7×** (1.5–2.0°F vs realised 2.81°F).
  The ensemble may only ever **widen** our distribution, never narrow it.
- Inter-model disagreement (90.1→96.0°F) swamps intra-model spread (~1.6°F) →
  multi-model pooling is the cheap, principled fix.
- PIT coverage after the seasonal fit: all-year 54/82/95, Jun–Aug 55/81/95 vs
  nominal 50/80/95. Tails — where we would trade — are well calibrated.

## Kalshi API (unchanged from the crypto desk)

- fp-string fields (`*_dollars`, `*_fp`); legacy int-cents fields are gone.
- Orders V2 only: create `POST /portfolio/events/orders`, **cancel
  `DELETE /portfolio/events/orders/{id}`** (legacy path 410s), amend
  `POST /portfolio/events/orders/{id}/amend` (queue position is preserved only
  when size decreases).
- POST retries **429 only** — a 500 may have executed.
- **Netting**: offsetting yes/no lots pay $1/matched pair *at netting time*; the
  later settlement object then reports `revenue: 0` with both side costs
  populated. `pnl = revenue − costs − fees` books the whole basis as a loss. See
  `run_desk.sync_portfolio`. When a P&L number looks alarming, check whether the
  **cash actually moved** before believing it.
- History: candlesticks return **`yes_bid`/`yes_ask`** series, but market data
  starts **2026-05-09** (~68 event-days/city; older events return `markets: []`).
