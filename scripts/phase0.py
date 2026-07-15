"""Phase 0: ground truth discovery. Read-only, no keys. Dumps docs/phase0_raw.json + prints summary."""
import datetime
import json
import re
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
DERIBIT = "https://www.deribit.com/api/v2"
out = {"fetched_at_utc": datetime.datetime.now(datetime.UTC).isoformat()}

c = httpx.Client(timeout=30, headers={"User-Agent": "density-desk-phase0"})

# --- 1. Kalshi crypto series ---
series = []
cursor = None
while True:
    params = {"category": "Crypto", "limit": 200}
    if cursor:
        params["cursor"] = cursor
    r = c.get(f"{KALSHI}/series", params=params)
    r.raise_for_status()
    d = r.json()
    series += d.get("series", [])
    cursor = d.get("cursor")
    if not cursor:
        break
out["kalshi_series"] = series
btc_eth = [s for s in series if re.search(r"BTC|ETH", s.get("ticker", ""))]
print(f"Kalshi crypto series: {len(series)} total, {len(btc_eth)} BTC/ETH:")
for s in btc_eth:
    print(f"  {s['ticker']:<16} freq={s.get('frequency','?'):<12} fee_type={s.get('fee_type','?')} mult={s.get('fee_multiplier','?')} title={s.get('title','')[:60]}")

# --- 2. Sample open markets for the interesting series (strike spacing, settle times) ---
out["sample_markets"] = {}
for tick in [s["ticker"] for s in btc_eth]:
    r = c.get(f"{KALSHI}/markets", params={"series_ticker": tick, "status": "open", "limit": 100})
    if r.status_code != 200:
        continue
    mkts = r.json().get("markets", [])
    if not mkts:
        continue
    out["sample_markets"][tick] = mkts
    strikes = sorted(m.get("floor_strike") or m.get("cap_strike") or 0 for m in mkts)
    closes = sorted({m["close_time"] for m in mkts})
    spacing = min((b - a for a, b in zip(strikes, strikes[1:]) if b > a), default=0)
    types = {m.get("strike_type") or m.get("market_type", "?") for m in mkts}
    print(f"  {tick}: {len(mkts)} open, strike_types={types}, min spacing={spacing}, "
          f"closes={[cl[11:16] for cl in closes[:6]]}{'...' if len(closes) > 6 else ''}")

# --- 3. Deribit expiries ---
for cur in ("BTC", "ETH"):
    r = c.get(f"{DERIBIT}/public/get_instruments", params={"currency": cur, "kind": "option", "expired": "false"})
    r.raise_for_status()
    inst = r.json()["result"]
    exps = sorted({i["expiration_timestamp"] for i in inst})
    out[f"deribit_{cur}_expiries"] = exps
    print(f"Deribit {cur}: {len(inst)} options, next expiries: "
          + ", ".join(datetime.datetime.fromtimestamp(e / 1000, datetime.UTC).strftime("%m-%d %H:%MZ") for e in exps[:4]))
    r = c.get(f"{DERIBIT}/public/get_index_price", params={"index_name": f"{cur.lower()}_usd"})
    px = r.json()["result"]["index_price"]
    out[f"deribit_{cur}_index"] = px
    print(f"Deribit {cur} index: {px}")

# --- 4. Fee schedule PDF (best effort text scrape) ---
try:
    r = c.get("https://kalshi.com/docs/kalshi-fee-schedule.pdf", follow_redirects=True)
    r.raise_for_status()
    if not r.content.startswith(b"%PDF"):
        raise ValueError(f"response is not PDF ({r.headers.get('content-type', 'unknown')})")
    (ROOT / "docs" / "kalshi-fee-schedule.pdf").write_bytes(r.content)
    print(f"Fee schedule PDF saved ({len(r.content)} bytes)")
except Exception as e:
    print(f"Fee PDF fetch failed: {e}")

(ROOT / "docs" / "phase0_raw.json").write_text(
    json.dumps(out, indent=1, default=str), encoding="utf-8")
print("Wrote docs/phase0_raw.json")
