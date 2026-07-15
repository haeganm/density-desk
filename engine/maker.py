"""Post-only resting quotes for the maker band: edges too small to cross the
spread (taker fee + spread kills them) but positive at a price posted inside it.
Maker fills carry ~zero fee on these series (modeled conservatively until live
fills verify). Kalshi's book is the state of record; every doubt cancels.

Fail-safe bias, in order: disarm/kill/halt cancels everything; an edge that
stops being emitted cancels its quote (this includes catalyst windows, surface
alarms and fair decay — the scanner suppresses the edge in all three cases);
a scan failure passes an empty edge list, which also cancels everything.
"""
from __future__ import annotations
import time
import uuid

import httpx

import config
import db
from engine import fees, rails, scanner
from engine.executor import STAND_DOWN_FINAL_S, joint_quantity, kelly_contracts, open_exposure
from kalshi.client import KalshiAPIError, fp, fpc

POSTS_PER_CYCLE = 1              # gentle: one new quote per scan cycle


def _bought_side(order: dict) -> str | None:
    s = order.get("outcome_side")
    if s in ("yes", "no"):
        return s
    bs = order.get("side") or order.get("book_side")
    return "yes" if bs == "bid" else "no" if bs == "ask" else None


def _event(ticker: str) -> str:
    return ticker.rsplit("-", 1)[0]


def _desk_resting(kalshi) -> list[dict]:
    # the taker path is IOC-only, so any desk-prefixed resting order is ours
    return [o for o in kalshi.orders(status="resting")
            if (o.get("client_order_id") or "").startswith("dd-")]


def _still_clears(con, ticker: str, side: str, price: float, now: float) -> bool:
    """Hysteresis for a resting quote: our own bid becomes the best bid, which can
    stop the scanner emitting the edge (the maker candidate needs room INSIDE the
    spread). Cancelling then reposting churns queue position for nothing. Hold
    while the freshest fair still covers price + half the required margin."""
    row = con.execute(
        "SELECT fair, ts, bid, ask FROM snapshots WHERE ticker=? ORDER BY ts DESC LIMIT 1",
        (ticker,)).fetchone()
    if not row or row[0] is None or now - row[1] > 300:
        return False                                   # no fresh fair -> fail safe
    fair, _ts, bid, ask = row
    # snapshots store the RAW model fair (calibration must measure the model);
    # shrink it here exactly as the scanner does before judging our own quote
    mid = ((bid + ask) / 2 if bid and ask and 0 < bid <= ask < 1 else None)
    side_fair = scanner._trusted_fair(fair if side == "yes" else 1.0 - fair, mid, side)
    return side_fair - price >= config.MAKER_EDGE_MIN / 2


def _cancel(kalshi, con, order: dict, why: str) -> bool:
    oid = order.get("order_id")
    if not oid:
        return False
    try:
        kalshi.cancel_order(oid)
    except (KalshiAPIError, httpx.HTTPError) as ex:
        db.log_event(con, "maker_cancel_fail", f"{order.get('ticker')}: {ex}")
        con.commit()
        return False
    con.execute("UPDATE orders SET status=? WHERE order_id=?",
                (f"canceled: {why}", oid))
    db.log_event(con, "maker_cancel", f"{order.get('ticker')}: {why}")
    con.commit()
    return True


def _day_reserve(con, settle_ts: float, kept, pos_cost: dict,
                 candidate_risk: float) -> float:
    """Worst-case loss riding on the candidate's UTC day.

    A long-only binary loses its whole premium, so cost IS the worst case. Nets
    between co-settling legs are ignored on purpose: the daily rail reserves
    pessimistically, and a resting quote is a loss we have already offered.
    """
    day = int(settle_ts // 86400)
    total = candidate_risk
    for ticker, cost in [(t, c) for t, _side, c in kept] + list(pos_cost.items()):
        row = con.execute("SELECT settle_ts FROM contracts WHERE ticker=?",
                          (ticker,)).fetchone()
        if row and row[0] and int(row[0] // 86400) == day:
            total += cost
    return total


def _amend(kalshi, con, order: dict, edge, count: float) -> float | None:
    """Reprice a resting quote to a better (lower) price. Returns the new price,
    or None if the amend failed (caller keeps the old quote — still on the book)."""
    oid = order.get("order_id")
    if not oid:
        return None
    try:
        kalshi.amend_order(oid, edge.ticker, buy_side=edge.side, count=count,
                           price=edge.price,
                           client_order_id=order.get("client_order_id"))
    except (KalshiAPIError, httpx.HTTPError, ValueError) as ex:
        db.log_event(con, "maker_amend_fail", f"{edge.ticker}: {ex}")
        con.commit()
        return None
    con.execute("UPDATE orders SET price=?, fair=? WHERE order_id=?",
                (edge.price, edge.fair, oid))
    db.log_event(con, "maker_amend",
                 f"{edge.ticker} {edge.side}: repriced to {edge.price:.2f} "
                 f"(fair {edge.fair:.2f})")
    con.commit()
    return edge.price


def manage(kalshi, con, edges, portfolio, model_age_s: float = 0.0,
           kalshi_age_s: float = 0.0) -> list[str]:
    if not getattr(kalshi, "can_trade", False) or not config.MAKER_ENABLED:
        return []
    log: list[str] = []
    now = time.time()
    resting = _desk_resting(kalshi)
    armed = (config.ARMED_FILE.exists() and not config.KILL_FILE.exists()
             and rails.active_halt_until() is None)
    if not armed:
        n = sum(_cancel(kalshi, con, o, "disarmed") for o in resting)
        return [f"MAKER: canceled {n} resting (disarmed/halted)"] if n else []

    # ledger hygiene: rows still marked resting whose order left the book
    # (filled or exchange-canceled) get closed out so the journal stays truthful
    on_book = {o.get("order_id") for o in resting}
    for (oid,) in con.execute(
            "SELECT order_id FROM orders WHERE status='resting'").fetchall():
        if oid not in on_book:
            con.execute("UPDATE orders SET status='off_book' WHERE order_id=?", (oid,))
    con.commit()

    live = {(e.ticker, e.side): e for e in edges if getattr(e, "maker", False)}
    kept: list[tuple[str, str, float]] = []          # (ticker, side, cost)
    for o in resting:
        ticker, side = o.get("ticker"), _bought_side(o)
        count = fpc(o, "remaining_count_fp", "remaining_count")
        px = fp(o, "yes_price_dollars", "yes_price") if side == "yes" \
            else fp(o, "no_price_dollars", "no_price")
        settle = con.execute(
            "SELECT settle_ts FROM contracts WHERE ticker=?", (ticker,)).fetchone()
        if side is None or not ticker:
            _cancel(kalshi, con, o, "unparseable")
        elif settle and settle[0] - now < STAND_DOWN_FINAL_S:
            _cancel(kalshi, con, o, "final_window")
        elif (ticker, side) not in live and not _still_clears(con, ticker, side, px, now):
            # hysteresis: our own quote moves the book, so the scanner may stop
            # emitting the edge even though the posted price still clears; hold
            # the quote (and its queue position) while the latest snapshot fair
            # covers price + half margin. No fresh fair within 5 min -> cancel.
            _cancel(kalshi, con, o, "edge_gone")
        else:
            target = live.get((ticker, side))
            if (target is not None and abs(target.price - px) >= config.REQUOTE_TRIGGER
                    and target.price < px):
                # only ever amend DOWN: pay less for the same contract. Amending up
                # would chase a market moving against us and forfeit queue position.
                px = _amend(kalshi, con, o, target, count) or px
            kept.append((ticker, side, count * px))

    room = config.MAKER_MAX_RESTING - len(kept)
    if room <= 0 or not live:
        return log
    try:
        rails.check_global(con, model_age_s, kalshi_age_s)
    except rails.RailsHalt:
        return log                                    # cancels above already ran

    state = portfolio or {}
    cash, equity = state.get("cash", 0.0), state.get("equity", 0.0)
    pos_cost = dict(state.get("pos_cost", {}))
    pos_count = dict(state.get("pos_count", {}))
    exposure = open_exposure(kalshi, pos_cost, state.get("resting", []))
    max_expo = min(config.MAX_OPEN_EXPOSURE, 0.55 * equity)
    ev_cost: dict[str, float] = {}
    for t, c in pos_cost.items():
        ev_cost[_event(t)] = ev_cost.get(_event(t), 0.0) + c
    for t, _s, c in kept:
        ev_cost[_event(t)] = ev_cost.get(_event(t), 0.0) + c
    held = {(t, s) for t, s, _ in kept}

    posted = 0
    for e in sorted(live.values(), key=lambda e: e.edge, reverse=True):
        if posted >= min(POSTS_PER_CYCLE, room):
            break
        if (e.ticker, e.side) in held:
            continue
        if e.settle_ts - now < STAND_DOWN_FINAL_S + 2 * config.REFRESH_S:
            continue                                  # a quote needs time to work
        positions = {t: (pos_count[t], pos_cost.get(t, 0.0))
                     for t in pos_count
                     if any(t in w for _, _, w in getattr(e, "scenarios", ()))}
        max_n = kelly_contracts(e.fair, e.price, bankroll=cash)
        n, reason, _m = joint_quantity(e, positions, max_n, e.price, cash)
        if n < 1:
            continue
        cost = n * e.price
        risk = cost + fees.maker_fee(e.price, n)
        try:
            rails.check_order(cost, exposure, max_expo, risk_cost=risk)
            # a quote is an offer to lose: reserve it against the day's rail
            # BEFORE posting, exactly as the taker path does (executor.py:463)
            rails.check_daily_reserve(
                con, e.settle_ts, _day_reserve(con, e.settle_ts, kept, pos_cost, risk))
        except rails.RailsHalt:
            continue
        if ev_cost.get(_event(e.ticker), 0.0) + risk > config.PER_EVENT_CAP + 1e-9:
            continue
        coid = f"dd-mk-{uuid.uuid4().hex[:12]}"
        # journal BEFORE sending (same discipline as the taker path)
        con.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (now, "?", coid, e.ticker, e.side, "buy", n, e.price,
                     e.fair, "sent"))
        db.record_order_evidence(
            con, client_order_id=coid, ticker=e.ticker, city=e.city,
            settle_ts=e.settle_ts, event_profit_model=0.0, event_profit_market=0.0,
            expected_pnl=e.edge * n, worst_loss=risk,
            model_version=getattr(e, "model_version", config.MODEL_VERSION))
        con.commit()
        try:
            resp = kalshi.create_order(e.ticker, buy_side=e.side, count=n,
                                       price=e.price, post_only=True,
                                       client_order_id=coid)
        except (KalshiAPIError, httpx.HTTPError, ValueError) as ex:
            uncertain = (not isinstance(ex, KalshiAPIError)
                         or not 400 <= getattr(ex, "status", 0) < 500)
            con.execute("UPDATE orders SET status=? WHERE client_order_id=?",
                        (f"{'uncertain' if uncertain else 'error'}: {str(ex)[:120]}",
                         coid))
            db.log_event(con, "maker_reject", f"{e.ticker} {e.side}: {ex}")
            con.commit()
            if uncertain:
                break                                 # reconcile next cycle
            continue
        order = resp.get("order") or resp
        oid = order.get("order_id", "?")
        con.execute("UPDATE orders SET order_id=?, status='resting' "
                    "WHERE client_order_id=?", (oid, coid))
        db.link_order_evidence(con, coid, oid)
        con.commit()
        exposure += cost
        ev_cost[_event(e.ticker)] = ev_cost.get(_event(e.ticker), 0.0) + risk
        posted += 1
        log.append(f"QUOTE {e.side} {e.ticker} x{n} @ {e.price:.2f} "
                   f"(fair {e.fair:.2f}, maker edge +{e.edge:.3f})")
    return log
