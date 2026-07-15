"""Scan cycle: build densities per open event, snapshot fair vs book, emit edges."""
from __future__ import annotations
import datetime as dt
import json
import math
import time
from dataclasses import dataclass

import config
import db
from engine import fees, regime
from engine.checks import DensityError, sanity
from engine.deribit import Chain, fetch_chain
from engine.density import SettleDensity

STAND_DOWN_FINAL_S = 45 * 60      # final-hour smile is unreliable (verified live 2026-07-13)
WIDTH_RATIO_MIN = config.MODEL_MARKET_IQR_MIN
WIDTH_RATIO_MAX = config.MODEL_MARKET_IQR_MAX
CATALYST_MAX_AGE_S = 48 * 3600
CATALYST_HORIZON_S = 48 * 3600    # docs/gpt-catalyst-prompt.md asks for "the next 48h"
MIN_LADDER_QUOTE_SIZE = 1.0
_last_profile_status = "fresh"    # log vol-profile degradation once per transition


def _catalyst_calendar() -> tuple[list[tuple[float, float, float]], float] | None:
    """(windows, covers_until_ts) or None. The horizon matters as much as the
    windows: an empty stretch beyond it is ignorance, not calm."""
    f = config.ROOT / "state" / "catalysts.json"
    if not f.exists():
        return None
    try:
        data = json.loads(f.read_text())
        if not isinstance(data, dict) or not isinstance(data.get("windows"), list):
            return None
        generated = float(data.get("generated_ts", f.stat().st_mtime))
        now = time.time()
        if (not math.isfinite(generated) or generated > now + 300
                or now - generated > CATALYST_MAX_AGE_S):
            return None
        windows = [(float(c["start_ts"]), float(c["end_ts"]),
                    float(c.get("factor", 2.0))) for c in data["windows"]]
        if any(not all(math.isfinite(x) for x in row) or row[0] >= row[1]
               or not 1.0 <= row[2] <= 99.0 for row in windows):
            return None
        # the sweep prompt enumerates the next 48h from when it ran, so that is
        # exactly how far this calendar can vouch for — regardless of its age
        covers_until = float(data.get("covers_until_ts",
                                      generated + CATALYST_HORIZON_S))
        if not math.isfinite(covers_until):
            return None
        return windows, covers_until
    except (OSError, ValueError, KeyError, TypeError):
        return None               # invalid/stale risk calendars fail closed for orders


def _catalyst_windows() -> list[tuple[float, float, float]] | None:
    calendar = _catalyst_calendar()
    return None if calendar is None else calendar[0]


def threshold_factor(now: float) -> float:
    """>1 widens edge thresholds inside a flagged catalyst window."""
    windows = _catalyst_windows()
    if windows is None:
        return 99.0
    return max((factor for start, end, factor in windows
                if start <= now <= end), default=1.0)


def catalyst_policy(now: float, settle: float) -> tuple[bool, float]:
    """(hard skip, edge factor) over the whole intended holding interval."""
    calendar = _catalyst_calendar()
    if calendar is None:
        return True, 1.0
    windows, covers_until = calendar
    if settle > covers_until:
        # Nobody enumerated events out this far. "No window" here means unknown,
        # not clear — never hold across risk we never looked for.
        return True, 1.0
    hard_skip = any(start <= settle <= end for start, end, _ in windows)
    factor = max((factor for start, end, factor in windows
                  if start <= settle and end >= now), default=1.0)
    return hard_skip, factor


@dataclass
class Edge:
    ticker: str
    event: str
    asset: str
    side: str          # 'yes' = buy yes at ask, 'no' = buy no at (1 - yes_bid)
    price: float       # what we'd pay per contract (dollars)
    fair: float        # fair value of the side we're buying
    edge: float        # fair - price - taker_fee, per contract
    settle_ts: float
    scenarios: tuple[tuple[float, float, tuple[str, ...]], ...] = ()
    strike_type: str = ""
    floor_strike: float | None = None
    cap_strike: float | None = None
    market_fair: float | None = None
    model_version: str = config.MODEL_VERSION
    maker: bool = False          # True = post-only resting quote, not an IOC take


ONE_SIDED = "one_sided"           # ladder quotes all on one side of 0.5: price beyond
                                  # the quoted ladder = the stale-chain regime -> alarm


def _trusted_fair(side_fair: float, mid: float | None, side: str) -> float:
    """Shrink the model's fair toward the market mid (config.MODEL_TRUST).

    The mid is the crowd's estimate; ours is a model's. Trading the raw difference
    assumes we are right and they are noise — the fill data says otherwise, so we
    only ever trade the part of the disagreement that survives shrinkage. With no
    two-sided quote there is no consensus to blend with, and the model stands alone.
    """
    if mid is None or not 0 < mid < 1:
        return side_fair
    market_fair = mid if side == "yes" else 1.0 - mid
    return config.MODEL_TRUST * side_fair + (1.0 - config.MODEL_TRUST) * market_fair


def _maker_candidate(side: str, bid: float, ask: float, side_fair: float,
                     factor: float) -> tuple[float, float] | None:
    """Post-only price one tick inside the spread, if the fee-light maker edge
    still clears MAKER_EDGE_MIN x catalyst factor. Returns (price, edge)."""
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


def _basis_bps(d: SettleDensity, ladder_mids: list[tuple[float, float]]) -> float | str | None:
    """Model median vs market-implied center (where ladder mids cross 0.5), in bps.
    None = not enough data to judge (thin book); ONE_SIDED = judgeable and bad."""
    pts = sorted(ladder_mids)
    for (k0, m0), (k1, m1) in zip(pts, pts[1:]):
        if m0 >= 0.5 >= m1 and m0 > m1:
            market_center = k0 + (m0 - 0.5) / (m0 - m1) * (k1 - k0)
            return (d.median() / market_center - 1.0) * 1e4
    if len(pts) >= 2:
        return ONE_SIDED
    return None


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


def _survival_at(points, strike: float) -> float | None:
    pts = _survival_points(points)
    if not pts or strike < pts[0][0] or strike > pts[-1][0]:
        return None
    for (k0, p0), (k1, p1) in zip(pts, pts[1:]):
        if k0 <= strike <= k1:
            return p0 + (strike - k0) / (k1 - k0) * (p1 - p0)
    return pts[-1][1]


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


def _wins(market: dict, price: float) -> bool:
    strike_type = market.get("strike_type")
    if strike_type == "greater":
        return price > float(market["floor_strike"])
    if strike_type == "between":
        return float(market["floor_strike"]) < price <= float(market["cap_strike"])
    if strike_type == "less":
        return price <= float(market.get("cap_strike") or market["floor_strike"])
    return False


def _scenarios(d: SettleDensity, ladder_mids: list[tuple[float, float]],
               markets: list[dict]) -> tuple[tuple[float, float, tuple[str, ...]], ...] | None:
    points = _survival_points(ladder_mids)
    if points is None:
        return None
    lo, hi = points[0][0], points[-1][0]
    covered = []
    try:
        for market in markets:
            strikes = [float(value) for value in
                       (market.get("floor_strike"), market.get("cap_strike"))
                       if value is not None]
            if strikes and all(math.isfinite(x) and lo <= x <= hi for x in strikes):
                covered.append(market)
    except (TypeError, ValueError):
        return None
    boundaries = sorted({float(value) for m in covered
                         for value in (m.get("floor_strike"), m.get("cap_strike"))
                         if value is not None})
    if not boundaries:
        return None
    market_survival = [_survival_at(ladder_mids, strike) for strike in boundaries]
    if any(p is None for p in market_survival):
        return None
    model_survival = [float(d.survival(strike)) for strike in boundaries]
    model_probs = ([1 - model_survival[0]]
                   + [a - b for a, b in zip(model_survival, model_survival[1:])]
                   + [model_survival[-1]])
    market_probs = ([1 - market_survival[0]]
                    + [a - b for a, b in zip(market_survival, market_survival[1:])]
                    + [market_survival[-1]])
    if (any(not math.isfinite(p) or p < -1e-9 for p in model_probs + market_probs)
            or abs(sum(model_probs) - 1) > 1e-6 or abs(sum(market_probs) - 1) > 1e-6):
        return None
    gap = max(1.0, boundaries[-1] - boundaries[0])
    samples = ([boundaries[0] - gap]
               + [(a + b) / 2 for a, b in zip(boundaries, boundaries[1:])]
               + [boundaries[-1] + gap])
    try:
        return tuple((max(0.0, mp), max(0.0, kp),
                      tuple(m["ticker"] for m in covered if _wins(m, sample)))
                     for mp, kp, sample in zip(model_probs, market_probs, samples))
    except (KeyError, TypeError, ValueError):
        return None


def _surface_verdict(d: SettleDensity, ladder_mids: list[tuple[float, float]],
                     markets: list[dict]):
    points = _survival_points(ladder_mids)
    if points is None:
        return True, "thin or non-monotone ladder", ()
    basis = _basis_bps(d, points)
    if basis == ONE_SIDED or basis is None:
        return True, "ladder does not bracket its center", ()
    if not (abs(basis) <= config.BASIS_ALARM_BPS):   # NaN must fail, not pass
        return True, f"model vs market center {basis:+.1f}bps", ()
    model_iqr = _iqr(zip(d.K_grid, d._D))
    market_iqr = _iqr(points)
    if model_iqr is None or market_iqr is None:
        return True, "ladder does not bracket both quartiles", ()
    ratio = model_iqr / market_iqr
    if not WIDTH_RATIO_MIN <= ratio <= WIDTH_RATIO_MAX:
        return True, f"model/market IQR ratio {ratio:.2f}", ()
    scenarios = _scenarios(d, points, markets)
    if scenarios is None:
        return True, "cannot build exhaustive market scenarios", ()
    return False, f"model/market IQR ratio {ratio:.2f}", scenarios


def _close_ts(m: dict) -> float:
    return dt.datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")).timestamp()


def _fair(d: SettleDensity, m: dict) -> float | None:
    st = m.get("strike_type")
    if st == "greater":
        return float(d.survival(m["floor_strike"]))
    if st == "between":
        return d.bucket(m["floor_strike"], m["cap_strike"])
    if st == "less":
        k = m.get("cap_strike") or m.get("floor_strike")
        return 1.0 - float(d.survival(k))
    return None


def _liquid_ladder_quote(market: dict, bid: float, ask: float) -> bool:
    """A usable distribution point needs a real, uncrossed two-sided top book."""
    try:
        bid_size = float(market.get("yes_bid_size_fp")
                         or market.get("yes_bid_size") or 0)
        ask_size = float(market.get("yes_ask_size_fp")
                         or market.get("yes_ask_size") or 0)
    except (TypeError, ValueError):
        return False
    return (0 < bid <= ask < 1 and ask - bid <= 0.10
            and min(bid_size, ask_size) >= MIN_LADDER_QUOTE_SIZE)


def scan(kalshi, chains: dict[str, Chain], con=None) -> tuple[list[Edge], float | None]:
    """Returns (edges sorted best-first, nearest upcoming settle ts or None)."""
    now = time.time()
    from engine import vol_profile
    profiles = vol_profile.load_profiles()
    regimes = regime.load_regimes() if con else {}
    global _last_profile_status
    if con and vol_profile.STATUS != "fresh" and vol_profile.STATUS != _last_profile_status:
        con.execute("INSERT INTO events VALUES(?,?,?)",
                    (now, f"vol_profile_{vol_profile.STATUS}",
                     "seasonal weights degraded — " +
                     ("using stale profile" if vol_profile.STATUS == "stale"
                      else "affected asset scans blocked")))
        con.commit()
    _last_profile_status = vol_profile.STATUS
    edges: list[Edge] = []
    snap_rows = []
    next_settle: float | None = None
    n_filtered = 0
    for asset, s in config.SERIES.items():
        profile = profiles.get(asset)
        if profile is None:
            if con:
                con.execute("INSERT INTO events VALUES(?,?,?)",
                            (now, "vol_profile_skip", f"{asset}: no valid profile"))
                con.commit()
            continue
        chain = chains[asset]
        markets = kalshi.markets(s["ladder"]) + kalshi.markets(s["range"])
        if con:
            db.record_contracts(con, [
                (m["ticker"], m["event_ticker"], asset, _close_ts(m),
                 m.get("strike_type"), m.get("floor_strike"), m.get("cap_strike"))
                for m in markets if m.get("ticker") and m.get("event_ticker")
            ])
        markets_by_settle: dict[float, list[dict]] = {}
        events: dict[str, list[dict]] = {}
        for m in markets:
            events.setdefault(m["event_ticker"], []).append(m)
            markets_by_settle.setdefault(_close_ts(m), []).append(m)
        # ladder events first: their basis verdict covers the co-settling range
        # event (same density, same failure mode — audit M2)
        settle_verdict: dict[float, bool] = {}
        settle_scenarios: dict[float, tuple[tuple[float, float, tuple[str, ...]], ...]] = {}
        settle_stats: dict[float, dict] = {}
        ordered = sorted(events.items(),
                         key=lambda kv: 0 if kv[0].startswith(s["ladder"]) else 1)
        for ev, ems in ordered:
            settle = _close_ts(ems[0])
            if settle > now and (next_settle is None or settle < next_settle):
                next_settle = settle
            if settle - now < STAND_DOWN_FINAL_S:
                continue
            catalyst_skip, catalyst_factor = catalyst_policy(now, settle)
            if catalyst_skip:
                if con:
                    db.record_surface(con, {
                        "ts": now, "event": ev, "asset": asset, "settle_ts": settle,
                        "model_version": config.MODEL_VERSION,
                        "profile_status": vol_profile.STATUS,
                        "profile_age_s": vol_profile.AGE_S,
                        "catalyst_factor": catalyst_factor, "catalyst_blocked": True,
                    })
                    con.execute("INSERT INTO events VALUES(?,?,?)",
                                (now, "catalyst_skip", f"{ev}: settles inside window"))
                    con.commit()
                continue
            min_edge = config.TAKER_EDGE_MIN * catalyst_factor
            try:
                d = SettleDensity(chain.slice_for_settle(settle), settle_ts=settle,
                                  now=now, index_price=chain.index_price,
                                  profile=profile)
                sanity(d)
            except (DensityError, ValueError) as e:
                if con:
                    db.record_surface(con, {
                        "ts": now, "event": ev, "asset": asset, "settle_ts": settle,
                        "model_version": config.MODEL_VERSION,
                        "profile_status": vol_profile.STATUS,
                        "profile_age_s": vol_profile.AGE_S,
                        "catalyst_factor": catalyst_factor, "catalyst_blocked": False,
                    })
                    con.execute("INSERT INTO events VALUES(?,?,?)",
                                (now, "density_skip", f"{ev}: {e}"))
                    con.commit()
                continue
            ev_edges: list[Edge] = []
            surface_contracts: list[dict] = []
            ladder_mids: list[tuple[float, float]] = []   # (strike, market mid) for basis check
            for m in ems:
                fair = _fair(d, m)
                if fair is None:
                    continue
                bid = float(m.get("yes_bid_dollars") or 0)
                ask = float(m.get("yes_ask_dollars") or 1)
                mid = ((bid + ask) / 2 if 0 < bid <= ask < 1 else None)
                if mid is not None:
                    surface_contracts.append({
                        "ticker": m["ticker"], "strike_type": m.get("strike_type"),
                        "floor_strike": m.get("floor_strike"),
                        "cap_strike": m.get("cap_strike"), "fair": fair,
                        "market_mid": mid,
                    })
                snap_rows.append((now, m["ticker"], ev, asset,
                                  m.get("floor_strike") or m.get("cap_strike"),
                                  settle, fair, bid, ask, chain.index_price))
                # basis-check inputs: ladder markets with a real two-sided quote;
                # junk 0.01/0.99 books (mid exactly 0.50) would hijack the crossing
                if (m.get("strike_type") == "greater"
                        and _liquid_ladder_quote(m, bid, ask)):
                    ladder_mids.append((m["floor_strike"], (bid + ask) / 2))
                # both directions, through the humility filters:
                # MIN_BUY_PRICE (sub-15c tails: 4-13% realized on 21-27% predicted) and
                # MAX_FAIR_RATIO (fair >> price historically means WE are wrong)
                for side, price, model_fair in (("yes", ask, fair),
                                                ("no", 1 - bid if 0 < bid else 0, 1 - fair)):
                    if not 0 < price < 1:
                        continue
                    # trade only the disagreement that survives shrinkage to the mid
                    side_fair = _trusted_fair(model_fair, mid, side)
                    e = side_fair - price - fees.taker_fee(price, 1)
                    if e <= min_edge:
                        # maker band: too small to cross, may clear when posted
                        # inside the spread; same humility filters at post price
                        if config.MAKER_ENABLED:
                            mk = _maker_candidate(side, bid, ask, side_fair,
                                                  catalyst_factor)
                            if mk is not None \
                                    and config.MIN_BUY_PRICE <= mk[0] <= config.MAX_BUY_PRICE \
                                    and side_fair <= config.MAX_FAIR_RATIO * mk[0]:
                                ev_edges.append(Edge(
                                    m["ticker"], ev, asset, side, mk[0], side_fair,
                                    mk[1], settle,
                                    strike_type=m.get("strike_type", ""),
                                    floor_strike=(float(m["floor_strike"])
                                                  if m.get("floor_strike") is not None
                                                  else None),
                                    cap_strike=(float(m["cap_strike"])
                                                if m.get("cap_strike") is not None
                                                else None),
                                    market_fair=(mid if side == "yes" else 1 - mid)
                                    if mid is not None else None,
                                    maker=True))
                        continue
                    if not config.MIN_BUY_PRICE <= price <= config.MAX_BUY_PRICE \
                            or side_fair > config.MAX_FAIR_RATIO * price:
                        n_filtered += 1
                        continue
                    ev_edges.append(Edge(
                        m["ticker"], ev, asset, side, price, side_fair, e, settle,
                        strike_type=m.get("strike_type", ""),
                        floor_strike=(float(m["floor_strike"])
                                      if m.get("floor_strike") is not None else None),
                        cap_strike=(float(m["cap_strike"])
                                    if m.get("cap_strike") is not None else None),
                        market_fair=(mid if side == "yes" else 1 - mid)
                        if mid is not None else None))
            # The ladder owns the center/width verdict even with zero usable
            # quotes. Co-settling ranges inherit it and missing verdicts block.
            if ev.startswith(s["ladder"]):
                bad, detail, scenarios = _surface_verdict(
                    d, ladder_mids, markets_by_settle[settle])
                settle_verdict[settle] = bad
                if not bad:
                    settle_scenarios[settle] = scenarios
                if con:
                    model_iqr = _iqr(zip(d.K_grid, d._D))
                    market_iqr = _iqr(ladder_mids)
                    settle_stats[settle] = {
                        "model_center": d.median(), "model_iqr": model_iqr,
                        "market_center": _survival_quantile(ladder_mids, .5),
                        "market_iqr": market_iqr,
                        "width_ratio": (model_iqr / market_iqr
                                        if model_iqr is not None and market_iqr else None),
                        "repair_max": d.repair_max,
                    }
            else:
                bad = settle_verdict.get(settle, True)
                scenarios = settle_scenarios.get(settle, ())
                detail = ("inherited from co-settling ladder event" if settle in settle_verdict
                          else "missing co-settling ladder verdict")
            if con:
                stats = settle_stats.get(settle, {})
                shadow = regime.surface_ratios(
                    regimes.get(asset, {}), chain.index_price, settle - now,
                    stats.get("model_iqr"), stats.get("market_iqr"),
                )
                shadow["status"] = regime.STATUS
                db.record_surface(con, {
                    "ts": now, "event": ev, "asset": asset, "settle_ts": settle,
                    "model_version": config.MODEL_VERSION, **stats,
                    "profile_status": vol_profile.STATUS,
                    "profile_age_s": vol_profile.AGE_S,
                    "catalyst_factor": catalyst_factor, "catalyst_blocked": False,
                    "contracts": surface_contracts, "scenarios": scenarios,
                    "regime": shadow,
                })
            if bad:
                if con:
                    con.execute("INSERT INTO events VALUES(?,?,?)",
                                (now, "surface_alarm",
                                 f"{ev}: {detail}, {len(ev_edges)} edges dropped"))
                    con.commit()
            else:
                covered_tickers = {ticker for _, _, winners in scenarios for ticker in winners}
                uncovered = [edge for edge in ev_edges if edge.ticker not in covered_tickers]
                if con and uncovered:
                    con.execute("INSERT INTO events VALUES(?,?,?)",
                                (now, "surface_coverage",
                                 f"{ev}: {len(uncovered)} edges outside liquid ladder dropped"))
                    con.commit()
                for edge in ev_edges:
                    if edge.ticker not in covered_tickers:
                        continue
                    edge.scenarios = scenarios
                    edges.append(edge)
    if con and n_filtered:
        con.execute("INSERT INTO events VALUES(?,?,?)",
                    (now, "edge_filtered", f"{n_filtered} raw edges blocked by "
                     "price/ratio humility filters this cycle"))
        con.commit()
    if con and snap_rows:
        con.executemany("INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?)", snap_rows)
        con.executemany(
            "INSERT INTO edges(ts,ticker,side,fair,price,edge,size,acted,reason) "
            "VALUES(?,?,?,?,?,?,0,0,'scan')",
            [(now, e.ticker, e.side, e.fair, e.price, e.edge) for e in edges])
        con.commit()
    edges.sort(key=lambda e: -e.edge)
    return edges, next_settle


def fetch_all_chains() -> dict[str, Chain]:
    return {a: fetch_chain(s["deribit_ccy"]) for a, s in config.SERIES.items()}
