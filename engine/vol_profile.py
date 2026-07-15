"""Intraday vol seasonality: hour-of-UTC-day variance weights from 90d of hourly
perp candles (Deribit public, free). Motivation: live night of 2026-07-14 — uniform
calendar-time variance scaling overpriced quiet-hour tails (model 21% vs realized 4%
on the 5-15c bucket). Weights are normalized to mean 1.0, so a flat market gives
weights == 1 and seasonal time == calendar time exactly."""
from __future__ import annotations
import json
import math
import time

import httpx

import config

PROFILE_FILE = config.ROOT / "state" / "vol_profile.json"
FRESH_AGE_S = 24 * 3600
MAX_AGE_S = 48 * 3600
PROFILE_VERSION = 2


STATUS = "missing"        # 'fresh' | 'stale' | 'missing' — scanner logs transitions
AGE_S: float | None = None
_last_fetch_fail = 0.0    # cooldown: don't hammer a dead endpoint inside scan cycles


def fetch_weights(ccy: str, days: int = 90, client: httpx.Client | None = None) -> list[float]:
    c = client or httpx.Client(timeout=8)
    end = int(time.time() * 1000)
    try:
        r = c.get(f"{config.DERIBIT_BASE}/public/get_tradingview_chart_data",
                  params={"instrument_name": f"{ccy}-PERPETUAL", "resolution": "60",
                          "start_timestamp": end - days * 86400 * 1000,
                          "end_timestamp": end})
        r.raise_for_status()
        d = r.json()["result"]
    finally:
        if client is None:
            c.close()
    closes, ticks = d["close"], d["ticks"]
    if len(closes) != len(ticks):
        raise ValueError(f"mismatched candle data for {ccy}")
    sums, counts = [0.0] * 24, [0] * 24
    for i in range(1, len(closes)):
        if (ticks[i] + 3_600_000 <= end
                and ticks[i] - ticks[i - 1] == 3_600_000
                and all(math.isfinite(float(x)) and float(x) > 0
                        for x in (closes[i - 1], closes[i]))):
            hour = int(ticks[i] // 1000 // 3600) % 24
            sums[hour] += math.log(closes[i] / closes[i - 1]) ** 2
            counts[hour] += 1
    if sum(counts) < 24 * 30 or min(counts) == 0:
        raise ValueError(f"insufficient candle coverage for {ccy}: {counts}")
    raw = [sums[h] / counts[h] if counts[h] else 0.0 for h in range(24)]
    mean = sum(raw) / 24
    return _profile([0.5 + 0.5 * x / mean for x in raw])


def _profile(values, *, legacy: bool = False) -> list[float]:
    """Validate and normalize one 24-hour profile; shrink legacy raw weights."""
    if not isinstance(values, list) or len(values) != 24:
        raise ValueError("profile must contain 24 hourly weights")
    weights = [float(x) for x in values]
    if not all(math.isfinite(x) and x > 0 for x in weights):
        raise ValueError("profile weights must be finite and positive")
    mean = sum(weights) / 24
    weights = [x / mean for x in weights]
    if legacy:
        weights = [0.5 + 0.5 * x for x in weights]
    mean = sum(weights) / 24
    return [x / mean for x in weights]


def _cache() -> tuple[float, dict[str, list[float]]]:
    d = json.loads(PROFILE_FILE.read_text())
    computed_at = float(d["computed_at"])
    legacy = d.get("version") != PROFILE_VERSION
    profiles = {}
    for asset, values in d["weights"].items():
        try:
            profiles[asset] = _profile(values, legacy=legacy)
        except (TypeError, ValueError):
            continue
    return computed_at, profiles


def load_profiles(force: bool = False) -> dict[str, list[float]]:
    """Return only valid profiles; callers must fail closed for missing assets."""
    global STATUS, AGE_S, _last_fetch_fail
    if not force and PROFILE_FILE.exists():
        try:
            computed_at, profiles = _cache()
            AGE_S = time.time() - computed_at
            if AGE_S <= FRESH_AGE_S and profiles:
                STATUS = "fresh"
                return profiles
        except (OSError, TypeError, ValueError, KeyError):
            pass
    if not force and time.time() - _last_fetch_fail < 3600:   # cooldown after failure
        return _fallback()
    try:
        weights = {a: fetch_weights(s["deribit_ccy"])
                   for a, s in config.SERIES.items()}
    except Exception:
        _last_fetch_fail = time.time()
        return _fallback()
    PROFILE_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROFILE_FILE.write_text(json.dumps(
        {"version": PROFILE_VERSION, "computed_at": time.time(), "weights": weights},
        indent=1))
    STATUS = "fresh"
    AGE_S = 0.0
    return weights


def _fallback() -> dict[str, list[float]]:
    global STATUS, AGE_S
    if PROFILE_FILE.exists():
        try:
            computed_at, profiles = _cache()
            AGE_S = time.time() - computed_at
            if AGE_S <= MAX_AGE_S and profiles:
                STATUS = "stale"
                return profiles
        except (OSError, TypeError, ValueError, KeyError):
            pass
    STATUS = "missing"
    AGE_S = None
    return {}


def seasonal_tau(weights: list[float], t0: float, t1: float) -> float:
    """Integral of the hourly weight over [t0, t1] (unix seconds, UTC hours) —
    'how much vol-time elapses'. Flat weights -> exactly t1 - t0."""
    if t1 <= t0:
        return 0.0
    tau, t = 0.0, t0
    while t < t1:
        hour_end = (t // 3600 + 1) * 3600
        seg = min(hour_end, t1) - t
        tau += weights[int(t // 3600) % 24] * seg
        t += seg
    return tau
