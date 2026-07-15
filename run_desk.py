"""Weather Desk main loop: ensembles -> distributions -> scan -> execute -> journal.
Run: .venv\\Scripts\\python.exe run_desk.py   (or via scheduled task, see ops/install_tasks.py)
Kill switch: create file KILL in repo root."""
import datetime as dt
import json
import time
import traceback

import config
import db
from engine import executor, maker, scanner
from kalshi.client import KalshiClient, fp, fpc

STATE = config.ROOT / "state"


def _iso_ts(s: str | None) -> float:
    if not s:
        return time.time()
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def sync_portfolio(kalshi, con):
    """Journal fills and settlement P&L. Idempotent: fills dedup on fill_id,
    settle pnl recomputed absolutely each pass from the settlement object itself.
    Field names per docs.kalshi.com 2026-07 (fp/dollars strings; legacy fallbacks)."""
    if not kalshi.can_trade:
        return
    unresolved = con.execute(
        """SELECT client_order_id FROM orders
           WHERE order_id='?' AND (status LIKE 'uncertain:%' OR status='sent')"""
    ).fetchall()   # 'sent' = crashed/odd-body orders stranded between journal and ack
    if unresolved:
        wanted = {row[0] for row in unresolved}
        resolved = set()
        for order in kalshi.orders(status=None):
            client_id = order.get("client_order_id")
            order_id = order.get("order_id")
            if client_id not in wanted or not order_id:
                continue
            remote_status = order.get("status") or "found"
            con.execute(
                "UPDATE orders SET order_id=?,status=? WHERE client_order_id=?",
                (order_id, f"reconciled:{remote_status}", client_id),
            )
            db.link_order_evidence(con, client_id, order_id)
            resolved.add(client_id)
        missing = wanted - resolved
        if missing:
            raise RuntimeError(
                f"unresolved uncertain orders: {', '.join(sorted(missing))}"
            )
    for f in kalshi.fills(min_ts=time.time() - 3 * 86400):
        fid = f.get("fill_id") or f.get("trade_id")
        if not fid:
            continue                       # no stable id -> can't dedup, skip (B3)
        if f.get("client_order_id") and f.get("order_id"):
            con.execute(
                "UPDATE orders SET order_id=? WHERE client_order_id=? AND order_id='?'",
                (f["order_id"], f["client_order_id"]),
            )
            db.link_order_evidence(con, f["client_order_id"], f["order_id"])
        side = f.get("outcome_side") or f.get("side") or ""
        px = fp(f, "yes_price_dollars", "yes_price") if side == "yes" \
            else fp(f, "no_price_dollars", "no_price")
        fair_row = con.execute(
            "SELECT MAX(fair),COUNT(*) FROM orders WHERE order_id=?", (f.get("order_id"),)
        ).fetchone()
        fair = fair_row[0] if fair_row and fair_row[1] == 1 else None
        con.execute("INSERT OR IGNORE INTO fills VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (_iso_ts(f.get("created_time")), f.get("order_id"),
                     f.get("ticker") or f.get("market_ticker"), side,
                     f.get("action") or "buy", fpc(f, "count_fp", "count"), px,
                     fp(f, "fee_cost"),      # real fee, straight from the fill
                     fair, fid))
    for s in kalshi.settlements(min_ts=time.time() - 3 * 86400):
        t = s.get("ticker")
        if not t:
            continue
        rev = fp(s, "revenue_dollars", "revenue")   # dollars-string first, cents fallback
        settled_ts = _iso_ts(s.get("settled_time"))
        realized = 1 if s.get("market_result") == "yes" else 0
        # Kalshi nets offsetting yes/no lots when they meet and pays $1 per matched
        # pair THEN — that cash is not in `revenue`, which reports 0 for a position
        # flattened before settlement. Without the pairs term the whole cost basis
        # reads as a loss: two WINNING tickers booked -$4.04 on 2026-07-15 and
        # falsely tripped the daily rail while the balance never moved a cent.
        # Held-to-settlement tickers only ever touch one side, so pairs=0 and this
        # reduces to the original formula.
        sides = dict(con.execute(
            "SELECT side, COALESCE(SUM(count),0) FROM fills WHERE ticker=? GROUP BY side",
            (t,)).fetchall())
        pairs = min(sides.get("yes", 0), sides.get("no", 0))
        pnl = (rev + pairs
               - fp(s, "yes_total_cost_dollars")
               - fp(s, "no_total_cost_dollars")
               - fp(s, "fee_cost"))
        prior = con.execute("SELECT pnl FROM settles WHERE ticker=?", (t,)).fetchone()
        if con.execute("UPDATE settles SET ts=?,realized=?,pnl=? WHERE ticker=?",
                       (settled_ts, realized, pnl, t)).rowcount == 0:
            con.execute("INSERT INTO settles VALUES(?,?,?,?,?)",
                        (settled_ts, t, None, realized, pnl))
        elif prior and prior[0] is not None and abs(prior[0] - pnl) > 0.005:
            # settles are rewritten in place; keep an audit trail of revisions
            db.log_event(con, "settle_revision",
                         f"{t}: pnl {prior[0]:.4f} -> {pnl:.4f}")
        meta = con.execute(
            """SELECT city,settle_ts FROM contracts WHERE ticker=?
               UNION ALL SELECT city,settle_ts FROM snapshots WHERE ticker=?
               ORDER BY settle_ts DESC LIMIT 1""", (t, t)
        ).fetchone()
        if meta:
            expiration = next((s.get(k) for k in ("expiration_value", "settlement_value",
                                                   "expiration_value_dollars")
                               if s.get(k) is not None), None)
            db.record_outcome(con, meta[0], meta[1], expiration, settled_ts)
    con.commit()


def record_settlements(kalshi, con):
    """Finalize durable city/settlement outcomes from any observed contract."""
    pending = [r[0] for r in con.execute(
        """SELECT MIN(c.ticker) FROM contracts c
           WHERE c.settle_ts<=? AND NOT EXISTS (
             SELECT 1 FROM outcomes o WHERE o.city=c.city
               AND o.settle_ts=c.settle_ts AND o.expiration_value IS NOT NULL)
           GROUP BY c.city,c.settle_ts ORDER BY c.settle_ts DESC""",
        (time.time(),))]
    if not pending:
        return
    for m in kalshi.markets_by_tickers(pending):
        finalized = m.get("settlement_ts") or m.get("settled_time")
        if (m.get("status") != "finalized" or m.get("result") not in ("yes", "no")
                or not finalized):
            continue
        final_ts = _iso_ts(finalized)
        predicted = con.execute(
            "SELECT fair,settle_ts,city FROM snapshots WHERE ticker=? ORDER BY ts DESC LIMIT 1",
            (m["ticker"],)).fetchone()
        meta = con.execute(
            "SELECT city,settle_ts FROM contracts WHERE ticker=?", (m["ticker"],)
        ).fetchone()
        if not meta:
            continue
        fair = predicted[0] if predicted else None
        realized = 1 if m["result"] == "yes" else 0
        if con.execute("UPDATE settles SET ts=?,predicted=COALESCE(?,predicted),"
                       "realized=? WHERE ticker=?",
                       (final_ts, fair, realized, m["ticker"])).rowcount == 0:
            con.execute("INSERT INTO settles VALUES(?,?,?,?,?)",
                        (final_ts, m["ticker"], fair, realized, 0.0))
        expiration = next((m.get(k) for k in ("expiration_value", "settlement_value",
                                               "expiration_value_dollars")
                           if m.get(k) is not None), None)
        db.record_outcome(con, meta[0], meta[1], expiration, final_ts)
    con.commit()


def write_status(edges, actions, forecasts, err=None, portfolio=None, health=None, con=None):
    STATE.mkdir(exist_ok=True)
    health = health or {}
    balance = portfolio["cash"] if portfolio else None
    equity = portfolio["equity"] if portfolio else None
    positions = portfolio["positions"] if portfolio else []
    if con is not None and portfolio:
        con.execute("INSERT INTO equity VALUES(?,?,?)",
                    (time.time(), balance, equity - balance))
        con.commit()
    (STATE / "status.json").write_text(json.dumps({
        "ts": time.time(),
        "utc": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "forecast_center": {c: f.center for c, f in (forecasts or {}).items()},
        "edges": [vars(e) for e in edges[:10]],
        "actions": actions,
        "error": err,
        "scan_ok": health.get("scan_ok", False),
        "portfolio_sync_ok": health.get("portfolio_sync_ok", False),
        "portfolio_sync_ts": health.get("portfolio_sync_ts"),
        "clock_skew_s": health.get("clock_skew_s"),
        "kill": config.KILL_FILE.exists(),
        "shakeout": config.SHAKEOUT,
        "balance": balance,
        "portfolio_value": equity,
        "positions": positions,
    }, indent=1))


def run_cycle(kalshi, con):
    """Reconcile first; scanning survives sync failure, execution does not."""
    forecasts, edges, actions, next_settle = None, [], [], None
    portfolio, sync_ts, sync_clock_skew_s = None, None, None
    sync_ok = False
    scan_ok = False
    errors = []

    try:
        sync_portfolio(kalshi, con)
        if getattr(kalshi, "can_trade", False):
            portfolio = executor.portfolio_state(kalshi, con)
            sync_ok = True
            sync_ts = time.time()
            # Preserve the authenticated response's Date header. Public scan
            # requests run afterward and must not replace arm-health evidence.
            sync_clock_skew_s = getattr(kalshi, "clock_skew_s", None)
    except Exception:
        errors.append("portfolio sync:\n" + traceback.format_exc(limit=3))

    scan_start = time.time()
    try:
        forecasts = scanner.fetch_all_forecasts()
        edges, next_settle = scanner.scan(kalshi, forecasts, con)
        scan_ok = True
        if not getattr(kalshi, "can_trade", False) or sync_ok:
            model_age = time.time() - min(f.fetched_ts for f in forecasts.values())
            actions = executor.execute(kalshi, con, edges, model_age_s=model_age,
                                       kalshi_age_s=time.time() - scan_start,
                                       portfolio=portfolio)
            for action in actions:
                if action.startswith(("BUY", "HALT")):
                    db.log_event(con, "action", action)
    except Exception:
        errors.append("scan/execute:\n" + traceback.format_exc(limit=3))

    try:
        # runs on scan failure too: an empty edge list cancels every resting
        # quote (we can't price what we can't scan)
        if sync_ok:
            model_age = (time.time() - min(f.fetched_ts for f in forecasts.values())
                         if forecasts else 1e9)
            actions += maker.manage(kalshi, con, edges, portfolio,
                                    model_age_s=model_age,
                                    kalshi_age_s=time.time() - scan_start)
    except Exception:
        errors.append("maker:\n" + traceback.format_exc(limit=3))

    try:
        record_settlements(kalshi, con)
    except Exception:
        errors.append("settlement scoring:\n" + traceback.format_exc(limit=3))

    err = "\n".join(errors) or None
    if err:
        db.log_event(con, "error", err)
    health = {"scan_ok": scan_ok, "portfolio_sync_ok": sync_ok,
              "portfolio_sync_ts": sync_ts,
              "clock_skew_s": sync_clock_skew_s}
    return forecasts, edges, actions, err, next_settle, portfolio, health


def main():
    import socket
    import sys
    if sys.stdout is None or sys.stderr is None:   # pythonw: print() would crash
        STATE.mkdir(exist_ok=True)
        log = open(STATE / "desk.log", "a", buffering=1, encoding="utf-8")
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log
    lock = socket.socket()
    try:
        lock.bind(("127.0.0.1", 8778))   # singleton: watchdog schtask can fire freely
        lock.listen(1)                   # listen so the lock shows up in netstat
    except OSError:
        return
    con = db.connect()
    backfilled = db.backfill_fill_fairs(con)
    kalshi = KalshiClient()
    db.log_event(con, "start", f"desk started, can_trade={kalshi.can_trade}, "
                 f"model={config.MODEL_VERSION}, fill_fairs_backfilled={backfilled}")
    last_prune = 0.0
    while True:
        if time.time() - last_prune > 6 * 3600:
            db.prune(con)
            last_prune = time.time()
        forecasts, edges, actions, err, next_settle, portfolio, health = run_cycle(kalshi, con)
        write_status(edges, actions, forecasts, err, portfolio, health, con)
        print(f"[{dt.datetime.now():%H:%M:%S}] edges={len(edges)} {actions[:2] if actions else ''}"
              + (" ERR" if err else ""))
        final = next_settle and (next_settle - time.time()) < config.FINAL_WINDOW_S
        time.sleep(config.REFRESH_FINAL_HOUR_S if final else config.REFRESH_S)


if __name__ == "__main__":
    main()
