# DENSITY DESK — Operator Handoff
*Written 2026-07-14 ~10:00 PT by Claude (Fable). Reader assumed to have ZERO context.*

## What this is
An autonomous trading desk at `C:\density desk` that prices Kalshi's BTC/ETH hourly/daily
binary markets (e.g. "BTC > $62,750 at 4am ET?") from the options-implied probability
density extracted off Deribit's free public options surface (Breeden-Litzenberger on
fitted smiles), then buys contracts whose Kalshi price disagrees with fair value by
more than fees + threshold. IOC taker orders, hold to settlement. All $0-cost
infrastructure; real money on a small Kalshi account.

## Current live state (verify, don't trust)
- **DISARMED after the R4 model/portfolio rebuild.** Re-arming remains a human action.
- Account cash changes with fills and settlements. Deposits are raw account equity,
  not strategy progress; Kelly sizing uses authenticated available cash.
- Dashboard: **http://127.0.0.1:8777** (Arasaka-styled; ARM/DISARM toggle top-left).
- Kill switches: dashboard ■ DISARM button (instant), or create file `C:\density desk\KILL`.

## The story so far (compressed)
1. Built + audited 3×; math core verified against Black-Scholes and live markets.
2. **First armed night (Jul 14, 00:12–06:45 PT): canonical scoped P&L was
   −$8.5043 through 06:45 PT (−$10.3838 over the full scoped order set).** Root cause found and
   proven: uniform √time variance scaling ignores intraday vol seasonality — quiet
   overnight hours (07–11Z) carry ~0.6× average variance, the 13Z US-data hour ~3.5×.
   Model predicted 45% avg P(win) on fills; reality delivered 15%, all in cheap tails.
   A small, correlated near-money subset looked profitable, but it was not enough
   evidence to validate the model.
3. **Round 3 rebuilt the model**: seasonal "vol-time" scaling (`engine/vol_profile.py`,
   hour-of-UTC-day weights from 90d hourly perp candles, refreshed daily with a
   48-hour stale grace in
   `state/vol_profile.json`), plus humility filters (never buy < 8¢; never trust
   fair > 2× price), per-event cap $10, drawdown auto-disarm at 65% of session-start
   MARKED equity, journal-before-send orders, paused-market/429 resilience.
4. **R4 rebuild:** model/market IQR must agree within `[2/3, 1.5]`; missing/thin
   ladders and profiles fail closed; catalyst checks cover the holding interval;
   Kelly targets subtract holdings with no round-up beyond quarter-Kelly; co-settling
   payoffs are evaluated jointly and must clear positive EV/log growth plus 0.55
   profit probability under both model and market distributions. Candidates rank by
   positive-day probability before raw edge. One prospective surface is stored per
   asset+settlement risk key, not once per correlated contract family.
5. `scripts/profit_forecast.py` is exact-version evidence only. It estimates no
   daily/weekly profit probability and remains insufficient until 30 filled event
   keys span 30 settlement timestamps and 14 UTC days.

## Architecture (one file each, all in C:\density desk)
- `run_desk.py` — main loop (scan/execute and fill/settlement sync have independent
  error boundaries; then status.json; 120s
  cycles, 30s when a settle < 2h; port-8778 bind = singleton; prunes DB every 6h).
- `engine/deribit.py` — options chain fetch. `engine/density.py` — smile fit → survival
  function (THE math). `engine/vol_profile.py` — seasonality weights + seasonal_tau.
- `engine/scanner.py` — enumerate markets, fair values, strict profile/quote checks,
  center+IQR regime guards, settlement-aware catalysts, and exhaustive event scenarios.
- `engine/executor.py` — target-position ¼-Kelly, joint event payoff/hit-rate checks,
  fill-time guard re-check, per-ticker/per-event caps, V2 IOC orders, and
  journal-before-send. `engine/rails.py` — proactive open-loss reserve plus daily
  realized loss halt ($5, scoped to
  STRATEGY_EPOCH orders), drawdown auto-disarm, exposure caps.
- `kalshi/client.py` — RSA-PSS signed REST; **fp/dollars string fields** (Kalshi
  migrated 2026; legacy cents fields dead); V2 orders = `POST /portfolio/events/orders`,
  yes-leg only (buy no = ask at 1−price); POST retries 429 ONLY (500 may have executed).
- `dashboard/` — FastAPI + single page; read-only except POST /api/arm.
- `ops/` — `gpt_sweep.py` (nightly 21:30 ChatGPT-in-Chrome catalyst calendar →
  `state/catalysts.json`; login profile in `state/chrome-profile`),
  `operator_review.py` (4-hourly `claude -p` review → `state/journal.md`),
  `install_tasks.py`, `run_dashboard.py`. `db.py` — SQLite journal (desk.db, WAL).
- `docs/market-structure.md` — ALL verified API facts + live-fire lessons. Read before
  touching anything API-adjacent.
- Tests: 117 passed at the R4 handoff; run the full `pytest` suite in repo root and
  KEEP GREEN. They catch real
  money-path bugs (fee float dust, a mistyped epoch that would have disabled a rail).

## Scheduled tasks (Windows, pythonw, no consoles)
DensityDesk-Desk & -Dashboard (5-min watchdogs; port binds make re-fires no-ops),
DensityDesk-Operator (4h), DensityDesk-GptSweep (daily 21:30).
**Restart procedure**: kill the pid holding port 8778 (desk) or 8777 (dashboard), then
`schtasks /end /tn DensityDesk-Desk` THEN `/run` (a stale "Running" state blocks /run).
Tasks are configured to start and remain running on battery. PC sleep is disabled on
both AC and battery power.

## Hard-won rules (violate at your peril)
1. **Never weaken a rail to trade sooner.** If a rail blocks legitimately-new
   circumstances, encode the principle (see STRATEGY_EPOCH in config.py) — don't
   raise limits or delete state files (the rails recompute and re-halt anyway).
2. **Verify every unix timestamp with python** — a hand-typed epoch 23h in the future
   silently disabled the loss rail once; only the test suite caught it.
3. **Log the body of every 4xx** — Kalshi pauses markets around macro prints; blind
   400s cost a night of forensics.
4. **Fills ≠ backtest**: quoted-price backtests are upper bounds; adverse selection
   historically ate ~60% of promised edge. Judge on realized fills only.
5. Kalshi API answers in fp-strings; the orderbook is `orderbook_fp`/`*_dollars`.
   Legacy int-cents fields are gone. Any new endpoint: check docs.kalshi.com first.
6. `pythonw` has no stdout — any new scheduled script must self-redirect (see
   run_desk.main / ops/run_dashboard.py pattern) or it dies silently.
7. DO NOT manually alter tickers the desk trades. Exact desk entry fills remain
   attributable, but a position mismatch makes that asset fail closed until reconciled.

## Open items for the next operator (in priority order)
1. Continue collecting exact-R4 evidence. Do not call the model confirmed before
   30 filled event keys span 30 settlement timestamps and 14 UTC days.
2. At those thresholds, perform fill-quality analysis; do not automatically introduce
   a replacement probability model or loosen risk rails automatically.
3. **Nightly 21:30**: confirm gpt_sweep succeeded (`state/gpt_sweep.log`, dashboard
   SWEEP event). The prior calendar has a 48-hour grace; beyond that the desk fails closed.
4. Maker quoting went live 2026-07-15 (`engine/maker.py`): post-only quotes one tick
   inside the spread when the maker-band edge clears MAKER_EDGE_MIN x catalyst factor;
   max MAKER_MAX_RESTING resting, one post per cycle, same joint gates and rails as
   taker; disarm/kill/halt/scan-failure cancels everything. Verify the first maker
   fills' fees (assumed ~0, modeled at 25% of taker). Still deferred: symmetric
   take-profit exits, funding decision.
5. Watch DB size (`desk.db`, prune keeps ~14d of snapshots ≈ 700MB steady-state cap).

## Where everything lives
Journal: `desk.db` (sqlite; legacy journal tables plus contracts/surfaces/outcomes/
order_evidence for exact R4 payoff and model attribution).
Live state: `state/status.json`, `state/ARMED` (content = session-start equity),
`state/catalysts.json`, `state/vol_profile.json`, `state/regime.json`, logs in `state/*.log`.
Secrets: `secrets/kalshi.pem` + `kalshi_key_id.txt` (gitignored). Plans/history: this
file + `docs/market-structure.md` + memory files in the Claude project.
