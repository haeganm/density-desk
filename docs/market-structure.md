# Market structure — Phase 0 ground truth (verified live 2026-07-13 ~07:00Z)

## Live-fire lessons (first armed session, 2026-07-14 00:12–06:45 PT)

1. **Intraday vol seasonality is NOT optional.** Uniform √time scaling of daily
   implied variance overpriced quiet-hour tails ~2-4x in probability (model 45%
   avg P(win) on fills vs 15% realized; 5-15c bucket 21%→4%). 90d hourly perp data:
   07-11Z carries ~0.6x average variance, the 13Z US-data hour ~3.5x. Fixed via
   `engine/vol_profile.py` seasonal-time scaling. Near-money contracts (15c+) were
   *profitable* through the same night — the core engine is sound.
2. **Kalshi pauses crypto markets around macro prints.** CPI minute -> 26x
   `400 Bad Request` on order placement. Always log 4xx bodies; never let one
   rejected order abort the cycle; `cancel_order_on_pause: true`.
   Cancel is V2-only too: `DELETE /portfolio/events/orders/{order_id}` — the legacy
   `DELETE /portfolio/orders/{id}` returns `410 deprecated_v1_order_endpoint`
   (found live 2026-07-15 when the first maker cancels failed for 10 minutes).
3. **Rate budget is shared and bursty lookups 429.** Batch + space + backoff;
   POSTs retry only on 429 (a 500 may have executed — double-fill risk).
4. **Daily-loss rail must count desk-placed tickers only** — a manual trade
   settling the same UTC day masked −$6.70 of desk losses.
5. **Same-event strikes/sides are one correlated bet** — cap cost per event.
6. The former night replay is retired as evidence: it was partly in-sample,
   excluded unreconstructable contracts, and lacked the original option surfaces.
7. **Risk-neutral width is not physical probability.** R4 rejects a settlement when
   the model/market IQR ratio is outside `[2/3, 1.5]` and sizes the complete
   co-settling payoff rather than treating contracts as independent bets.

## Tradeable series (all settle on 60s average of CF Benchmarks RTI; `settlement_timer_seconds: 60`)

| Series | What | Structure | Strike spacing | Cadence |
|---|---|---|---|---|
| `KXBTCD` | BTC above/below | ladder of `strike_type: greater` binaries vs `floor_strike` | $100 | one event per hour (`KXBTCD-26JUL1304` = Jul 13, 04 EDT hour), opens ~1h before close |
| `KXBTC`  | BTC range | `between` buckets + `less`/`greater` tails | ~$100 | hourly |
| `KXETHD` | ETH above/below | same as KXBTCD | $10 (verify per event) | hourly |
| `KXETH`  | ETH range | same as KXBTC | ~$10 | hourly |

- Fair value of a `greater` contract = `P(settle > floor_strike)` = survival function at strike.
- `between` bucket = `CDF(cap) − CDF(floor)`; `less` = `CDF(floor)`.
- Settlement: simple average of 60 one-second BRTI (BTC) / ETH RTI prints in the final minute before the close hour. Expected expiration ~5 min after close.
- Prices: 1¢ ticks (`price_level_structure: linear_cent`), fields are dollar-strings (`yes_bid_dollars`, `yes_ask_dollars`, `no_bid_dollars`, `no_ask_dollars`).
- Old markets close each hour; scanner must re-enumerate open markets every cycle (don't hardcode tickers).
- Liquidity is hour-dependent; overnight books can be empty. Depth check before sizing is mandatory.

## Deribit alignment (verified)

- BTC: 874 live options, ETH: 696. Daily expiries at **08:00 UTC** exactly — matches the 4 AM EDT Kalshi settle to the second. All other hourly settles use total-variance interpolation between spot (t=0) and nearest expiry chain.
- Index at check: BTC $62,762.78, ETH $1,778.54.
- Basis caveat: Deribit's own index (deribit_price_index) ≠ CF Benchmarks RTI. Both are multi-exchange USD composites; expected basis small but must be logged (checks.py) and alarmed > 10bps.

## Fees (best available; VERIFY against first real fills in Phase 3 shakeout)

- Taker: `0.07 × p × (1−p)` per contract, order total rounded **up** to the next
  $0.0001 (centicent). Peak 1.75¢/contract at p=0.50.
- Maker: series API distinguishes `fee_type: quadratic` (our 4 series) vs `quadratic_with_maker_fees` (others). Strong signal our series charge **no maker fee**; one third-party source claims maker = 25% of taker universally.
  → Config models maker at 25% of taker (conservative). If shakeout fills show $0 maker fees, set `MAKER_FEE_FRACTION = 0.0` and thresholds tighten for free.
- `fee_multiplier: 1` on all four series (no crypto surcharge in API metadata).

## Audit addenda (verified live 2026-07-13)

- **Orderbook shape**: `/markets/{t}/orderbook` returns `{"orderbook_fp": {"yes_dollars": [["0.5500","5200.00"],...], "no_dollars": [...]}}` — resting BIDS per side as dollar/quantity STRING pairs, ascending price. There is no `orderbook.yes/no` cents form. Buying yes crosses the best no bid at `1 − price`.
- **Batch lookup**: `/markets?tickers=a,b,c` works — use it for settlement checks; never paginate `status=settled` series history (grows unboundedly). Settled markets report `status: "finalized"`, `result: "yes"|"no"` (empty string while open).
- **DST**: settles are defined at "4 AM **EDT**". In winter (EST) that's 09:00Z while Deribit expiries stay 08:00Z year-round. The code keys off the API `close_time` and `slice_for_settle` picks the next expiry ≥ settle, so it degrades gracefully to T_set/T_exp interpolation — do NOT "fix" the 08:00Z assumption.
- **Forward basis**: densities must center on the forward interpolated to the *settle* time (`index × (F_exp/index)^(T_set/T_exp)`), never the raw daily-expiry forward — at 20% annualized basis the difference is ~4¢ of fake edge on hourly ATM contracts. Guard: scanner drops an event's edges when model median vs market-implied center exceeds `BASIS_ALARM_BPS` (logged as `basis_alarm`).

## Fixed-point API migration (verified vs docs.kalshi.com 2026-07-13)

Kalshi moved money/count fields to strings: `*_dollars` (up to 6dp) and `*_fp` (2dp).
Legacy cents/int fields are deprecated (fills' `side`/`action` past their May-2026 date).
`kalshi/client.py::fp()/fpc()` parse new-first with legacy fallback.

- **Fills**: `fill_id`, `outcome_side`, `count_fp`, `yes/no_price_dollars`, `is_taker`,
  **`fee_cost` (real fees — no estimation needed)**. Dedup key = `fill_id`; rows without
  one are skipped (NULLs are distinct under SQLite UNIQUE).
- **Positions**: `position_fp`, `market_exposure_dollars` (aggregate cost),
  `fees_paid_dollars`, `realized_pnl_dollars`.
- **Settlements**: `revenue` (cents int), `yes/no_total_cost_dollars`, `fee_cost` →
  pnl computed entirely from the settlement object.
- **Orders**: V2 `POST /portfolio/events/orders`, yes-leg only: buy yes = `side:"bid"`
  @ price; buy no = `side:"ask"` @ (1−price). String price/count, required
  `time_in_force` + `self_trade_prevention_type`. Response: `fill_count`,
  `remaining_count`, `average_fill_price`, `average_fee_paid` (no `status` field).

**Shakeout checklist (first real fills):**
- ~~taker fee vs 0.07·p(1−p)~~ **VERIFIED 2026-07-14** against a real fill
  (KXWTI, 9.47 @ 0.51: model $0.1656 vs charged $0.1657 — multiplier and
  centicent rounding confirmed).
  Full pipeline reconciled to the cent vs account balance.
- maker fills expected $0 fee on quadratic-fee series — still unverified.
- orderbook_fp depth SECOND field unit assumed CONTRACTS — confirm on first partial fill.
- position `market_exposure_dollars` matches sum of fill costs — first desk position.

## API

- Base `https://api.elections.kalshi.com/trade-api/v2`. Public market data unauthenticated.
- Auth: RSA-PSS(SHA-256, MGF1-SHA256, salt=digest len) over `timestamp_ms + METHOD + /trade-api/v2/...path` (no query string); headers `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP`, `KALSHI-ACCESS-SIGNATURE` (base64).
- Rate limit ~600 req/min sustained; live budget at `GET /account/limits`.
- Raw discovery dump: `docs/phase0_raw.json` (scripts/phase0.py to refresh).
