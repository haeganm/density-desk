"""Scan cycle: build a calibrated temp distribution per open event, snapshot fair
vs book, emit edges.

Weather events are NATIVE exhaustive partitions (<=88 / 89-90 / ... / >=97 --
exactly one leg pays), so unlike the crypto ladder we do not synthesise buckets
from a fitted density: Kalshi hands us the partition. That makes
`sum(model legs) == 1` a real cross-contract CONSISTENCY GUARD -- if it drifts,
either our pricing is inconsistent or the partition has a gap, and either way we
must not quote. It is not an arbitrage signal: the market's bid side already sums
to ~1.000, and for any partition the taker fee is 0.07*(1 - sum p^2), which is
structurally largest exactly where the apparent overround is biggest.
"""
from __future__ import annotations
import datetime as dt
import math
import re
import time
from dataclasses import dataclass, field

import config
import db
from engine import fees, weather
from engine.weather import WeatherError

# Weather forecasts SHARPEN toward settlement -- the opposite of a crypto smile,
# which decays near expiry. So this is not model distrust; it is only a guard
# against settlement-window ambiguity and quotes with no time left to work.
STAND_DOWN_FINAL_S = 30 * 60
WIDTH_RATIO_MIN = config.MODEL_MARKET_IQR_MIN
WIDTH_RATIO_MAX = config.MODEL_MARKET_IQR_MAX
MIN_LADDER_QUOTE_SIZE = 1.0
PARTITION_TOL = 0.02              # model legs must sum to 1 within this
MARKET_SUM_MIN, MARKET_SUM_MAX = 0.90, 1.25
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}


@dataclass
class Forecast:
    """One city's pooled ensemble, keyed by local date. `fetched_ts` feeds the
    staleness rail exactly as the Deribit chain's did."""
    pooled: dict[str, list[float]] = field(default_factory=dict)
    fetched_ts: float = 0.0

    @property
    def center(self) -> float | None:
        """Raw pooled mean for the nearest date -- display only (uncalibrated)."""
        if not self.pooled:
            return None
        members = self.pooled[min(self.pooled)]
        return sum(members) / len(members) if members else None


@dataclass
class Edge:
    ticker: str
    event: str
    city: str
    side: str          # 'yes' = buy yes at ask, 'no' = buy no at (1 - yes_bid)
    price: float       # what we'd pay per contract (dollars)
    fair: float        # fair value of the side we're buying
    edge: float        # fair - price - fee, per contract
    settle_ts: float
    scenarios: tuple[tuple[float, float, tuple[str, ...]], ...] = ()
    strike_type: str = ""
    floor_strike: float | None = None
    cap_strike: float | None = None
    market_fair: float | None = None
    model_version: str = config.MODEL_VERSION
    maker: bool = False          # True = post-only resting quote, not an IOC take


def event_date(event_ticker: str) -> dt.date | None:
    """KXHIGHNY-26JUL16 -> date(2026, 7, 16). The market's LOCAL calendar day."""
    m = re.match(r"^[A-Z0-9]+-(\d{2})([A-Z]{3})(\d{2})$", event_ticker or "")
    if not m:
        return None
    try:
        return dt.date(2000 + int(m.group(1)), _MONTHS[m.group(2)], int(m.group(3)))
    except (KeyError, ValueError):
        return None


def _trusted_fair(side_fair: float, mid: float | None, side: str) -> float:
    """Shrink the model's fair toward the market mid (config.MODEL_TRUST).

    The mid is the crowd's estimate; ours is a model's. Trading the raw difference
    assumes we are right and they are noise -- the crypto fill record said
    otherwise, so we only ever trade the part of the disagreement that survives
    shrinkage. With no two-sided quote there is no consensus to blend with.
    """
    if mid is None or not 0 < mid < 1:
        return side_fair
    market_fair = mid if side == "yes" else 1.0 - mid
    return config.MODEL_TRUST * side_fair + (1.0 - config.MODEL_TRUST) * market_fair


def _maker_candidate(side: str, bid: float, ask: float, side_fair: float,
                     factor: float = 1.0) -> tuple[float, float] | None:
    """Post-only price one tick inside the spread, if the fee-free maker edge
    still clears MAKER_EDGE_MIN. Returns (price, edge)."""
    if not (0 < bid <= ask < 1) or ask - bid < 0.02:   # need room to stand inside
        return None
    if side == "yes":
        px = round(bid + 0.01, 2)                      # improve best bid one tick
        if px >= ask:
            return None                                # would cross: post_only rejects
    else:
        yes_px = round(ask - 0.01, 2)                  # undercut best ask one tick
        if yes_px <= bid:
            return None
        px = round(1 - yes_px, 2)                      # the no-price we pay
    edge = side_fair - px - fees.maker_fee(px, 1)
    if edge <= config.MAKER_EDGE_MIN * factor:
        return None
    return px, edge


def _survival_points(points) -> list[tuple[float, float]] | None:
    try:
        pts = sorted((float(k), float(p)) for k, p in points)
    except (TypeError, ValueError):
        return None
    if (len(pts) < 2 or any(not (math.isfinite(k) and math.isfinite(p) and 0 <= p <= 1)
                            for k, p in pts)
            or any(k1 <= k0 or p1 > p0 + 1e-9
                   for (k0, p0), (k1, p1) in zip(pts, pts[1:]))):
        return None
    return pts


def _survival_quantile(points, probability: float) -> float | None:
    pts = _survival_points(points)
    if not pts:
        return None
    for (k0, p0), (k1, p1) in zip(pts, pts[1:]):
        if p0 >= probability >= p1 and p0 > p1:
            return k0 + (p0 - probability) / (p0 - p1) * (k1 - k0)
    return None


def _iqr(points) -> float | None:
    points = _survival_points(points)
    if points is None:
        return None
    lower = _survival_quantile(points, 0.75)
    upper = _survival_quantile(points, 0.25)
    if lower is None or upper is None or upper <= lower:
        return None
    return upper - lower


def _upper_edge(m: dict) -> float | None:
    """Highest REPORTED integer this leg still pays on. None = open-ended top."""
    st = m.get("strike_type")
    if st == "between":
        return float(m["cap_strike"])
    if st == "less":                       # 'less' cap=89 means "88 or below"
        cap = m.get("cap_strike")
        return float(cap if cap is not None else m["floor_strike"]) - 1.0
    return None                            # 'greater' is the open top bucket


def _market_points(legs) -> list[tuple[float, float]]:
    """Market-implied survival P(reported > K) at each bucket's upper edge.

    legs: [(market, probability)] -- normalised bucket probabilities.
    """
    ordered = sorted(legs, key=lambda lp: (_upper_edge(lp[0]) is None,
                                           _upper_edge(lp[0]) or 0.0))
    pts, cum = [], 0.0
    for m, p in ordered:
        cum += p
        edge = _upper_edge(m)
        if edge is not None:
            pts.append((edge, max(0.0, min(1.0, 1.0 - cum))))
    return pts


def _liquid_quote(market: dict, bid: float, ask: float) -> bool:
    """A usable distribution point needs a real, uncrossed two-sided top book.

    NB: never gate on `liquidity_dollars` -- it is "0.0000" on every weather
    market and would block every trade.
    """
    try:
        bid_size = float(market.get("yes_bid_size_fp")
                         or market.get("yes_bid_size") or 0)
        ask_size = float(market.get("yes_ask_size_fp")
                         or market.get("yes_ask_size") or 0)
    except (TypeError, ValueError):
        return False
    return (0 < bid <= ask < 1 and ask - bid <= 0.15
            and min(bid_size, ask_size) >= MIN_LADDER_QUOTE_SIZE)


def _mid(m: dict) -> float | None:
    bid = float(m.get("yes_bid_dollars") or 0)
    ask = float(m.get("yes_ask_dollars") or 1)
    return (bid + ask) / 2 if 0 < bid <= ask < 1 else None


def _scenarios(d, markets: list[dict]):
    """One scenario per leg -- the event IS the partition.

    Returns None (fail closed) unless the model legs sum to 1 within tolerance,
    which simultaneously proves our pricing is self-consistent and that Kalshi's
    partition has no gap or overlap.
    """
    legs = []
    for m in markets:
        f = weather.fair(d, m)
        mid = _mid(m)
        if f is None or mid is None or not math.isfinite(f):
            return None                        # every leg must be priced and quoted
        legs.append((m, f, mid))
    if len(legs) < 2:
        return None
    model_sum = sum(f for _m, f, _k in legs)
    market_sum = sum(k for _m, _f, k in legs)
    if abs(model_sum - 1.0) > PARTITION_TOL:
        return None                            # gap/overlap, or inconsistent pricing
    if not MARKET_SUM_MIN <= market_sum <= MARKET_SUM_MAX:
        return None
    return tuple((max(0.0, f / model_sum), max(0.0, k / market_sum), (m["ticker"],))
                 for m, f, k in legs)


def _surface_verdict(d, markets: list[dict]):
    """(bad, detail, scenarios). Center compared in ABSOLUTE degF -- the crypto
    relative-bps test would mean 0.09degF at 90degF, which is absurd."""
    quoted = [(m, _mid(m)) for m in markets if _mid(m) is not None
              and _liquid_quote(m, float(m.get("yes_bid_dollars") or 0),
                                float(m.get("yes_ask_dollars") or 1))]
    if len(quoted) < 3:
        return True, "thin book", ()
    total = sum(p for _m, p in quoted)
    if not MARKET_SUM_MIN <= total <= MARKET_SUM_MAX:
        return True, f"market legs sum to {total:.3f}", ()
    pts = _market_points([(m, p / total) for m, p in quoted])
    market_pts = _survival_points(pts)
    if market_pts is None:
        return True, "non-monotone market curve", ()
    market_center = _survival_quantile(market_pts, 0.5)
    if market_center is None:
        return True, "book does not bracket its center", ()
    basis = d.median() - market_center
    if not (abs(basis) <= config.BASIS_ALARM_F):     # NaN must fail, not pass
        return True, f"model vs market center {basis:+.1f}F", ()
    model_iqr = _iqr(zip(d.K_grid, d._D))
    market_iqr = _iqr(market_pts)
    if model_iqr is None or market_iqr is None:
        return True, "book does not bracket both quartiles", ()
    ratio = model_iqr / market_iqr
    if not WIDTH_RATIO_MIN <= ratio <= WIDTH_RATIO_MAX:
        return True, f"model/market IQR ratio {ratio:.2f}", ()
    scenarios = _scenarios(d, markets)
    if scenarios is None:
        return True, "partition not exhaustive or legs unquoted", ()
    return False, f"model/market IQR ratio {ratio:.2f}", scenarios


def _close_ts(m: dict) -> float:
    return dt.datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")).timestamp()


def scan(kalshi, forecasts: dict[str, Forecast], con=None) -> tuple[list[Edge], float | None]:
    """Returns (edges sorted best-first, nearest upcoming settle ts or None)."""
    now = time.time()
    calib = weather.load_calibration()
    if con and weather.STATUS != "fresh":
        db.log_event(con, f"weather_calib_{weather.STATUS}",
                     "calibration degraded — " +
                     ("older than 30d, refit" if weather.STATUS == "stale"
                      else "missing: run scripts/fit_weather.py — scans blocked"))
    edges: list[Edge] = []
    snap_rows = []
    next_settle: float | None = None
    n_filtered = 0
    today = dt.datetime.now(dt.UTC).date()

    for city, cfg in config.CITIES.items():
        fc = forecasts.get(city)
        if fc is None or not fc.pooled or city not in calib:
            if con:
                db.log_event(con, "forecast_skip",
                             f"{city}: no usable forecast/calibration")
            continue
        markets = kalshi.markets(cfg["series"])
        if con:
            db.record_contracts(con, [
                (m["ticker"], m["event_ticker"], city, _close_ts(m),
                 m.get("strike_type"), m.get("floor_strike"), m.get("cap_strike"))
                for m in markets if m.get("ticker") and m.get("event_ticker")
            ])
        events: dict[str, list[dict]] = {}
        for m in markets:
            events.setdefault(m["event_ticker"], []).append(m)

        for ev, ems in sorted(events.items()):
            settle = _close_ts(ems[0])
            if settle > now and (next_settle is None or settle < next_settle):
                next_settle = settle
            if settle - now < STAND_DOWN_FINAL_S:
                continue
            local = event_date(ev)
            if local is None:
                continue
            # D+0 books go degenerate (one-sided 0.01/0.99) once the day's high is
            # effectively known, and our edge there is nil. Trade D+1 and beyond.
            lead = (local - today).days
            if lead < 1:
                continue
            try:
                d = weather.distribution(city, local.isoformat(), fc.pooled, calib,
                                         fc.fetched_ts, lead_days=lead)
            except WeatherError as e:
                if con:
                    db.record_surface(con, {
                        "ts": now, "event": ev, "city": city, "settle_ts": settle,
                        "model_version": config.MODEL_VERSION,
                        "forecast_status": weather.STATUS,
                        "forecast_age_s": now - fc.fetched_ts,
                    })
                    db.log_event(con, "forecast_skip", f"{ev}: {e}")
                continue

            ev_edges: list[Edge] = []
            surface_contracts: list[dict] = []
            for m in ems:
                fair = weather.fair(d, m)
                if fair is None:
                    continue
                bid = float(m.get("yes_bid_dollars") or 0)
                ask = float(m.get("yes_ask_dollars") or 1)
                mid = _mid(m)
                if mid is not None:
                    surface_contracts.append({
                        "ticker": m["ticker"], "strike_type": m.get("strike_type"),
                        "floor_strike": m.get("floor_strike"),
                        "cap_strike": m.get("cap_strike"), "fair": fair,
                        "market_mid": mid,
                    })
                floor_s = (float(m["floor_strike"])
                           if m.get("floor_strike") is not None else None)
                cap_s = (float(m["cap_strike"])
                         if m.get("cap_strike") is not None else None)
                # store BOTH edges: the crypto `floor_strike or cap_strike` was
                # lossy for brackets and silently dropped a 0 floor
                snap_rows.append((now, m["ticker"], ev, city, floor_s, cap_s,
                                  settle, fair, bid, ask, d.median()))
                for side, price, model_fair in (("yes", ask, fair),
                                                ("no", 1 - bid if 0 < bid else 0, 1 - fair)):
                    if not 0 < price < 1:
                        continue
                    side_fair = _trusted_fair(model_fair, mid, side)
                    e = side_fair - price - fees.taker_fee(price, 1)
                    if e <= config.TAKER_EDGE_MIN:
                        if config.MAKER_ENABLED:
                            mk = _maker_candidate(side, bid, ask, side_fair)
                            if mk is not None \
                                    and config.MIN_BUY_PRICE <= mk[0] <= config.MAX_BUY_PRICE \
                                    and side_fair <= config.MAX_FAIR_RATIO * mk[0]:
                                ev_edges.append(Edge(
                                    m["ticker"], ev, city, side, mk[0], side_fair,
                                    mk[1], settle, strike_type=m.get("strike_type", ""),
                                    floor_strike=floor_s, cap_strike=cap_s,
                                    market_fair=(mid if side == "yes" else 1 - mid)
                                    if mid is not None else None, maker=True))
                        continue
                    if not config.MIN_BUY_PRICE <= price <= config.MAX_BUY_PRICE \
                            or side_fair > config.MAX_FAIR_RATIO * price:
                        n_filtered += 1
                        continue
                    ev_edges.append(Edge(
                        m["ticker"], ev, city, side, price, side_fair, e, settle,
                        strike_type=m.get("strike_type", ""),
                        floor_strike=floor_s, cap_strike=cap_s,
                        market_fair=(mid if side == "yes" else 1 - mid)
                        if mid is not None else None))

            bad, detail, scenarios = _surface_verdict(d, ems)
            if con:
                model_iqr = _iqr(zip(d.K_grid, d._D))
                quoted = [(m, _mid(m)) for m in ems if _mid(m) is not None]
                total = sum(p for _m, p in quoted) or 1.0
                market_pts = _market_points([(m, p / total) for m, p in quoted])
                market_iqr = _iqr(market_pts)
                db.record_surface(con, {
                    "ts": now, "event": ev, "city": city, "settle_ts": settle,
                    "model_version": config.MODEL_VERSION,
                    "model_center": d.median(), "model_iqr": model_iqr,
                    "market_center": _survival_quantile(market_pts, 0.5),
                    "market_iqr": market_iqr,
                    "width_ratio": (model_iqr / market_iqr
                                    if model_iqr is not None and market_iqr else None),
                    "forecast_status": weather.STATUS,
                    "forecast_age_s": now - fc.fetched_ts,
                    "spread_widen": d.spread_widen,
                    "contracts": surface_contracts, "scenarios": scenarios,
                })
            if bad:
                if con:
                    db.log_event(con, "surface_alarm",
                                 f"{ev}: {detail}, {len(ev_edges)} edges dropped")
                continue
            covered = {t for _, _, winners in scenarios for t in winners}
            for edge in ev_edges:
                if edge.ticker not in covered:
                    continue
                edge.scenarios = scenarios
                edges.append(edge)

    if con and n_filtered:
        db.log_event(con, "edge_filtered",
                     f"{n_filtered} raw edges blocked by price/ratio humility filters")
    if con and snap_rows:
        con.executemany("INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?)", snap_rows)
        con.executemany(
            "INSERT INTO edges(ts,ticker,side,fair,price,edge,size,acted,reason) "
            "VALUES(?,?,?,?,?,?,0,0,'scan')",
            [(now, e.ticker, e.side, e.fair, e.price, e.edge) for e in edges])
        con.commit()
    edges.sort(key=lambda e: -e.edge)
    return edges, next_settle


def fetch_all_forecasts() -> dict[str, Forecast]:
    out: dict[str, Forecast] = {}
    for city, cfg in config.CITIES.items():
        pooled, ts = weather.fetch_members(city, cfg)
        out[city] = Forecast(pooled=pooled, fetched_ts=ts)
    return out
