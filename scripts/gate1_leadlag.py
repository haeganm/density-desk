"""GATE 1: does the index lead Kalshi's S&P range books at horizons we can act on?

Run:  .venv\\Scripts\\python.exe scripts\\gate1_leadlag.py [--days 90] [--refresh]

Pre-committed rule (fixed before looking):
    beta > 0 out-of-sample at h in {1,5,15} min AND the implied correction on ATM
    buckets exceeds (half-spread + taker fee + 1c adverse-selection allowance)
        -> the thesis survives; build the desk (Phase 2).
    otherwise -> stop, report, retire. No re-tuning after seeing the result.

Model under test (H1 from the research report):
    dKalshi_mid(t -> t+h) = alpha + beta * (fair(t) - mid(t)) + gamma * dSPX(t) + eps

where fair(t) is a CRUDE spot-implied bucket probability: Normal(spot_t, sigma*sqrt(tau)),
sigma from same-day realized vol (no lookahead), spot from SPY 1-min scaled by the
PRIOR day's settle/SPY ratio (no lookahead). Crude is the point: if Kalshi is slow,
even a crude fair moves first. If beta <= 0 against a crude fair, a fancy one will
not save the thesis -- the MM is faster than us and we are the adverse flow.

Data: Kalshi 1-min yes_bid/yes_ask candles (settled KXINX legs, quoted ATM only) x
UW SPY 1-min OHLC (90-day lookback on this tier; UW refuses OHLC for index tickers,
so SPY is the spot series). Everything cached in state/backtest_cache.db.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import statistics
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from scripts.weather_edge_test import _cache, cached, _f, _get  # noqa: E402

UW = "https://api.unusualwhales.com/api"
ET = ZoneInfo("America/New_York")
MON = {'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4, 'MAY': 5, 'JUN': 6,
       'JUL': 7, 'AUG': 8, 'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12}
HORIZONS = (1, 5, 15)            # minutes
TAKER_FEE_ATM = 0.0175           # 0.07*0.5*0.5 -- worst case, at P=0.5
ADVERSE_ALLOWANCE = 0.01


def uw_get(client, path, params):
    tok = (config.SECRETS / "uw_token.txt").read_text().strip()
    for attempt in range(5):
        r = client.get(UW + path, params=params, timeout=30,
                       headers={"Authorization": f"Bearer {tok}",
                                "Accept": "application/json"})
        if r.status_code == 429:
            time.sleep(1.5 * (attempt + 1))
            continue
        r.raise_for_status()
        body = r.json()
        return body.get("data") if isinstance(body, dict) and "data" in body else body
    raise RuntimeError(f"UW rate-limited out on {path}")


def event_day(event_ticker: str) -> dt.date | None:
    m = re.match(r"KXINX-(\d{2})([A-Z]{3})(\d{2})H1600$", event_ticker or "")
    if not m:
        return None
    try:
        return dt.date(2000 + int(m.group(1)), MON[m.group(2)], int(m.group(3)))
    except (KeyError, ValueError):
        return None


def settled_inx(con, client) -> dict[dt.date, list[dict]]:
    """Settled KXINX legs grouped by event day, quoted legs only (volume > 0)."""
    def pull():
        out, cursor = [], None
        while True:
            params = {"series_ticker": "KXINX", "status": "settled", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            page = _get(client, "/markets", params)
            out.extend(page.get("markets") or [])
            cursor = page.get("cursor")
            if not cursor or not page.get("markets"):
                return out
    markets = cached(con, "inx:settled", pull)
    by_day: dict[dt.date, list[dict]] = {}
    for m in markets:
        d = event_day(m.get("event_ticker", ""))
        vol = float(m.get("volume_fp") or m.get("volume") or 0)
        if d and vol > 0:
            by_day.setdefault(d, []).append(m)
    return by_day


def kalshi_candles(con, client, ticker: str, close_ts: float) -> list[dict]:
    def pull():
        return _get(client, f"/series/KXINX/markets/{ticker}/candlesticks",
                    {"start_ts": int(close_ts - 8 * 3600), "end_ts": int(close_ts),
                     "period_interval": 1}).get("candlesticks") or []
    return cached(con, f"kc1:{ticker}", pull)


def spy_minutes(con, client, day: dt.date) -> dict[int, float]:
    """SPY 1-min closes for one session, regular hours only, keyed by epoch-minute."""
    def pull():
        rows = uw_get(client, "/stock/SPY/ohlc/1m",
                      {"date": day.isoformat(), "limit": 1000}) or []
        return [{"t": r.get("start_time"), "c": r.get("close"),
                 "mt": r.get("market_time")} for r in rows]
    rows = cached(con, f"spy1m:{day.isoformat()}", pull)
    out = {}
    for r in rows:
        if r.get("mt") != "r" or not r.get("t") or r.get("c") is None:
            continue
        ts = dt.datetime.fromisoformat(r["t"].replace("Z", "+00:00")).timestamp()
        out[int(ts // 60)] = float(r["c"])
    return out


def bucket_bounds(m: dict) -> tuple[float, float] | None:
    st = m.get("strike_type")
    if st == "between":
        return float(m["floor_strike"]), float(m["cap_strike"])
    return None                    # tails excluded: they are never the quoted ATM legs


def fair_prob(spot: float, lo: float, hi: float, sigma_min: float,
              minutes_left: float) -> float | None:
    """Crude spot-implied P(lo <= close <= hi): Normal(spot, sigma*sqrt(tau))."""
    if minutes_left <= 0 or sigma_min <= 0:
        return None
    sd = sigma_min * math.sqrt(minutes_left)
    if sd <= 0:
        return None
    def phi(x):
        return 0.5 * (1 + math.erf(x / math.sqrt(2)))
    return phi((hi - spot) / sd) - phi((lo - spot) / sd)


def build_rows(days: int):
    con, client = _cache(), httpx.Client()
    by_day = settled_inx(con, client)
    sessions = sorted(by_day)[-days:]
    print(f"sessions with quoted settled legs: {len(sessions)} "
          f"({sessions[0]} .. {sessions[-1]})")
    obs = []          # one row per (leg, minute): mid, fair, spx, minute-key
    prev_ratio = None
    prev_close_spy = None
    prev_settle = None
    for day in sessions:
        legs = by_day[day]
        spy = spy_minutes(con, client, day)
        if len(spy) < 200:
            continue
        close_et = dt.datetime.combine(day, dt.time(16, 0), tzinfo=ET)
        close_min = int(close_et.timestamp() // 60)
        # scale SPY -> SPX with the PRIOR day's ratio (no lookahead)
        settle = next((float(str(m.get("expiration_value")).replace(",", ""))
                       for m in legs if m.get("expiration_value")), None)
        ratio = prev_ratio
        if prev_settle and prev_close_spy:
            ratio = prev_settle / prev_close_spy
        last_spy = spy.get(max(spy))
        if settle and last_spy:
            prev_settle, prev_close_spy = settle, last_spy
            prev_ratio = settle / last_spy
        if ratio is None:
            continue
        # same-day realized vol of the scaled spot, expanding window (no lookahead)
        mins = sorted(spy)
        spx = {k: spy[k] * ratio for k in mins}
        rets = {}
        prev_k = None
        for k in mins:
            if prev_k is not None and k - prev_k == 1:
                rets[k] = spx[k] - spx[prev_k]
            prev_k = k
        run: list[float] = []
        sigma_at: dict[int, float] = {}
        for k in mins:
            if k in rets:
                run.append(rets[k])
            if len(run) >= 30:
                sigma_at[k] = statistics.stdev(run[-120:])
        for m in legs:
            b = bucket_bounds(m)
            if not b:
                continue
            close_ts = dt.datetime.fromisoformat(
                m["close_time"].replace("Z", "+00:00")).timestamp()
            for c in kalshi_candles(con, client, m["ticker"], close_ts):
                ts = c.get("end_period_ts")
                if ts is None:
                    continue
                k = int(float(ts) // 60)
                if k not in spx or k not in sigma_at or k >= close_min - 16:
                    continue
                bid = _f(c.get("yes_bid"), "close_dollars")
                ask = _f(c.get("yes_ask"), "close_dollars")
                if bid is None or ask is None or not 0 < bid <= ask < 1:
                    continue
                f = fair_prob(spx[k], b[0], b[1], sigma_at[k], close_min - k)
                if f is None:
                    continue
                obs.append({"day": day.isoformat(), "ticker": m["ticker"], "k": k,
                            "mid": (bid + ask) / 2, "spread": ask - bid,
                            "fair": f, "spx": spx[k]})
    con.close(); client.close()
    return obs


def regress(rows, h):
    """OLS of dMid(t->t+h) on gap(t) and dSPX(t->t+h) with day-clustered walk-forward."""
    by_key = {(r["day"], r["ticker"], r["k"]): r for r in rows}
    days = sorted({r["day"] for r in rows})
    if len(days) < 20:
        return None
    split = days[int(len(days) * 0.6)]
    def sample(subset):
        X, Y = [], []
        for r in rows:
            if (subset == "train") != (r["day"] <= split):
                continue
            fut = by_key.get((r["day"], r["ticker"], r["k"] + h))
            if not fut:
                continue
            X.append((r["fair"] - r["mid"], fut["spx"] - r["spx"]))
            Y.append(fut["mid"] - r["mid"])
        return X, Y
    import numpy as np
    Xtr, Ytr = sample("train")
    Xte, Yte = sample("test")
    if len(Xtr) < 500 or len(Xte) < 500:
        return None
    A = np.column_stack([np.array(Xtr), np.ones(len(Xtr))])
    coef, *_ = np.linalg.lstsq(A, np.array(Ytr), rcond=None)
    beta, gamma, alpha = float(coef[0]), float(coef[1]), float(coef[2])
    # out-of-sample: does trading the gap direction capture anything?
    Ate = np.column_stack([np.array(Xte), np.ones(len(Xte))])
    pred = Ate @ coef
    yte = np.array(Yte)
    ss_res = float(((yte - pred) ** 2).sum())
    ss_tot = float(((yte - yte.mean()) ** 2).sum())
    # beta refit on TEST alone (the honest out-of-sample beta)
    coef_te, *_ = np.linalg.lstsq(Ate, yte, rcond=None)
    gaps = np.array([x[0] for x in Xte])
    # expected mid correction per unit gap, evaluated at the typical actionable gap
    big = np.abs(gaps) > 0.03
    capture = float(np.mean(np.sign(gaps[big]) * yte[big])) if big.sum() > 50 else None
    return {"h": h, "beta_train": beta, "beta_test": float(coef_te[0]),
            "gamma": gamma, "r2_test": 1 - ss_res / ss_tot if ss_tot else 0.0,
            "n_train": len(Xtr), "n_test": len(Xte),
            "capture_at_3c_gap": capture, "n_big_gaps": int(big.sum())}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    args = ap.parse_args()
    rows = build_rows(args.days)
    print(f"observations: {len(rows)} "
          f"({len({r['day'] for r in rows})} days, "
          f"{len({(r['day'], r['ticker']) for r in rows})} leg-days)")
    if not rows:
        raise SystemExit("no rows")
    spreads = [r["spread"] for r in rows]
    print(f"spread: median {statistics.median(spreads)*100:.1f}c "
          f"mean {statistics.fmean(spreads)*100:.1f}c")
    bar = statistics.median(spreads) / 2 + TAKER_FEE_ATM + ADVERSE_ALLOWANCE
    print(f"cost bar (half-spread + fee + adverse allowance): {bar*100:.2f}c\n")
    verdict_pass = False
    L = []
    for h in HORIZONS:
        res = regress(rows, h)
        if not res:
            L.append(f"h={h:2}m: insufficient data")
            continue
        cap = res["capture_at_3c_gap"]
        line = (f"h={h:2}m: beta train {res['beta_train']:+.3f} / TEST {res['beta_test']:+.3f}"
                f" | gamma {res['gamma']:+.4f} | R2 {res['r2_test']:.3f}"
                f" | capture@|gap|>3c {f'{cap*100:+.2f}c' if cap is not None else 'n/a'}"
                f" (n={res['n_big_gaps']})")
        L.append(line)
        if res["beta_test"] > 0 and cap is not None and cap > bar:
            verdict_pass = True
    print("\n".join(L))
    print("\n" + "=" * 74)
    if verdict_pass:
        print("GATE 1: PASS — the gap predicts Kalshi's correction beyond costs.")
        print("Proceed to Phase 2 (conversion). Gate 2 (live shadow) still required.")
    else:
        print("GATE 1: FAIL — no out-of-sample correction beyond costs at any horizon.")
        print("Pre-committed: stop, report, retire. No re-tuning.")
    print("=" * 74)
    (config.ROOT / "state" / "gate1_result.txt").write_text(
        "\n".join(L) + f"\nbar={bar:.4f}\nPASS={verdict_pass}\n", encoding="utf-8")


if __name__ == "__main__":
    main()
