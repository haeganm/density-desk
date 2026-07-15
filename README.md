# Density Desk

Risk-controlled Kalshi BTC/ETH settle desk. Deribit's Breeden-Litzenberger density
is treated as a risk-neutral benchmark, then checked against Kalshi's center and
width before any event-level position is considered.

## Run

```
.venv\Scripts\python.exe run_desk.py                      # trading loop (scan-only until keys exist)
.venv\Scripts\python.exe -m uvicorn dashboard.app:app --port 8777   # dashboard -> http://127.0.0.1:8777
.venv\Scripts\python.exe test_density.py && .venv\Scripts\python.exe test_rails.py
.venv\Scripts\python.exe ops\install_tasks.py --yes       # schtasks: desk watchdog, dashboard, operator
```

## Go live

1. Kalshi account funded. Kelly sizing reads live available cash every cycle;
   deposits are raw account funding, never counted as strategy profit.
2. Account settings -> API keys -> create. Save private key as `secrets\kalshi.pem`,
   key id as the only line of `secrets\kalshi_key_id.txt`.
3. The desk is out of SHAKEOUT after verified live fills. Hard rails remain $2 per
   ticker/order, $10 per asset+settlement event, and $12 total open exposure.
   R4 also requires at least 0.55 modeled and market-implied probability that the
   complete co-settling position finishes profitable, positive EV/log growth under
   both distributions, and ranks candidates by that hit-rate objective before edge.

## Controls

- **Kill switch**: create file `KILL` in repo root (delete to resume). Disarm/kill/
  halt also cancels all resting maker quotes on the next cycle.
- Maker quoting (`engine/maker.py`): post-only quotes inside the spread for edges
  too small to cross with; MAKER_ENABLED/MAKER_MAX_RESTING in config.
- Daily realized loss limit $5 -> auto-halt until next UTC day (`state/HALT_UNTIL`).
- Account-equity charts include deposits; strategy progress is measured only from
  strategy-scoped settlement P&L. No forecast or audit guarantees profit.
- Catalyst windows in `state/catalysts.json` block settlements inside a window and
  widen thresholds for windows crossed during the holding period. Auto-filled
  nightly at 21:30 by `ops/gpt_sweep.py` (drives ChatGPT in a dedicated Chrome
  profile). One-time setup: `.venv\Scripts\python.exe ops\gpt_sweep.py --login`,
  sign into chatgpt.com, close the window. A failed refresh preserves the prior
  calendar, but an invalid/missing calendar or one older than 48 hours blocks orders.
  See `state/gpt_sweep.log`; manual paste via `docs/gpt-catalyst-prompt.md` still works.
- Operator brief: `state/brief.md`; Claude CLI verdicts append to `state/journal.md`.
- `scripts/profit_forecast.py` reports only exact-version, event-balanced evidence;
  it does not synthesize a daily or weekly profit probability.

## Layout

`engine/` density math + scanner + executor + rails · `kalshi/` signed REST client ·
`dashboard/` FastAPI + Arasaka UI · `ops/` brief, operator review, schtasks ·
`docs/market-structure.md` verified market facts · `desk.db` SQLite journal.
