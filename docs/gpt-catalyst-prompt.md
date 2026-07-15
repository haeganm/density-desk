# Nightly GPT 5.6 catalyst sweep (paste-ready)

Paste this into ChatGPT each evening; paste its JSON output over
`C:\density desk\state\catalysts.json`. The desk widens edge thresholds by
`factor` inside each window (2.0 = require double the usual edge; use 99 to
effectively stand down).

---

You are the risk calendar for an automated crypto probability-trading desk on
Kalshi (BTC and ETH settle markets, hourly and daily, next 48 hours). List every
scheduled or high-probability event in the next 48h that could cause an outsized
BTC/ETH move or regime break: FOMC decisions/minutes/speakers, CPI/PPI/NFP,
large option expiries, ETF flow anomalies, exchange incidents, hard forks,
major court/regulatory rulings, geopolitical escalations already in motion.
Use today's date and reliable sources; no speculation beyond scheduled items.

Output ONLY this JSON (unix seconds UTC; window = event time ±45 min unless the
event warrants more; factor 2.0 for routine data, 3.0+ for FOMC-grade):

{
  "windows": [
    {"label": "US CPI", "start_ts": 0, "end_ts": 0, "factor": 2.0}
  ]
}

If nothing qualifies, output {"windows": []}.
