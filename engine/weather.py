"""Calibrated daily-high-temperature distribution for Kalshi weather markets.

THE MODEL. Replaces the crypto Breeden-Litzenberger density; exposes the same
interface the scanner consumes (survival/bucket/median/K_grid/_D/repair_max).

Three measured facts drive every design choice here (KNYC, 2023-2026, n=1076):

  1. Bias is SEASONAL and bucket-sized: lead-1d runs Mar -2.72F to Jul +2.42F, a
     5.1F swing = 2.5 Kalshi buckets. The annual mean (-0.51F) is 2.41F wrong in
     summer. Trading raw forecast output screams false edges.
  2. Forecast error sd is ~2.8-3.4F against 2F buckets -- real uncertainty to price.
  3. Raw ensemble spread is underdispersed ~1.7x (1.5-2.0F vs realized 2.81F), so
     the member histogram is NOT a probability distribution.

CENTER = the DETERMINISTIC best_match forecast. This is the estimator
`scripts/fit_weather.py` measured bias/sd against, because previous-runs archives
only the deterministic run -- no ensemble history exists to fit. Centering on the
pooled ensemble mean instead (theoretically the better forecast) would wear a bias
correction never measured for IT, and at +-2.4F seasonal a 1F mismatch is half a
bucket. Consistency beats a better-but-uncalibrated estimator. The backtest
therefore validates exactly what production trades.

SPREAD = the fitted sd, WIDENED if the live ensemble is unusually dispersed. The
ensemble can only ever widen us, never narrow us, so it cannot manufacture a false
edge -- which is why it is safe to use despite its own bias being unmeasured.

Migration path: log ensemble-vs-actual forward until the ensemble mean's own bias
is measured, then switch the center on evidence (see `from_members`).

Kalshi settles on the NWS Climatological Report's INTEGER daily high, so this
whole module works in reported-integer space: `survival(K) = P(reported > K)`.
The latent continuous high is rounded, hence the 0.5 continuity correction.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import time

import httpx
import numpy as np

import config

CALIB_FILE = config.ROOT / "state" / "weather_calib.json"
ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"   # deterministic best_match

# Verified member counts 2026-07-15. NEVER add ecmwf_ifs04 -- it is deprecated
# and returns a single member, which would silently collapse the pool's spread.
MODELS = ("gfs025", "ecmwf_ifs025", "icon_seamless", "gem_global")

MIN_MEMBERS = 40          # pooled across models; below this the pool is degraded
MIN_SD_F = 1.0            # never claim more precision than this, whatever the fit
MAX_SD_F = 12.0
STATUS = "missing"        # module-level, mirrors the old vol_profile.STATUS slot


class WeatherError(RuntimeError):
    """Model input is unusable -> the caller must not quote."""


def _phi(z):
    """Standard normal CDF, vectorised, no scipy dependency."""
    return 0.5 * (1.0 + np.vectorize(math.erf)(np.asarray(z, dtype=float) / math.sqrt(2.0)))


def pooled_widen(members, typical_spread: float | None) -> float:
    """How much the live ensemble spread should WIDEN the fitted sd — never narrow it.

    A live spread wider than typical is a genuinely uncertain day and we should be
    less confident. A narrow one must NOT buy us confidence the measured error
    record does not support. Because this can only ever widen, the ensemble cannot
    manufacture a false edge — which is why it is safe to use even though the
    ensemble's own bias is unmeasured.
    """
    members = np.asarray([m for m in members if m is not None and math.isfinite(m)],
                         dtype=float)
    if members.size < 2 or not typical_spread or typical_spread <= 0:
        return 1.0
    spread = float(members.std(ddof=1))
    if not math.isfinite(spread):
        return 1.0
    return max(1.0, spread / typical_spread)


class TempDistribution:
    """Predictive distribution of a city's REPORTED integer daily high.

    Latent high ~ Normal(mu, sd); reported = round(latent). All public methods
    speak reported-integer space so the Kalshi partition sums to exactly 1.
    """

    def __init__(self, mu: float, sd: float, fetched_ts: float = 0.0,
                 spread_widen: float = 1.0, members=None):
        if not math.isfinite(mu) or not math.isfinite(sd) or sd <= 0:
            raise WeatherError(f"bad distribution: mu={mu} sd={sd}")
        self.mu = float(mu)
        self.sd = float(min(MAX_SD_F, max(MIN_SD_F, sd)))
        self.fetched_ts = fetched_ts
        self.spread_widen = spread_widen
        self.members = members

        # grid over reported integers, wide enough to hold ~all the mass
        lo = int(math.floor(self.mu - 6 * self.sd))
        hi = int(math.ceil(self.mu + 6 * self.sd))
        self.K_grid = np.arange(lo, hi + 1, dtype=float)
        self._D = self.survival(self.K_grid)
        self.repair_max = 0.0        # a normal CDF is monotone by construction

    @classmethod
    def from_point(cls, point: float, bias: float, sd: float,
                   fetched_ts: float = 0.0, spread_widen: float = 1.0):
        """Center from a DETERMINISTIC forecast — the estimator the calibration was
        actually fitted on (`scripts/fit_weather.py`) and the only one the archive
        can backtest. Production uses this too, so the backtest validates exactly
        what we trade."""
        if not math.isfinite(bias):
            raise WeatherError(f"bad calibration: bias={bias}")
        if point is None or not math.isfinite(point):
            raise WeatherError("non-finite forecast center")
        return cls(mu=point - bias, sd=sd * spread_widen, fetched_ts=fetched_ts,
                   spread_widen=spread_widen)

    @classmethod
    def from_members(cls, members, bias: float, sd: float, fetched_ts: float,
                     typical_spread: float | None = None):
        """Center from a POOLED ENSEMBLE mean.

        ⚠️ Only sound once the ensemble mean's OWN bias has been measured. It is a
        different estimator from the deterministic run the calibration was fitted
        on, and with bias swinging ±2.4°F seasonally a 1°F mismatch is half a
        bucket. Not used in production yet — kept for the forward A/B, and for the
        spread signal via `pooled_spread()`.
        """
        members = np.asarray([m for m in members if m is not None and math.isfinite(m)],
                             dtype=float)
        if members.size < MIN_MEMBERS:
            raise WeatherError(f"only {members.size} ensemble members (need {MIN_MEMBERS})")
        if not math.isfinite(bias) or not math.isfinite(sd) or sd <= 0:
            raise WeatherError(f"bad calibration: bias={bias} sd={sd}")
        widen = pooled_widen(members, typical_spread)
        d = cls(mu=float(members.mean()) - bias, sd=sd * widen,
                fetched_ts=fetched_ts, spread_widen=widen, members=members)
        return d

    def survival(self, K):
        """P(reported high > K). Reported = round(latent) -> P(latent > K+0.5)."""
        z = (np.asarray(K, dtype=float) + 0.5 - self.mu) / self.sd
        out = 1.0 - _phi(z)
        return float(out) if np.ndim(K) == 0 else out

    def bucket(self, floor: float, cap: float) -> float:
        """P(floor <= reported <= cap), inclusive both ends (Kalshi's 'X to Y')."""
        return float(self.survival(float(floor) - 1.0) - self.survival(float(cap)))

    def median(self) -> float:
        return float(self.mu)


def _wins(market: dict, reported: float) -> bool:
    """Does `market` pay YES on a REPORTED integer high?

    Kalshi's weather semantics, verified against live subtitles 2026-07-15:
      greater floor=96 -> "97 or above"  -> reported > 96
      between 89..90   -> "89 to 90"     -> 89 <= reported <= 90  (inclusive!)
      less    cap=89   -> "88 or below"  -> reported < 89         (strict!)
    The crypto scanner used `floor < x <= cap` / `x <= cap`, which are the
    CONTINUOUS conventions -- they drop the bottom of every bracket and misprice
    every `less` leg by a full degree.
    """
    st = market.get("strike_type")
    if st == "greater":
        return reported > float(market["floor_strike"])
    if st == "between":
        return float(market["floor_strike"]) <= reported <= float(market["cap_strike"])
    if st == "less":
        cap = market.get("cap_strike")
        return reported < float(cap if cap is not None else market["floor_strike"])
    return False


def fair(d: TempDistribution, market: dict) -> float | None:
    """Model probability this market resolves YES. Mirrors _wins exactly."""
    st = market.get("strike_type")
    if st == "greater":
        return d.survival(float(market["floor_strike"]))
    if st == "between":
        return d.bucket(float(market["floor_strike"]), float(market["cap_strike"]))
    if st == "less":
        cap = market.get("cap_strike")
        cap = float(cap if cap is not None else market["floor_strike"])
        return 1.0 - d.survival(cap - 1.0)      # P(reported < cap)
    return None


def load_calibration() -> dict:
    """Fitted bias/sd by city and lead day. Missing/stale -> caller fails closed."""
    global STATUS
    try:
        data = json.loads(CALIB_FILE.read_text())
        cities = data["cities"]
    except (OSError, ValueError, KeyError, TypeError):
        STATUS = "missing"
        return {}
    age = time.time() - float(data.get("computed_at", 0))
    STATUS = "fresh" if age <= 30 * 86400 else "stale"
    return cities


_MONTH_CENTRE = {m: dt.date(2001, m, 15).timetuple().tm_yday for m in range(1, 13)}


def _circ(a: float, b: float) -> float:
    d = abs(a - b) % 365
    return min(d, 365 - d)


def seasonal_params(lead: dict, target: dt.date) -> tuple[float, float]:
    """Bias/sd for a date, interpolated between the two nearest month centres.

    The bias is strongly seasonal -- measured KNYC lead-1d: Mar -2.72F, Jul +2.42F,
    a 5.1F swing (2.5 buckets). The annual mean (-0.51F) is 2.41F wrong in summer.
    Interpolating keeps the curve smooth instead of jumping at month boundaries.
    """
    by_month = lead.get("by_month") or {}
    if not by_month:
        raise WeatherError("calibration has no seasonal fit")
    doy = target.timetuple().tm_yday
    near = sorted(_MONTH_CENTRE, key=lambda m: _circ(doy, _MONTH_CENTRE[m]))[:2]
    (m1, m2) = near
    p1, p2 = by_month.get(str(m1)), by_month.get(str(m2))
    if p1 is None or p2 is None:
        raise WeatherError(f"calibration missing month {m1 if p1 is None else m2}")
    d1, d2 = _circ(doy, _MONTH_CENTRE[m1]), _circ(doy, _MONTH_CENTRE[m2])
    if d1 + d2 == 0:
        w = 1.0
    else:
        w = d2 / (d1 + d2)          # nearer centre gets the larger weight
    bias = w * float(p1["bias"]) + (1 - w) * float(p2["bias"])
    sd = w * float(p1["sd"]) + (1 - w) * float(p2["sd"])
    return bias, sd


def _lead_key(calib_city: dict, lead_days: int) -> dict | None:
    by_lead = calib_city.get("by_lead") or {}
    return by_lead.get(str(lead_days)) or by_lead.get(str(max(1, min(3, lead_days))))


def fetch_deterministic(cfg: dict, client: httpx.Client | None = None,
                        forecast_days: int = 4) -> dict[str, float]:
    """Daily-max per local date from the DETERMINISTIC best_match forecast.

    This is the CENTER we trade on, because it is the exact estimator
    `scripts/fit_weather.py` measured bias/sd against (previous-runs archives only
    the deterministic run — no ensemble history exists). Centering on the pooled
    ensemble mean instead would wear a bias correction never measured for it, and
    with bias swinging ±2.4°F seasonally a 1°F mismatch is half a bucket.
    """
    own = client is None
    c = client or httpx.Client(timeout=25)
    try:
        r = c.get(FORECAST_URL, params={
            "latitude": cfg["lat"], "longitude": cfg["lon"],
            "daily": "temperature_2m_max", "temperature_unit": "fahrenheit",
            "timezone": cfg["tz"], "forecast_days": forecast_days,
        })
        r.raise_for_status()
        daily = r.json().get("daily") or {}
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        raise WeatherError(f"deterministic forecast unavailable: {exc}") from exc
    finally:
        if own:
            c.close()
    dates = daily.get("time") or []
    highs = daily.get("temperature_2m_max") or []
    out = {d: float(v) for d, v in zip(dates, highs) if v is not None}
    if not out:
        raise WeatherError("deterministic forecast returned no daily maxima")
    return out


def fetch_members(city: str, cfg: dict, client: httpx.Client | None = None,
                  forecast_days: int = 3) -> tuple[dict[str, list[float]], float]:
    """Pooled daily-max members per local date, across every model.

    Used ONLY for the spread signal (see `pooled_widen`), never for the center.

    timezone is mandatory: the daily max MUST bucket on the city's local calendar
    day to match the NWS Climatological Report Kalshi settles on.
    """
    own = client is None
    c = client or httpx.Client(timeout=25)
    pooled: dict[str, list[float]] = {}
    ok_models = 0
    try:
        for model in MODELS:
            try:
                r = c.get(ENSEMBLE_URL, params={
                    "latitude": cfg["lat"], "longitude": cfg["lon"],
                    "daily": "temperature_2m_max", "models": model,
                    "temperature_unit": "fahrenheit", "timezone": cfg["tz"],
                    "forecast_days": forecast_days,
                })
                r.raise_for_status()
                daily = r.json().get("daily") or {}
            except (httpx.HTTPError, ValueError, KeyError):
                continue                      # one model down != pool down
            dates = daily.get("time") or []
            member_keys = [k for k in daily
                           if k.startswith("temperature_2m_max") and k != "time"]
            if not dates or not member_keys:
                continue
            ok_models += 1
            for i, day in enumerate(dates):
                vals = [daily[k][i] for k in member_keys
                        if i < len(daily[k]) and daily[k][i] is not None]
                pooled.setdefault(day, []).extend(float(v) for v in vals)
    finally:
        if own:
            c.close()
    if ok_models < 2:
        raise WeatherError(f"only {ok_models} ensemble models responded (need 2+)")
    return pooled, time.time()


def distribution(city: str, local_date: str, deterministic: dict[str, float],
                 calib: dict, fetched_ts: float, lead_days: int = 1,
                 pooled: dict[str, list[float]] | None = None) -> TempDistribution:
    """Build the calibrated distribution for one city-date. Fails closed.

    Center: the deterministic forecast (the estimator the calibration was fitted
    on). Spread: the fitted sd, widened if the live ensemble is unusually dispersed
    — never narrowed.
    """
    point = deterministic.get(local_date)
    if point is None:
        raise WeatherError(f"no deterministic forecast for {city} {local_date}")
    city_calib = calib.get(city)
    if not city_calib:
        raise WeatherError(f"no calibration fitted for {city}")
    lead = _lead_key(city_calib, lead_days)
    if not lead:
        raise WeatherError(f"no calibration for {city} lead {lead_days}d")
    bias, sd = seasonal_params(lead, dt.date.fromisoformat(local_date))
    widen = 1.0
    members = (pooled or {}).get(local_date)
    if members:
        widen = pooled_widen(members, lead.get("typical_spread"))
    return TempDistribution.from_point(point, bias=bias, sd=sd,
                                       fetched_ts=fetched_ts, spread_widen=widen)
