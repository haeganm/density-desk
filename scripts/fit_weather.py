"""Fit forecast bias + error spread against the ACTUAL settling station.

Run:  .venv\\Scripts\\python.exe scripts\\fit_weather.py [--years 3]

Writes state/weather_calib.json -- the numbers engine/weather.py trades on.

Why this exists: the Open-Meteo grid cell is not the NWS gauge Kalshi settles on.
Measured for KNYC: bias +2.39 degF, WIDER THAN A WHOLE 2 degF BUCKET. And forecast
error sd is 2.81 degF vs a raw ensemble spread of only ~1.6 -- underdispersed 1.7x.
Uncorrected, the model screams false edges. This is not a nicety; it IS the model.

Data sources (both free, no key):
  forecasts -- previous-runs-api.open-meteo.com: what the forecast SAID on D-N.
               Hourly only (daily max variables 400 out), so we aggregate the
               local-day max ourselves -- which is actually more faithful to the
               NWS midnight-to-midnight local-day max Kalshi settles on.
  actuals   -- Iowa State IEM ASOS: the real station's max_temp_f.

NEVER use Open-Meteo's ERA5 archive as "actual": measured +1.90 degF bias and only
22% exact-integer agreement vs KNYC over 67 days. Kalshi buckets are 2 degF wide,
so ERA5 is off by ~a full bucket, systematically warm. It would corrupt this fit
silently and we would never know why the desk lost.

There is NO archived ensemble (per-member history returns nulls), so the spread is
fitted from the deterministic run's realised error. Production pools a multi-model
ensemble for the CENTER, which is strictly better than the deterministic run -- so
this fit is conservative w.r.t. what we actually trade.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import math
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from engine.weather import MAX_SD_F, MIN_SD_F  # noqa: E402

PREV_RUNS = "https://previous-runs-api.open-meteo.com/v1/forecast"
IEM = "https://mesonet.agron.iastate.edu/cgi-bin/request/daily.py"

# One city, done properly (operator decision 2026-07-15). Central Park = KNYC,
# the station named in the KXHIGHNY settlement rule.
CITIES = {
    "NYC": {"series": "KXHIGHNY", "lat": 40.7794, "lon": -73.9692,
            "tz": "America/New_York", "iem_network": "NY_ASOS", "iem_station": "NYC"},
}
LEADS = (1, 2, 3)          # forecast made D-1, D-2, D-3
SEASON_WINDOW_D = 30       # +/- days around each month centre, pooled across years
MIN_SEASON_N = 100         # refuse to fit a month on thin evidence


def _circ_days(a: int, b: int) -> float:
    """Distance between two days-of-year, wrapping at New Year."""
    d = abs(a - b) % 365
    return min(d, 365 - d)


def fetch_actuals(cfg: dict, start: dt.date, end: dt.date) -> dict[str, float]:
    """Observed integer daily highs from the real settling station."""
    r = httpx.get(IEM, params={
        "network": cfg["iem_network"], "stations": cfg["iem_station"],
        "year1": start.year, "month1": start.month, "day1": start.day,
        "year2": end.year, "month2": end.month, "day2": end.day,
        "format": "comma",
    }, timeout=90)
    r.raise_for_status()
    text = "\n".join(ln for ln in r.text.splitlines() if not ln.startswith("#"))
    out: dict[str, float] = {}
    for row in csv.DictReader(io.StringIO(text)):
        raw = (row.get("max_temp_f") or "").strip()
        if raw and raw not in ("None", "M"):
            try:
                out[row["day"]] = float(raw)
            except ValueError:
                pass
    return out


def fetch_past_forecasts(cfg: dict, start: dt.date, end: dt.date,
                         lead: int) -> dict[str, float]:
    """Local-day max of what the forecast said `lead` days before each date."""
    var = f"temperature_2m_previous_day{lead}"
    r = httpx.get(PREV_RUNS, params={
        "latitude": cfg["lat"], "longitude": cfg["lon"], "hourly": var,
        "temperature_unit": "fahrenheit", "timezone": cfg["tz"],
        "start_date": start.isoformat(), "end_date": end.isoformat(),
    }, timeout=120)
    r.raise_for_status()
    hourly = r.json().get("hourly") or {}
    times, vals = hourly.get("time") or [], hourly.get(var) or []
    by_day: dict[str, float] = {}
    for t, v in zip(times, vals):
        if v is None:
            continue
        day = t[:10]                       # already local: timezone= was honoured
        by_day[day] = max(by_day.get(day, -1e9), float(v))
    return by_day


def _fit(errs: list[float]) -> tuple[float, float]:
    n = len(errs)
    bias = sum(errs) / n
    var = sum((e - bias) ** 2 for e in errs) / (n - 1)
    return bias, max(MIN_SD_F, min(MAX_SD_F, math.sqrt(var)))


def fit_city(city: str, cfg: dict, years: int) -> dict:
    end = dt.date.today() - dt.timedelta(days=1)
    start = end - dt.timedelta(days=int(365.25 * years))
    print(f"[{city}] {start} -> {end}")
    actuals = fetch_actuals(cfg, start, end)
    print(f"[{city}] actual station days: {len(actuals)}")
    if len(actuals) < 200:
        raise SystemExit(f"{city}: only {len(actuals)} actuals -- refusing to fit")

    by_lead: dict[str, dict] = {}
    for lead in LEADS:
        fc = fetch_past_forecasts(cfg, start, end, lead)
        # (day-of-year, forecast - actual). Positive => grid runs hot vs the gauge.
        pairs = [(dt.date.fromisoformat(d).timetuple().tm_yday, fc[d] - actuals[d])
                 for d in fc if d in actuals]
        if len(pairs) < 200:
            print(f"[{city}] lead {lead}d: only {len(pairs)} pairs, skipped")
            continue
        errs = [e for _doy, e in pairs]
        bias_all, sd_all = _fit(errs)

        # SEASONAL fit -- the whole point. Measured for KNYC lead-1d: Mar -2.72F,
        # Jul +2.42F. A single annual bias (-0.51F) is 2.41F wrong in summer, i.e.
        # MORE THAN A WHOLE 2F BUCKET off on every trade we would place today,
        # while annual PIT coverage still looks fine because seasons cancel.
        by_month: dict[str, dict] = {}
        for m in range(1, 13):
            centre = dt.date(2001, m, 15).timetuple().tm_yday
            window = [e for doy, e in pairs if _circ_days(doy, centre) <= SEASON_WINDOW_D]
            if len(window) < MIN_SEASON_N:
                continue
            b, s = _fit(window)
            by_month[str(m)] = {"bias": round(b, 4), "sd": round(s, 4), "n": len(window)}
        if len(by_month) < 12:
            raise SystemExit(f"{city} lead {lead}d: only {len(by_month)}/12 months fitted")

        by_lead[str(lead)] = {
            "annual_bias": round(bias_all, 4), "annual_sd": round(sd_all, 4),
            "n": len(errs), "by_month": by_month,
            "mae": round(sum(abs(e) for e in errs) / len(errs), 4),
        }
        span = [by_month[str(m)]["bias"] for m in range(1, 13)]
        print(f"[{city}] lead {lead}d: n={len(errs)} annual bias={bias_all:+.2f}F "
              f"sd={sd_all:.2f}F | seasonal bias {min(span):+.2f} .. {max(span):+.2f}F")
    if not by_lead:
        raise SystemExit(f"{city}: no lead fitted")
    return {"station": cfg["iem_station"], "lat": cfg["lat"], "lon": cfg["lon"],
            "tz": cfg["tz"], "series": cfg["series"], "by_lead": by_lead}


def validate(city: str, cfg: dict, calib: dict, years: int) -> None:
    """PIT coverage: is the fitted Normal honest, or just fitted?

    A calibrated distribution puts ~x% of outcomes inside its x% interval. If the
    68% band only covers 55%, we are overconfident and will overprice the modal
    bucket -- exactly the failure that would bleed the account one bucket at a time.
    """
    from engine.weather import seasonal_params
    end = dt.date.today() - dt.timedelta(days=1)
    start = end - dt.timedelta(days=int(365.25 * years))
    actuals = fetch_actuals(cfg, start, end)
    lead = 1
    fc = fetch_past_forecasts(cfg, start, end, lead)
    par = calib["by_lead"].get(str(lead))
    if not par:
        return
    z = {0.5: 0.6745, 0.8: 1.2816, 0.95: 1.9600}
    # in-sample overall, plus a summer-only slice: seasonal miscalibration hides
    # in the annual average because winter and summer errors cancel
    for label, months in (("all year", None), ("Jun-Aug (what we trade)", {6, 7, 8})):
        hits = {0.5: 0, 0.8: 0, 0.95: 0}
        n = 0
        for d, f in fc.items():
            if d not in actuals:
                continue
            day = dt.date.fromisoformat(d)
            if months and day.month not in months:
                continue
            bias, sd = seasonal_params(par, day)
            n += 1
            for lvl, zz in z.items():
                if abs(actuals[d] - (f - bias)) <= zz * sd:
                    hits[lvl] += 1
        if not n:
            continue
        print(f"\n[{city}] PIT coverage, lead {lead}d, {label} (n={n}):")
        for lvl in (0.5, 0.8, 0.95):
            got = hits[lvl] / n
            flag = "OK" if abs(got - lvl) <= 0.05 else "MISCALIBRATED"
            print(f"    {lvl:.0%} interval -> {got:.1%}  {flag}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=3)
    args = ap.parse_args()
    out = {"computed_at": time.time(), "years": args.years, "cities": {}}
    for city, cfg in CITIES.items():
        calib = fit_city(city, cfg, args.years)
        out["cities"][city] = calib
        validate(city, cfg, calib, args.years)
    path = config.ROOT / "state" / "weather_calib.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(out, indent=1))
    print(f"\nwrote {path}")


if __name__ == "__main__":
    main()
