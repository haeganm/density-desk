"""Order execution: taker on big edges. All orders pass through rails; the
journal row is written BEFORE the order is sent (a transport timeout may still
have executed server-side — the desk must never hold an unjournaled position)."""
from __future__ import annotations
from dataclasses import dataclass
import math
import time
import uuid

import httpx

import config
import db
from engine import fees, rails
from engine.scanner import Edge, STAND_DOWN_FINAL_S
from kalshi.client import KalshiAPIError, fp, fpc


def kelly_contracts(fair: float, price: float, bankroll: float = 0.0) -> int:
    """¼-Kelly contracts using entry price plus the modeled taker fee."""
    entry_cost = price + fees.taker_fee(price, 1)
    if price <= 0 or entry_cost >= 1 or fair <= entry_cost:
        return 0
    stake = (config.KELLY_FRACTION * max(0.0, bankroll)
             * (fair - entry_cost) / (1 - entry_cost))
    stake = min(stake, rails.per_trade_cap())
    return int(stake / entry_cost)


@dataclass(frozen=True)
class JointMetrics:
    model_ev: float
    market_ev: float
    model_profit_p: float
    market_profit_p: float
    worst_loss: float
    model_log_wealth: float
    market_log_wealth: float


def joint_metrics(scenarios, positions: dict[str, tuple[float, float]],
                  candidate: tuple[str, str, float, float] | None = None,
                  bankroll: float = 0.0) -> JointMetrics | None:
    """Value a co-settling long-only book over exhaustive terminal scenarios.

    scenarios are (model_probability, market_probability, yes_winning_tickers).
    positions map ticker -> (signed contracts, entry cost including fees).
    candidate is (ticker, side, contracts, entry cost including fees).
    """
    if not scenarios:
        return None
    try:
        model_total = sum(float(s[0]) for s in scenarios)
        market_total = sum(float(s[1]) for s in scenarios)
        if (not math.isfinite(model_total) or not math.isfinite(market_total)
                or abs(model_total - 1.0) > 1e-6
                or abs(market_total - 1.0) > 1e-6):
            return None
        total_cost = sum(max(0.0, float(v[1])) for v in positions.values())
        candidate_cost = 0.0 if candidate is None else max(0.0, float(candidate[3]))
        total_cost += candidate_cost
        covered = {ticker for _, _, winners in scenarios for ticker in winners}
        required = set(positions)
        if candidate is not None:
            required.add(candidate[0])
        if not required <= covered:
            return None
        rows = []
        for model_p, market_p, winners in scenarios:
            model_p, market_p = float(model_p), float(market_p)
            if (not math.isfinite(model_p) or not math.isfinite(market_p)
                    or model_p < 0 or market_p < 0):
                return None
            winners = set(winners)
            payout = sum(abs(count) for ticker, (count, _) in positions.items()
                         if bool(count > 0) == (ticker in winners))
            if candidate is not None:
                ticker, side, count, _ = candidate
                if (side == "yes") == (ticker in winners):
                    payout += count
            profit = payout - total_cost
            wealth = max(1e-9, bankroll + payout - candidate_cost)
            rows.append((model_p / model_total, market_p / market_total,
                         profit, math.log(wealth)))
    except (TypeError, ValueError, IndexError):
        return None
    return JointMetrics(
        sum(mp * profit for mp, _, profit, _ in rows),
        sum(kp * profit for _, kp, profit, _ in rows),
        sum(mp for mp, _, profit, _ in rows if profit > 1e-9),
        sum(kp for _, kp, profit, _ in rows if profit > 1e-9),
        max(0.0, -min(profit for _, _, profit, _ in rows)),
        sum(mp * log_wealth for mp, _, _, log_wealth in rows),
        sum(kp * log_wealth for _, kp, _, log_wealth in rows),
    )


def joint_quantity(edge: Edge, positions: dict[str, tuple[float, float]], max_n: int,
                   price: float, bankroll: float) -> tuple[int, str, JointMetrics | None]:
    """Largest contiguous one-lot addition passing EV, log-growth and hit-rate gates."""
    scenarios = getattr(edge, "scenarios", ())
    base = joint_metrics(scenarios, positions, bankroll=bankroll)
    if base is None:
        return 0, "portfolio_evidence", None
    accepted, previous_model_ev, previous_market_ev, previous_model_log, \
        previous_market_log, last = (
            0, base.model_ev, base.market_ev, base.model_log_wealth,
            base.market_log_wealth, base,
        )
    reason = "joint_ev"
    for n in range(1, max_n + 1):
        risk = n * price + fees.taker_fee(price, n)
        metrics = joint_metrics(scenarios, positions,
                                (edge.ticker, edge.side, n, risk), bankroll)
        if metrics is None:
            return accepted, "portfolio_evidence", last
        if (min(metrics.model_profit_p, metrics.market_profit_p)
                < config.MIN_EVENT_PROFIT_PROB - 1e-9):
            reason = "profit_probability"
            break
        if (metrics.model_ev <= 1e-9
                or metrics.model_ev <= previous_model_ev + 1e-9
                or metrics.market_ev <= 1e-9
                or metrics.market_ev <= previous_market_ev + 1e-9):
            reason = "joint_ev"
            break
        if (metrics.model_log_wealth <= previous_model_log + 1e-12
                or metrics.market_log_wealth <= previous_market_log + 1e-12):
            reason = "log_utility"
            break
        accepted, previous_model_ev, previous_market_ev, previous_model_log, \
            previous_market_log, last = (
                n, metrics.model_ev, metrics.market_ev, metrics.model_log_wealth,
                metrics.market_log_wealth, metrics,
            )
        reason = "joint_ok"
    return accepted, reason, last


def edge_priority(edge: Edge, positions: dict[str, tuple[float, float]],
                  bankroll: float) -> tuple[float, float, float, float]:
    """Rank viable events by positive-day evidence, with raw edge only last."""
    base = joint_metrics(edge.scenarios, positions, bankroll=bankroll)
    risk = edge.price + fees.taker_fee(edge.price, 1)
    candidate = joint_metrics(
        edge.scenarios, positions, (edge.ticker, edge.side, 1, risk), bankroll
    )
    if base is None or candidate is None:
        return (-1.0, -math.inf, -math.inf, edge.edge)
    return (
        min(candidate.model_profit_p, candidate.market_profit_p),
        min(candidate.model_log_wealth - base.model_log_wealth,
            candidate.market_log_wealth - base.market_log_wealth),
        min(candidate.model_ev - base.model_ev,
            candidate.market_ev - base.market_ev),
        edge.edge,
    )


def _max_qty_for_risk(price: float, room: float, maximum: int) -> int:
    n = min(maximum, int(max(0.0, room) / price))
    while n and n * price + fees.taker_fee(price, n) > room + 1e-9:
        n -= 1
    return n


def _ask_depth(book: dict, side: str) -> tuple[float, float]:
    """(ask_price_dollars, contracts available) for buying `side`.
    Buying yes crosses resting no bids and vice versa. Live shape (verified
    2026-07-13): {"orderbook_fp": {"yes_dollars": [["0.5500","5200.00"],...],
    "no_dollars": [...]}} — resting BIDS per side, dollar strings, ascending."""
    fp_book = book.get("orderbook_fp") or {}
    levels = fp_book.get("no_dollars" if side == "yes" else "yes_dollars") or []
    if not levels:
        return 1.0, 0.0
    px, qty = max(((float(p), float(q)) for p, q in levels), key=lambda lv: lv[0])
    return 1.0 - px, qty


def position_state(kalshi, positions=None) -> tuple[dict[str, float], dict[str, float]]:
    """({ticker: aggregate cost $}, {ticker: signed contract count})."""
    cost, count = {}, {}
    for p in positions if positions is not None else kalshi.positions():
        t = p.get("ticker")
        if not t:
            continue
        exposure = fp(p, "market_exposure_dollars", "market_exposure")
        contracts = fpc(p, "position_fp", "position")
        if abs(exposure) < 1e-9 and abs(contracts) < 1e-9:
            continue
        cost[t] = exposure
        count[t] = contracts
    return cost, count


def marked_value(con, ticker: str, count: float, cost: float) -> float:
    """Position value at the latest snapshot mid (cost basis lies about drawdown:
    a $5 position that's now worthless must read ~$0, not $5)."""
    if not con or not count:
        return cost
    row = con.execute("SELECT bid, ask FROM snapshots WHERE ticker=? "
                      "ORDER BY ts DESC LIMIT 1", (ticker,)).fetchone()
    if not row:
        return cost
    mid = ((row[0] or 0.0) + (row[1] if row[1] is not None else 1.0)) / 2
    per = mid if count > 0 else 1.0 - mid
    return abs(count) * per


def marked_equity(con, cash: float, pos_cost: dict, pos_count: dict) -> float:
    return cash + sum(marked_value(con, t, pos_count.get(t, 0.0), c)
                      for t, c in pos_cost.items())


def portfolio_state(kalshi, con=None) -> dict:
    """Fetch one coherent-enough portfolio view for execution and reporting."""
    balance = kalshi.balance()
    raw_positions = kalshi.positions()
    resting = kalshi.orders(status="resting")
    cash = fp(balance, "balance_dollars", "balance")
    pos_cost, pos_count = position_state(kalshi, raw_positions)
    position_value = fp(balance, "portfolio_value_dollars", "portfolio_value", default=None)
    if position_value is None:               # legacy API fallback
        equity = marked_equity(con, cash, pos_cost, pos_count)
    else:
        equity = cash + position_value        # live API field is positions-only
    positions = [{"ticker": t, "count": pos_count[t], "cost": pos_cost[t]}
                 for t in pos_cost]
    return {"cash": cash, "equity": equity, "positions": positions,
            "pos_cost": pos_cost, "pos_count": pos_count, "resting": resting}


def open_exposure(kalshi, pos_cost: dict[str, float] | None = None,
                  resting_orders=None) -> float:
    """Cost basis of open positions + resting orders, dollars."""
    exp = sum((pos_cost if pos_cost is not None else position_state(kalshi)[0]).values())
    orders = resting_orders if resting_orders is not None else kalshi.orders(status="resting")
    for o in orders:
        side = o.get("outcome_side")
        if side not in ("yes", "no"):
            raw_side = o.get("side")
            if raw_side in ("yes", "no"):
                side = raw_side
            else:
                book_side = o.get("book_side") or raw_side
                side = "yes" if book_side == "bid" else "no" if book_side == "ask" else None
        if side not in ("yes", "no"):
            raise ValueError(f"unrecognized resting order side for {o.get('ticker', '?')}")
        px = (fp(o, "yes_price_dollars", "yes_price") if side == "yes"
              else fp(o, "no_price_dollars", "no_price"))
        exp += fpc(o, "remaining_count_fp", "remaining_count") * px
    return exp


def _log_halt_once(con, msg: str) -> None:
    last = con.execute("SELECT detail FROM events WHERE kind='halt' "
                       "ORDER BY ts DESC LIMIT 1").fetchone()
    if not last or last[0] != msg:            # don't re-log an unchanged halt every cycle
        con.execute("INSERT INTO events VALUES(?,?,?)", (time.time(), "halt", msg))
        con.commit()


def _strategy_city(ticker: str) -> str | None:
    """Which city's book does this ticker belong to? One series per city now."""
    for city, cfg in config.CITIES.items():
        if ticker.startswith(cfg["series"] + "-"):
            return city
    return None


def _mark_edge(con, edge: Edge, reason: str, size: float = 0, acted: int = 0) -> None:
    """Update only this scan's newest observation, never a ten-minute history."""
    con.execute(
        """UPDATE edges SET acted=?,size=?,reason=? WHERE rowid=(
               SELECT rowid FROM edges WHERE ticker=? AND side=?
               ORDER BY ts DESC,rowid DESC LIMIT 1)""",
        (acted, size, reason, edge.ticker, edge.side),
    )


def execute(kalshi, con, edges: list[Edge], model_age_s: float,
            kalshi_age_s: float = 0.0, portfolio: dict | None = None) -> list[str]:
    """Take the best edges within rails. Returns human-readable action log."""
    log: list[str] = []
    if not kalshi.can_trade:
        return ["no API key: scan-only mode"]
    if not config.ARMED_FILE.exists():         # dashboard toggle; default DISARMED
        return ["DISARMED: scan-only (arm on dashboard)"]
    try:
        rails.check_global(con, model_age_s, kalshi_age_s)
    except rails.RailsHalt as e:
        _log_halt_once(con, str(e))
        return [f"HALT: {e}"]

    state = portfolio if portfolio is not None else portfolio_state(kalshi, con)
    cash, equity = state["cash"], state["equity"]
    pos = dict(state["pos_cost"])
    pos_count = dict(state["pos_count"])
    exposure = open_exposure(kalshi, pos, state["resting"])
    try:
        rails.drawdown_check(con, equity)
    except rails.RailsHalt as e:
        return [f"HALT: {e}"]
    max_expo = min(config.MAX_OPEN_EXPOSURE, 0.55 * equity)   # deployment shrinks
    ticker_risk = dict(pos)
    ev_cost: dict[tuple[str, int], float] = {}
    ev_positions: dict[tuple[str, int], dict[str, tuple[float, float]]] = {}
    ev_reserved: dict[tuple[str, int], float] = {}
    day_reserved: dict[int, float] = {}
    unmapped_assets: set[str] = set()
    scope_ts = max(time.time() - 7 * 86400, config.STRATEGY_EPOCH)
    for t, c in pos.items():
        desk_owned = con.execute(
            "SELECT 1 FROM orders WHERE ticker=? AND ts>=? LIMIT 1", (t, scope_ts)
        ).fetchone()
        if not desk_owned:                       # manual positions: global exposure only
            continue
        versioned = con.execute(
            "SELECT 1 FROM order_evidence WHERE ticker=? LIMIT 1", (t,)
        ).fetchone()
        if versioned:
            desk_count = float(con.execute(
                """SELECT COALESCE(SUM(CASE WHEN o.side='yes' THEN f.count
                                             ELSE -f.count END),0)
                   FROM fills f JOIN orders o ON o.order_id=f.order_id
                   JOIN order_evidence oe ON oe.client_order_id=o.client_order_id
                   WHERE f.ticker=? AND o.ts>=?""", (t, scope_ts)
            ).fetchone()[0] or 0.0)
            if abs(desk_count - pos_count.get(t, 0.0)) > 1e-6:
                city = _strategy_city(t)
                if city:
                    unmapped_assets.add(city)
                continue
        row = con.execute("SELECT city,settle_ts FROM snapshots WHERE ticker=? "
                          "ORDER BY ts DESC LIMIT 1", (t,)).fetchone()
        if row and row[0] and row[1] is not None:
            key = (row[0], int(row[1]))
            entry_fee = float(con.execute(
                """SELECT COALESCE(SUM(f.fee),0) FROM fills f JOIN orders o
                     ON o.order_id=f.order_id WHERE f.ticker=? AND f.action='buy'
                     AND o.ts>=?""", (t, scope_ts)
            ).fetchone()[0] or 0.0)
            risk = c + entry_fee
            ticker_risk[t] = risk
            ev_cost[key] = ev_cost.get(key, 0.0) + risk
            ev_positions.setdefault(key, {})[t] = (pos_count.get(t, 0.0), risk)
        else:
            city = _strategy_city(t)
            if city:
                unmapped_assets.add(city)
    for key, event_cost in ev_cost.items():
        row = con.execute(
            """SELECT oe.worst_loss FROM order_evidence oe JOIN orders o
                 ON o.client_order_id=oe.client_order_id
               WHERE oe.model_version=? AND oe.city=? AND CAST(oe.settle_ts AS INT)=?
                 AND EXISTS (SELECT 1 FROM fills f WHERE f.order_id=oe.order_id
                             AND f.action='buy' AND f.count>0)
               ORDER BY o.ts DESC LIMIT 1""",
            (config.MODEL_VERSION, key[0], key[1]),
        ).fetchone()
        reserved = float(row[0]) if row and row[0] is not None else event_cost
        if not math.isfinite(reserved) or reserved < 0:
            reserved = event_cost
        ev_reserved[key] = reserved
        day = int(key[1] // 86400)
        day_reserved[day] = day_reserved.get(day, 0.0) + reserved
    edges = sorted(
        (e for e in edges if not getattr(e, "maker", False)),   # maker.manage owns those
        key=lambda edge: edge_priority(
            edge, ev_positions.get((edge.city, int(edge.settle_ts)), {}), cash
        ),
        reverse=True,
    )
    placed = 0
    paused: set[str] = set()
    submitted_keys: set[tuple[str, int]] = set()
    for i, e in enumerate(edges):
        if placed >= config.ORDERS_PER_CYCLE:         # pacing: don't deploy the whole
            for pending in edges[i:]:
                _mark_edge(con, pending, "pacing")
            break
        if e.event in paused:
            _mark_edge(con, e, "api_rejection")
            continue
        risk_key = (e.city, int(e.settle_ts))
        if risk_key in submitted_keys:
            _mark_edge(con, e, "event_pacing")
            continue
        held_count = pos_count.get(e.ticker, 0.0)
        if held_count and ((held_count > 0) != (e.side == "yes")):
            _mark_edge(con, e, "opposite_position")
            continue
        if e.city in unmapped_assets:
            _mark_edge(con, e, "unmapped_position")
            continue
        held_risk = ticker_risk.get(e.ticker, 0.0)
        ticker_room = rails.per_trade_cap() - held_risk
        if ticker_room < 0.01:
            _mark_edge(con, e, "ticker_cap")
            continue
        event_room = config.PER_EVENT_CAP - ev_cost.get(risk_key, 0.0)
        if event_room < 0.01:
            _mark_edge(con, e, "event_cap")
            continue
        global_room = max_expo - exposure
        if global_room < 0.01:
            _mark_edge(con, e, "global_cap")
            continue
        try:
            ask, depth = _ask_depth(kalshi.orderbook(e.ticker), e.side)
        except (KalshiAPIError, httpx.HTTPError) as ex:
            _mark_edge(con, e, "api_rejection")
            con.execute("INSERT INTO events VALUES(?,?,?)",
                        (time.time(), "order_reject", f"book {e.ticker}: {ex}"))
            con.commit()
            if isinstance(ex, KalshiAPIError) and "pause" in ex.body.lower():
                paused.add(e.event)
            continue
        if depth < 1:
            _mark_edge(con, e, "no_depth")
            continue
        if ask > e.price + 1e-9:                      # book moved away since scan
            _mark_edge(con, e, "moved_book")
            continue
        # fill-time humility re-check: a price that COLLAPSED since the scan is
        # the most adverse-selection-prone fill there is — same filters as scan
        if not config.MIN_BUY_PRICE <= ask <= config.MAX_BUY_PRICE \
                or e.fair > config.MAX_FAIR_RATIO * ask:
            _mark_edge(con, e, "humility")
            continue
        kelly_n = kelly_contracts(e.fair, ask, cash)
        if kelly_n < 1:
            _mark_edge(con, e, "zero_kelly")
            continue
        target_room = int(max(0.0, math.floor(kelly_n - abs(held_count) + 1e-9)))
        if target_room < 1:
            _mark_edge(con, e, "kelly_target")
            continue
        room_n = min(_max_qty_for_risk(ask, min(ticker_room, event_room), target_room),
                     int(max(0.0, global_room) / ask))
        if room_n < 1:
            reason = ("global_cap" if global_room <= min(event_room, ticker_room)
                      else "event_cap" if event_room <= ticker_room else "ticker_cap")
            _mark_edge(con, e, reason)
            continue
        max_n = min(int(depth), room_n)
        n, joint_reason, metrics = joint_quantity(
            e, ev_positions.get(risk_key, {}), max_n, ask, cash
        )
        if n < 1:
            _mark_edge(con, e, joint_reason)
            continue
        day = int(e.settle_ts // 86400)
        existing_event_reserve = ev_reserved.get(risk_key, 0.0)
        while n:
            order_risk = n * ask + fees.taker_fee(ask, n)
            metrics = joint_metrics(
                getattr(e, "scenarios", ()), ev_positions.get(risk_key, {}),
                (e.ticker, e.side, n, order_risk), cash,
            )
            reserved = (day_reserved.get(day, 0.0) - existing_event_reserve
                        + (metrics.worst_loss if metrics
                           else existing_event_reserve + order_risk))
            try:
                rails.check_daily_reserve(con, e.settle_ts, reserved)
                break
            except rails.RailsHalt:
                n -= 1
        if n < 1:
            _mark_edge(con, e, "daily_reserve")
            continue
        cost = n * ask
        order_risk = cost + fees.taker_fee(ask, n)
        try:
            rails.check_order(cost, exposure, max_exposure=max_expo,
                              risk_cost=order_risk)
        except rails.RailsHalt as ex:
            _mark_edge(con, e, "global_cap")
            log.append(f"skip {e.ticker}: {ex}")
            continue
        # Recheck operator and time-sensitive rails immediately before every
        # submission. A scan can take long enough for either state to change.
        if not config.ARMED_FILE.exists():
            for pending in edges[i:]:
                _mark_edge(con, pending, "disarmed")
            log.append("DISARMED: stopped before next order")
            break
        try:
            rails.check_global(con, model_age_s, kalshi_age_s)
        except rails.RailsHalt as ex:
            reason = "kill" if config.KILL_FILE.exists() else "global_halt"
            for pending in edges[i:]:
                _mark_edge(con, pending, reason)
            _log_halt_once(con, str(ex))
            log.append(f"HALT: {ex}")
            break
        # send-time edge re-check. (No catalyst policy on weather: there is no
        # exogenous macro calendar to widen for -- the forecast IS the event.)
        live_edge = e.fair - ask - fees.taker_fee(ask, 1)
        if live_edge <= config.TAKER_EDGE_MIN:
            _mark_edge(con, e, "edge_gone")
            continue
        if e.settle_ts - time.time() < STAND_DOWN_FINAL_S:
            _mark_edge(con, e, "final_window")
            continue
        coid = f"dd-{uuid.uuid4().hex[:12]}"
        # journal BEFORE sending: a transport timeout can still fill server-side,
        # and an unjournaled ticker would evade the daily-loss rail's scoping
        con.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (time.time(), "?", coid, e.ticker, e.side, "buy", n, ask,
                     e.fair, "sent"))
        db.record_order_evidence(
            con, client_order_id=coid, ticker=e.ticker, city=e.city,
            settle_ts=e.settle_ts, event_profit_model=metrics.model_profit_p,
            event_profit_market=metrics.market_profit_p,
            expected_pnl=metrics.model_ev, worst_loss=metrics.worst_loss,
            model_version=getattr(e, "model_version", config.MODEL_VERSION),
        )
        con.commit()
        submitted_keys.add(risk_key)
        placed += 1
        try:
            resp = kalshi.create_order(e.ticker, buy_side=e.side, count=n, price=ask,
                                       client_order_id=coid)
        except (KalshiAPIError, httpx.HTTPError, ValueError) as ex:
            # ValueError: a 2xx with a non-JSON body — the order may have executed
            uncertain = (not isinstance(ex, KalshiAPIError)
                         or not 400 <= getattr(ex, "status", 0) < 500)
            reason = "uncertain_order" if uncertain else "api_rejection"
            _mark_edge(con, e, reason, acted=1)
            status = f"{'uncertain' if uncertain else 'error'}: {str(ex)[:120]}"
            con.execute("UPDATE orders SET status=? WHERE client_order_id=?",
                        (status, coid))
            con.execute("INSERT INTO events VALUES(?,?,?)",
                        (time.time(), "order_uncertain" if uncertain else "order_reject",
                         f"{e.ticker} {e.side}: {ex}"))
            con.commit()
            if uncertain:
                for pending in edges[i + 1:]:
                    _mark_edge(con, pending, "uncertain_order")
                log.append(f"HALT: uncertain outcome for {e.ticker}; reconcile next cycle")
                break
            if isinstance(ex, KalshiAPIError) and "pause" in ex.body.lower():
                paused.add(e.event)
            continue
        order = resp.get("order") or resp
        oid = order.get("order_id", "?")
        filled = min(float(n), max(0.0, fpc(order, "fill_count_fp", "fill_count")))
        book_px = fp(order, "average_fill_price",
                     default=ask if e.side == "yes" else 1.0 - ask)
        avg_px = book_px if e.side == "yes" else 1.0 - book_px
        avg_fee = fp(order, "average_fee_paid")
        status = f"ioc filled {filled:g}/{n}"
        reason = "zero_fill" if not filled else "full_fill" if filled == n else "partial_fill"
        con.execute("UPDATE orders SET order_id=?, status=? WHERE client_order_id=?",
                    (oid, status, coid))
        db.link_order_evidence(con, coid, oid)
        _mark_edge(con, e, reason, filled, 1)
        con.commit()
        actual_cost = filled * avg_px
        actual_fee = (filled * avg_fee if avg_fee
                      else fees.taker_fee(avg_px, filled))
        actual_risk = actual_cost + actual_fee
        actual_metrics = joint_metrics(
            getattr(e, "scenarios", ()), ev_positions.get(risk_key, {}),
            (e.ticker, e.side, filled, actual_risk) if filled else None, cash,
        )
        if actual_metrics is not None:
            db.record_order_evidence(
                con, client_order_id=coid, order_id=oid, ticker=e.ticker,
                city=e.city, settle_ts=e.settle_ts,
                event_profit_model=actual_metrics.model_profit_p,
                event_profit_market=actual_metrics.market_profit_p,
                expected_pnl=actual_metrics.model_ev,
                worst_loss=actual_metrics.worst_loss,
                model_version=getattr(e, "model_version", config.MODEL_VERSION),
            )
            con.commit()
        cash = max(0.0, cash - actual_risk)
        exposure += actual_cost
        pos[e.ticker] = pos.get(e.ticker, 0.0) + actual_cost
        ticker_risk[e.ticker] = ticker_risk.get(e.ticker, 0.0) + actual_risk
        signed_fill = filled if e.side == "yes" else -filled
        pos_count[e.ticker] = pos_count.get(e.ticker, 0.0) + signed_fill
        previous_event_risk = ev_cost.get(risk_key, 0.0)
        ev_cost[risk_key] = previous_event_risk + actual_risk
        event_positions = ev_positions.setdefault(risk_key, {})
        event_positions[e.ticker] = (pos_count[e.ticker], ticker_risk[e.ticker])
        post = joint_metrics(getattr(e, "scenarios", ()), event_positions,
                             bankroll=cash)
        previous_event_reserve = ev_reserved.get(risk_key, 0.0)
        new_event_reserve = post.worst_loss if post else ev_cost[risk_key]
        ev_reserved[risk_key] = new_event_reserve
        day_reserved[day] = (day_reserved.get(day, 0.0) - previous_event_reserve
                             + new_event_reserve)
        verb = "BUY" if filled else "IOC"
        log.append(f"{verb} {e.side} {e.ticker} [{status}] @ ask {ask:.2f}"
                   + (f" avg {avg_px:.2f} fee {avg_fee:.2f}" if filled else "")
                   + f" (fair {e.fair:.2f}, edge {e.edge:+.3f})")
    con.commit()
    return log or ["no actionable edges"]
