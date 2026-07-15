"""Shadow-only realized-volatility regime metrics from completed 5-minute candles."""
from __future__ import annotations

import bisect
import json
import math
import time

import httpx

import config

CACHE_FILE = config.ROOT / "state" / "regime.json"
FRESH_S = 24 * 3600
STALE_GRACE_S = 48 * 3600
BAR_MS = 5 * 60 * 1000
CHUNK_MS = 10 * 86400 * 1000
ANNUAL_BARS = 365 * 24 * 12
STATUS = "missing"


def fetch_candles(ccy: str, days: int = 90, client: httpx.Client | None = None,
                  now: float | None = None) -> list[tuple[int, float]]:
    """Fetch deduplicated completed bars in bounded chunks (Deribit caps responses)."""
    own = client is None
    c = client or httpx.Client(timeout=12)
    now_ms = int((time.time() if now is None else now) * 1000)
    completed_end = (now_ms // BAR_MS) * BAR_MS - 1
    start = completed_end - days * 86400 * 1000
    bars: dict[int, float] = {}
    try:
        cursor = start
        while cursor <= completed_end:
            end = min(cursor + CHUNK_MS - 1, completed_end)
            response = c.get(
                f"{config.DERIBIT_BASE}/public/get_tradingview_chart_data",
                params={"instrument_name": f"{ccy}-PERPETUAL", "resolution": "5",
                        "start_timestamp": cursor, "end_timestamp": end},
            )
            response.raise_for_status()
            data = response.json()["result"]
            for tick, close in zip(data.get("ticks", []), data.get("close", [])):
                tick = int(tick)
                if close and tick + BAR_MS <= now_ms:
                    bars[tick] = float(close)
            cursor = end + 1
    finally:
        if own:
            c.close()
    return sorted(bars.items())


def _returns(bars: list[tuple[int, float]]) -> list[float]:
    """Only adjacent completed bars contribute; gaps never become five-minute moves."""
    return [math.log(p1 / p0) for (t0, p0), (t1, p1) in zip(bars, bars[1:])
            if t1 - t0 == BAR_MS and p0 > 0 and p1 > 0]


def _segments(bars: list[tuple[int, float]]) -> list[list[float]]:
    segments: list[list[float]] = [[]]
    for (t0, p0), (t1, p1) in zip(bars, bars[1:]):
        if t1 - t0 != BAR_MS:
            segments.append([])
        elif p0 > 0 and p1 > 0:
            segments[-1].append(math.log(p1 / p0))
    return [segment for segment in segments if segment]


def _rv(values: list[float]) -> float | None:
    if not values:
        return None
    return math.sqrt(sum(x * x for x in values) / len(values) * ANNUAL_BARS)


def _percentile(history: list[float], value: float) -> float | None:
    return bisect.bisect_right(sorted(history), value) / len(history) if history else None


def metrics(bars: list[tuple[int, float]]) -> dict:
    segments = _segments(bars)
    returns = [value for segment in segments for value in segment]
    out = {"bars": len(bars), "adjacent_returns": len(returns)}
    for label, count in (("1h", 12), ("6h", 72), ("24h", 288)):
        latest = segments[-1] if segments else []
        current = _rv(latest[-count:]) if len(latest) >= count else None
        history = [_rv(segment[i - count:i]) for segment in segments
                   for i in range(count, len(segment) + 1, count)]
        history = [x for x in history if x is not None]
        out[f"rv_{label}"] = current
        out[f"rv_{label}_percentile"] = (_percentile(history[:-1], current)
                                           if current is not None else None)
    return out


def load_regimes(force: bool = False) -> dict[str, dict]:
    """Refresh daily; shadow data may be stale for 48h but never controls orders."""
    global STATUS
    cached = None
    if CACHE_FILE.exists():
        try:
            cached = json.loads(CACHE_FILE.read_text())
            age = time.time() - float(cached["computed_at"])
            if not force and age <= FRESH_S:
                STATUS = "fresh"
                return cached["metrics"]
        except (OSError, ValueError, KeyError, TypeError):
            cached = None
    try:
        result = {asset: metrics(fetch_candles(series["deribit_ccy"]))
                  for asset, series in config.SERIES.items()}
        CACHE_FILE.parent.mkdir(exist_ok=True)
        CACHE_FILE.write_text(json.dumps({"computed_at": time.time(), "metrics": result},
                                         indent=1))
        STATUS = "fresh"
        return result
    except Exception:
        if cached and time.time() - float(cached["computed_at"]) <= STALE_GRACE_S:
            STATUS = "stale"
            return cached["metrics"]
        STATUS = "missing"
        return {}


def surface_ratios(regime: dict, index_price: float, horizon_s: float,
                   model_iqr: float | None, market_iqr: float | None) -> dict:
    """Compare forecast widths with a normal-width diagnostic; shadow only."""
    out = dict(regime)
    rv = regime.get("rv_24h")
    if not rv or not index_price or horizon_s <= 0:
        return out
    rv_iqr = 1.3489795 * index_price * rv * math.sqrt(horizon_s / (365 * 86400))
    out["rv_implied_iqr"] = rv_iqr
    out["model_iqr_to_rv"] = model_iqr / rv_iqr if model_iqr is not None and rv_iqr else None
    out["market_iqr_to_rv"] = market_iqr / rv_iqr if market_iqr is not None and rv_iqr else None
    return out
