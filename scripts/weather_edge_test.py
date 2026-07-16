"""THE GATE. Does passive selling on Kalshi weather actually make money?

Run:  .venv\\Scripts\\python.exe scripts\\weather_edge_test.py [--series 20] [--refresh]

Pre-committed rule, fixed BEFORE looking at any result:

    passive-sell EV > +1.5c/contract AND the 95% CI clears zero
        -> the premise is real; arm small, NYC only.
    otherwise
        -> DO NOT ARM. Report and stop. No "one more tweak".

Read the limits before you read the numbers:

  * 68 EVENT-DAYS IS A HARD CEILING. Kalshi retains a rolling ~68-day window; every
    KXHIGH series returns exactly 408 settled markets / 68 events, 2026-05-09 ->
    2026-07-15. Older events return stubs. It is ALL SUMMER. There is no winter and
    there never will be, retroactively. The S3 archive goes back to 2021 but carries
    daily high/low only -- no bid/ask -- so it cannot support a maker fill model.
  * POOLING CITIES IS NOT 20x INDEPENDENT. A continental heat wave correlates
    NY/CHI/PHIL/DEN on the same day, so the bootstrap clusters BY DATE, not by
    city-day. Clustering per city-day would overstate significance exactly the way
    a naive read does.
  * THE FILL MODEL UNDERSTATES ADVERSE SELECTION. It cannot model our own book
    impact, or being picked off by a faster updater on a forecast revision. Real
    fills will be worse-selected than this.

An independent read of this same window found indiscriminate passive selling worth
+1.46c/contract assuming fill, but only +0.81c once conditioned on the fill model --
and the ENTIRE net edge lived in one 0.40-0.60 bucket (n=57, z=1.94). Strip that
bucket and it is -2.00c/contract. So the honest prior is that this gate FAILS. This
harness must reproduce that baseline; if it cannot, the harness is wrong.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import sqlite3
import statistics
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from engine import scanner  # noqa: E402
from engine.weather import TempDistribution, WeatherError, seasonal_params  # noqa: E402
from engine.weather import fair as model_fair  # noqa: E402
from scripts.fit_weather import CITIES, fetch_past_forecasts  # noqa: E402

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
CACHE = config.ROOT / "state" / "backtest_cache.db"
TICK = 0.01
REQ_PER_S = 4.0            # measured: >5.9/s eats 429s, and there are NO
                           # Retry-After or X-RateLimit headers to guide us

_last_call = [0.0]


def _get(client: httpx.Client, path: str, params: dict) -> dict:
    """Throttled GET with blind 429 backoff (Kalshi sends no rate-limit headers)."""
    for attempt in range(6):
        wait = 1.0 / REQ_PER_S - (time.time() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()
        r = client.get(KALSHI + path, params=params, timeout=30)
        if r.status_code == 429:
            time.sleep(2 ** attempt)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"rate limited out on {path}")


def _cache() -> sqlite3.Connection:
    CACHE.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(CACHE)
    con.execute("CREATE TABLE IF NOT EXISTS blob(k TEXT PRIMARY KEY, v TEXT)")
    con.commit()
    return con


def cached(con, key: str, produce):
    """Settled markets are immutable -> fetch once, ever."""
    row = con.execute("SELECT v FROM blob WHERE k=?", (key,)).fetchone()
    if row:
        return json.loads(row[0])
    value = produce()
    con.execute("INSERT OR REPLACE INTO blob VALUES(?,?)", (key, json.dumps(value)))
    con.commit()
    return value


def settled_markets(con, client, series: str) -> list[dict]:
    def pull():
        out, cursor = [], None
        while True:
            params = {"series_ticker": series, "status": "settled", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            page = _get(client, "/markets", params)
            out.extend(page.get("markets") or [])
            cursor = page.get("cursor")
            if not cursor or not page.get("markets"):
                return out
    return cached(con, f"markets:{series}", pull)


def candles(con, client, series: str, ticker: str, close_ts: float) -> list[dict]:
    def pull():
        return _get(client, f"/series/{series}/markets/{ticker}/candlesticks",
                    {"start_ts": int(close_ts - 48 * 3600),
                     "end_ts": int(close_ts), "period_interval": 60}
                    ).get("candlesticks") or []
    return cached(con, f"candles:{ticker}", pull)


def _f(d: dict | None, key: str) -> float | None:
    """Kalshi serves decimal-dollar STRINGS, and `price` is a VARIABLE-KEY object:
    on a zero-volume period it collapses to {"previous_dollars": ...} and
    high_dollars is ABSENT, not null. Naive d['price']['high_dollars'] raises."""
    if not d:
        return None
    v = d.get(key)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def d1_quote(cs: list[dict], close_ts: float) -> tuple[float, float] | None:
    """(yes_bid, yes_ask) from the candle nearest 24h before close."""
    target = close_ts - 24 * 3600
    best, best_d = None, 1e18
    for c in cs:
        ts = c.get("end_period_ts")
        if ts is None:
            continue
        d = abs(float(ts) - target)
        if d < best_d:
            best, best_d = c, d
    if best is None or best_d > 3600:
        return None
    bid = _f(best.get("yes_bid"), "close_dollars")
    ask = _f(best.get("yes_ask"), "close_dollars")
    if bid is None or ask is None or not 0 < bid <= ask < 1:
        return None
    return bid, ask


def filled(cs: list[dict], close_ts: float, price: float) -> bool:
    """Would a resting ask at `price`, posted at D-1, have been lifted?

    `price.high > price` STRICTLY. Justified, not assumed: price.high is present
    iff a trade occurred (0 of 408 zero-volume periods carry it) and trades never
    print above the ask (0 of 1,816). So a print strictly THROUGH a resting ask
    means the aggressor swept it -- we fill regardless of queue position. Equality
    is excluded on purpose: that is exactly where queue position decides.
    """
    after = close_ts - 24 * 3600
    for c in cs:
        ts = c.get("end_period_ts")
        if ts is None or float(ts) <= after:
            continue
        high = _f(c.get("price"), "high_dollars")
        if high is not None and high > price + 1e-9:
            return True
    return False


def build(series_limit: int) -> list[dict]:
    """One row per (event-day, leg) with the D-1 book, our model fair, and truth."""
    con, client = _cache(), httpx.Client()
    rows: list[dict] = []
    calib = json.loads((config.ROOT / "state" / "weather_calib.json").read_text())
    chosen = list(CITIES)[:series_limit]
    try:
        for city in chosen:
            cfg = CITIES[city]
            series = cfg["series"]
            city_cal = calib["cities"].get(city)
            if not city_cal:
                print(f"[{city}] no calibration fitted -- run fit_weather --cities all")
                continue
            markets = settled_markets(con, client, series)
            events: dict[str, list[dict]] = {}
            for m in markets:
                events.setdefault(m["event_ticker"], []).append(m)
            days = sorted(d for d in (scanner.event_date(e) for e in events) if d)
            if not days:
                continue
            fc = cached(con, f"fc:{city}", lambda: fetch_past_forecasts(
                cfg, days[0], days[-1], 1))
            print(f"[{series}] {len(events)} events {days[0]}..{days[-1]}, "
                  f"{len(fc)} archived forecasts")
            for ev, legs in sorted(events.items()):
                day = scanner.event_date(ev)
                point = fc.get(day.isoformat()) if day else None
                if point is None:
                    continue
                # Kalshi's own expiration_value IS the settled high (validated
                # 476/476 exact vs the station). Truth, straight from the source.
                truth = next((float(m["expiration_value"]) for m in legs
                              if m.get("expiration_value") is not None), None)
                if truth is None:
                    continue
                lead = city_cal["by_lead"].get("1")
                bias, sd = seasonal_params(lead, day)
                try:
                    d = TempDistribution.from_point(point, bias=bias, sd=sd)
                except WeatherError:
                    continue
                for m in legs:
                    close_ts = dt.datetime.fromisoformat(
                        m["close_time"].replace("Z", "+00:00")).timestamp()
                    cs = candles(con, client, series, m["ticker"], close_ts)
                    q = d1_quote(cs, close_ts)
                    if not q:
                        continue
                    bid, ask = q
                    f_yes = model_fair(d, m)
                    if f_yes is None:
                        continue
                    rows.append({
                        "series": city, "date": day.isoformat(), "ticker": m["ticker"],
                        "bid": bid, "ask": ask, "mid": (bid + ask) / 2,
                        "fair": f_yes, "won": 1 if m.get("result") == "yes" else 0,
                        "truth": truth,
                        "filled_at_ask": filled(cs, close_ts, ask - TICK),
                    })
    finally:
        client.close()
        con.close()
    return rows


def _boot(by_date: dict[str, list[float]], seed=0, n=20000):
    """Cluster bootstrap BY DATE: cities share synoptic regimes, and within one
    event exactly one leg wins, so legs are neither independent of each other nor
    across cities on the same day."""
    dates = list(by_date)
    if len(dates) < 5:
        return None
    rng = random.Random(seed)
    means = []
    for _ in range(n):
        pool = []
        for _ in range(len(dates)):
            pool.extend(by_date[dates[rng.randrange(len(dates))]])
        if pool:
            means.append(statistics.fmean(pool))
    means.sort()
    return means[int(0.025 * len(means))], means[int(0.975 * len(means))]


def _sell_pnl(r: dict) -> float:
    """Post at ask - 1 tick (what scanner._maker_candidate does); maker fee is $0.
    Selling yes at P: receive P, pay $1 if yes wins."""
    return (r["ask"] - TICK) - r["won"]


def report(rows: list[dict]) -> None:
    L = []
    P = L.append
    P("=" * 78)
    P("KALSHI WEATHER — PASSIVE-SELL EDGE TEST")
    P(f"rows: {len(rows)} legs | {len({r['date'] for r in rows})} distinct dates "
      f"| {len({r['series'] for r in rows})} series")
    P("LIMITS: 68-day rolling window, ALL SUMMER (2026-05-09..07-15), no winter ever.")
    P("        Cities correlate on synoptic regimes -> bootstrap clusters BY DATE.")
    P("        Fill model UNDERSTATES adverse selection (no book impact, no pick-off).")
    P("=" * 78)

    # 1. Does the model beat the market mid at all? Nothing else matters if not.
    n = len(rows)
    mb = sum((r["mid"] - r["won"]) ** 2 for r in rows) / n
    kb = sum((r["fair"] - r["won"]) ** 2 for r in rows) / n
    P("\nA. CALIBRATION AT D-1 (does the model beat the mid?)")
    P(f"   model Brier {kb:.4f} vs market-mid Brier {mb:.4f}  "
      f"-> {'model better' if kb < mb else 'MARKET BETTER'} by {(mb-kb)/mb*100:+.1f}%")

    # 2. Indiscriminate baseline -- the reference an independent read put at +0.81c
    P("\nB. INDISCRIMINATE PASSIVE SELL (no model; the baseline to beat)")
    for label, sel in (("assuming fill", lambda r: True),
                       ("conditioned on fill model", lambda r: r["filled_at_ask"])):
        sub = [r for r in rows if sel(r)]
        if not sub:
            continue
        pnl = [_sell_pnl(r) for r in sub]
        P(f"   {label:26} n={len(sub):5} mean {statistics.fmean(pnl):+.4f}/contract "
          f"total {sum(pnl):+.2f}")

    # 3. What production would ACTUALLY do
    P("\nC. MODEL-SELECTED PASSIVE SELL (what the desk would do)")
    sel_rows = []
    for r in rows:
        # sell yes = buy no at 1-(ask-tick); mirror scanner exactly
        side_fair = scanner._trusted_fair(1 - r["fair"], r["mid"], "no")
        px = round(1 - (r["ask"] - TICK), 2)
        if not config.MIN_BUY_PRICE <= px <= config.MAX_BUY_PRICE:
            continue
        if side_fair > config.MAX_FAIR_RATIO * px:
            continue
        if side_fair - px <= config.MAKER_EDGE_MIN:      # maker fee is $0
            continue
        sel_rows.append(r)
    for label, sel in (("assuming fill", lambda r: True),
                       ("conditioned on fill model", lambda r: r["filled_at_ask"])):
        sub = [r for r in sel_rows if sel(r)]
        if not sub:
            P(f"   {label:26} n=0 — the model selects nothing")
            continue
        pnl = [_sell_pnl(r) for r in sub]
        P(f"   {label:26} n={len(sub):5} mean {statistics.fmean(pnl):+.4f}/contract "
          f"total {sum(pnl):+.2f}")

    live = [r for r in sel_rows if r["filled_at_ask"]]
    by_date: dict[str, list[float]] = {}
    for r in live:
        by_date.setdefault(r["date"], []).append(_sell_pnl(r))
    ci = _boot(by_date)
    mean = statistics.fmean([_sell_pnl(r) for r in live]) if live else 0.0

    # 4. Is the whole edge one lucky cell?
    P("\nD. BY ASK BUCKET (is the edge one cell?)  [model-selected, filled]")
    P("   bucket        n   mean ask  realized  P&L/contract    total")
    buckets = ((0, .02), (.02, .05), (.05, .10), (.10, .20), (.20, .40),
               (.40, .60), (.60, 1.01))
    for lo, hi in buckets:
        sub = [r for r in live if lo <= r["ask"] < hi]
        if not sub:
            continue
        pnl = [_sell_pnl(r) for r in sub]
        P(f"   [{lo:.2f},{hi:.2f}) {len(sub):5}   {statistics.fmean([r['ask'] for r in sub]):.4f}"
          f"    {statistics.fmean([r['won'] for r in sub]):.4f}   {statistics.fmean(pnl):+.4f}"
          f"    {sum(pnl):+7.2f}")
    P("\n   LEAVE-ONE-BUCKET-OUT (a CI that survives on one cell is not evidence):")
    for lo, hi in buckets:
        rest = [r for r in live if not (lo <= r["ask"] < hi)]
        if len(rest) < 10 or len(rest) == len(live):
            continue
        pnl = [_sell_pnl(r) for r in rest]
        P(f"     drop [{lo:.2f},{hi:.2f}) -> n={len(rest):5} mean {statistics.fmean(pnl):+.4f}/contract")

    # 5. The pre-committed verdict
    P("\n" + "=" * 78)
    P("E. VERDICT (rule fixed BEFORE looking: EV > +1.5c AND 95% CI clears zero)")
    P(f"   model-selected, fill-conditioned: n={len(live)} over {len(by_date)} dates")
    P(f"   mean EV = {mean:+.4f}/contract ({mean*100:+.2f}c)")
    if ci:
        P(f"   95% CI (clustered by date) = [{ci[0]:+.4f}, {ci[1]:+.4f}]")
    else:
        P("   95% CI = insufficient distinct dates to bootstrap")
    passed = bool(ci and mean > 0.015 and ci[0] > 0)
    P("")
    if passed:
        P("   >>> PASS. EV clears +1.5c and the CI excludes zero.")
        P("   >>> Still summer-only: re-derive MIN/MAX_BUY_PRICE, arm SMALL, NYC only.")
    else:
        why = []
        if mean <= 0.015:
            why.append(f"EV {mean*100:+.2f}c does not clear the +1.50c bar")
        if ci and ci[0] <= 0:
            why.append("95% CI includes zero")
        if not ci:
            why.append("cannot bootstrap")
        P("   >>> DO NOT ARM: " + "; ".join(why) + ".")
        P("   >>> The rule was pre-committed. Do not re-tune it now. Report and stop.")
    P("=" * 78)
    print("\n".join(L))
    (config.ROOT / "state" / "weather_edge_test.txt").write_text("\n".join(L),
                                                                 encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", type=int, default=7,
                    help="how many cities to pool (max 20)")
    ap.add_argument("--refresh", action="store_true", help="drop the cache first")
    args = ap.parse_args()
    if args.refresh and CACHE.exists():
        CACHE.unlink()
    rows = build(args.series)
    if not rows:
        raise SystemExit("no rows built -- check calibration and cache")
    report(rows)


if __name__ == "__main__":
    main()
