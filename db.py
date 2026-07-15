"""SQLite journal. One connection per process; WAL so dashboard reads while desk writes."""
import datetime as dt
import json
import sqlite3
import time

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots(   -- every scan cycle, per market
  ts REAL, ticker TEXT, event TEXT, asset TEXT, strike REAL, settle_ts REAL,
  fair REAL, bid REAL, ask REAL, index_price REAL);
CREATE TABLE IF NOT EXISTS edges(       -- actionable edges found
  ts REAL, ticker TEXT, side TEXT, fair REAL, price REAL, edge REAL,
  size INTEGER, acted INTEGER DEFAULT 0, reason TEXT);
CREATE TABLE IF NOT EXISTS orders(
  ts REAL, order_id TEXT, client_order_id TEXT, ticker TEXT, side TEXT,
  action TEXT, count INTEGER, price REAL, fair REAL, status TEXT);
CREATE TABLE IF NOT EXISTS fills(
  ts REAL, order_id TEXT, ticker TEXT, side TEXT, action TEXT,
  count INTEGER, price REAL, fee REAL, fair_at_entry REAL, trade_id TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS idx_fills_trade ON fills(trade_id);
CREATE TABLE IF NOT EXISTS settles(     -- for calibration: predicted vs realized
  ts REAL, ticker TEXT, predicted REAL, realized INTEGER, pnl REAL);
CREATE TABLE IF NOT EXISTS events(      -- halts, alarms, operator notes
  ts REAL, kind TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS equity(      -- per-cycle snapshots (peak/trough, sparkline)
  ts REAL, cash REAL, open_cost REAL);
CREATE TABLE IF NOT EXISTS contracts(   -- immutable payoff metadata, latest API truth
  ticker TEXT PRIMARY KEY, event TEXT, asset TEXT, settle_ts REAL,
  strike_type TEXT, floor_strike REAL, cap_strike REAL);
CREATE TABLE IF NOT EXISTS surfaces(    -- one prospective, versioned event surface per scan
  ts REAL, event TEXT, asset TEXT, settle_ts REAL, model_version TEXT,
  model_center REAL, model_iqr REAL, market_center REAL, market_iqr REAL,
  width_ratio REAL, repair_max REAL, profile_status TEXT, profile_age_s REAL,
  catalyst_factor REAL, catalyst_blocked INTEGER, contracts_json TEXT,
  regime_json TEXT,
  PRIMARY KEY(ts,event,model_version));
CREATE TABLE IF NOT EXISTS outcomes(    -- asset-settlement result, not one row per contract
  asset TEXT, settle_ts REAL, expiration_value REAL, finalized_ts REAL,
  PRIMARY KEY(asset,settle_ts));
CREATE TABLE IF NOT EXISTS order_evidence( -- exact model/order attribution; manual orders absent
  client_order_id TEXT PRIMARY KEY, order_id TEXT, ticker TEXT,
  model_version TEXT, asset TEXT, settle_ts REAL,
  event_profit_model REAL, event_profit_market REAL,
  expected_pnl REAL, worst_loss REAL);
CREATE INDEX IF NOT EXISTS idx_snap_ts ON snapshots(ts);
CREATE INDEX IF NOT EXISTS idx_snap_ticker ON snapshots(ticker, ts);
CREATE INDEX IF NOT EXISTS idx_edges_ts ON edges(ts);
CREATE INDEX IF NOT EXISTS idx_orders_id ON orders(order_id);
CREATE INDEX IF NOT EXISTS idx_surfaces_version ON surfaces(model_version,asset,settle_ts,ts);
CREATE INDEX IF NOT EXISTS idx_order_evidence_id ON order_evidence(order_id);
"""

RETENTION = {"snapshots": 14, "edges": 30, "surfaces": 90,
             "events": 90, "equity": 180}  # days


def prune(con):
    """~520k snapshot rows/day accumulate otherwise (measured live 2026-07-14)."""
    import time as _t
    for table, days in RETENTION.items():
        con.execute(f"DELETE FROM {table} WHERE ts < ?", (_t.time() - days * 86400,))
    con.commit()


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(config.DB_PATH, timeout=10)
    con.execute("PRAGMA journal_mode=WAL")
    try:  # pre-audit DBs lack fills.trade_id; add before SCHEMA's unique index
        con.execute("ALTER TABLE fills ADD COLUMN trade_id TEXT")
    except sqlite3.OperationalError:
        pass  # column exists or table doesn't yet (SCHEMA creates it)
    con.executescript(SCHEMA)
    return con


def log_event(con, kind: str, detail: str):
    con.execute("INSERT INTO events VALUES(?,?,?)", (time.time(), kind, detail))
    con.commit()


def record_contracts(con, rows) -> None:
    """Persist the payoff definition needed to score fills after the market disappears."""
    con.executemany(
        """INSERT INTO contracts VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(ticker) DO UPDATE SET event=excluded.event,asset=excluded.asset,
             settle_ts=excluded.settle_ts,strike_type=excluded.strike_type,
             floor_strike=excluded.floor_strike,cap_strike=excluded.cap_strike""",
        rows,
    )


def record_surface(con, surface: dict) -> None:
    """Store one JSON-backed observation per asset/settlement/scan/version.

    Kalshi exposes the co-settling ladder and range as different events.  They
    are one risk event, so repeated calls in a scan merge their contract
    metadata instead of creating pseudo-independent surface observations.
    """
    version = surface.get("model_version", config.MODEL_VERSION)
    asset, settle_ts, ts = surface["asset"], surface["settle_ts"], surface["ts"]
    prior = con.execute(
        """SELECT model_center,model_iqr,market_center,market_iqr,width_ratio,
                  repair_max,profile_status,profile_age_s,catalyst_factor,
                  catalyst_blocked,contracts_json,regime_json
           FROM surfaces
           WHERE ts=? AND asset=? AND settle_ts=? AND model_version=?
           ORDER BY rowid DESC LIMIT 1""",
        (ts, asset, settle_ts, version),
    ).fetchone()
    names = ("model_center", "model_iqr", "market_center", "market_iqr",
             "width_ratio", "repair_max", "profile_status", "profile_age_s",
             "catalyst_factor", "catalyst_blocked")
    prior_values = dict(zip(names, prior[:10])) if prior else {}
    prior_contracts, prior_scenarios = [], []
    prior_regime = {}
    if prior:
        try:
            payload = json.loads(prior[10] or "{}")
            prior_contracts = payload.get("contracts", [])
            prior_scenarios = payload.get("scenarios", [])
            prior_regime = json.loads(prior[11] or "{}")
        except (AttributeError, TypeError, json.JSONDecodeError):
            pass
    contracts = {
        row.get("ticker"): row for row in prior_contracts + surface.get("contracts", [])
        if isinstance(row, dict) and row.get("ticker")
    }
    scenarios = [{"model_p": row[0], "market_p": row[1]}
                 for row in surface.get("scenarios", [])] or prior_scenarios
    con.execute(
        "DELETE FROM surfaces WHERE ts=? AND asset=? AND settle_ts=? AND model_version=?",
        (ts, asset, settle_ts, version),
    )
    def value(name, default=None):
        incoming = surface.get(name)
        return incoming if incoming is not None else prior_values.get(name, default)

    con.execute(
        """INSERT OR REPLACE INTO surfaces VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            ts, f"{asset}:{int(settle_ts)}", asset, settle_ts, version,
            value("model_center"), value("model_iqr"),
            value("market_center"), value("market_iqr"),
            value("width_ratio"), value("repair_max"),
            value("profile_status"), value("profile_age_s"),
            value("catalyst_factor", 1.0),
            int(bool(surface.get("catalyst_blocked")
                     or prior_values.get("catalyst_blocked"))),
            json.dumps({"contracts": list(contracts.values()),
                        "scenarios": scenarios},
                       separators=(",", ":")),
            json.dumps(surface.get("regime") or prior_regime, separators=(",", ":")),
        ),
    )


def record_outcome(con, asset: str, settle_ts: float, expiration_value,
                   finalized_ts: float | None = None) -> None:
    """Upsert exact settlement value; a later non-null API value wins."""
    value = None if expiration_value in (None, "") else float(expiration_value)
    con.execute(
        """INSERT INTO outcomes VALUES(?,?,?,?)
           ON CONFLICT(asset,settle_ts) DO UPDATE SET
             expiration_value=COALESCE(excluded.expiration_value,outcomes.expiration_value),
             finalized_ts=COALESCE(excluded.finalized_ts,outcomes.finalized_ts)""",
        (asset, settle_ts, value, finalized_ts),
    )


def record_order_evidence(con, *, client_order_id: str, ticker: str, asset: str,
                          settle_ts: float, event_profit_model: float,
                          event_profit_market: float, expected_pnl: float,
                          worst_loss: float, order_id: str | None = None,
                          model_version: str = config.MODEL_VERSION) -> None:
    con.execute(
        """INSERT OR REPLACE INTO order_evidence VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (client_order_id, order_id, ticker, model_version, asset, settle_ts,
         event_profit_model, event_profit_market, expected_pnl, worst_loss),
    )


def link_order_evidence(con, client_order_id: str, order_id: str) -> None:
    con.execute("UPDATE order_evidence SET order_id=? WHERE client_order_id=?",
                (order_id, client_order_id))


def backfill_fill_fairs(con) -> int:
    """Fill only exact one-order matches; ambiguous/manual fills remain NULL."""
    before = con.total_changes
    con.execute(
        """UPDATE fills SET fair_at_entry=(
               SELECT MAX(o.fair) FROM orders o WHERE o.order_id=fills.order_id)
           WHERE fair_at_entry IS NULL AND order_id IS NOT NULL AND order_id!='?'
             AND (SELECT COUNT(*) FROM orders o WHERE o.order_id=fills.order_id)=1"""
    )
    con.commit()
    return con.total_changes - before


def strategy_pnl(con, since: float, now: float | None = None) -> float:
    """Realized autonomous desk P&L, with manual activity excluded.

    Versioned orders use their exact entry fills and terminal result. Legacy
    tickers retain the settlement aggregate (which already includes fees) so
    old accounting remains stable; the two sets are mutually exclusive.
    Entry fills are matched on f.side=o.side, NOT action: Kalshi journals a
    buy-no entry as action='sell'. Manual closes carry foreign order_ids and
    never join.
    """
    until = now if now is not None else time.time()
    scope_ts = max(until - 7 * 86400, config.STRATEGY_EPOCH)
    exact = con.execute(
        """SELECT COALESCE(SUM(f.count *
                    ((CASE WHEN f.side='yes' THEN s.realized ELSE 1-s.realized END)
                     - f.price) - COALESCE(f.fee,0)),0)
           FROM fills f
           JOIN orders o ON o.order_id=f.order_id
           JOIN order_evidence oe ON oe.client_order_id=o.client_order_id
           JOIN settles s ON s.ticker=f.ticker
           WHERE o.ts>=? AND s.ts>=? AND s.ts<=? AND s.realized IS NOT NULL
             AND f.side=o.side""",
        (scope_ts, since, until),
    ).fetchone()[0]
    legacy = con.execute(
        """SELECT COALESCE(SUM(s.pnl),0) FROM settles s
           WHERE s.ts>=? AND s.ts<=? AND s.ticker IN
             (SELECT DISTINCT ticker FROM orders WHERE ts>=?)
             AND NOT EXISTS (SELECT 1 FROM order_evidence oe WHERE oe.ticker=s.ticker)""",
        (since, until, scope_ts),
    ).fetchone()[0]
    return float(exact or 0.0) + float(legacy or 0.0)


def desk_pnl(con, since: float, now: float | None = None) -> float:
    """Version-independent realized P&L across every desk-traded ticker.
    Rails floor: a STRATEGY_EPOCH bump must never forgive the same day's
    realized losses (2026-07-14: rail read +0.50 on a -9.88 day)."""
    until = now if now is not None else time.time()
    return float(con.execute(
        """SELECT COALESCE(SUM(pnl),0) FROM settles
           WHERE ts>=? AND ts<=?
             AND ticker IN (SELECT DISTINCT ticker FROM orders)""",
        (since, until),
    ).fetchone()[0])


def _yes_result(contract: dict, expiration_value: float) -> int:
    kind = contract.get("strike_type")
    floor = contract.get("floor_strike")
    cap = contract.get("cap_strike")
    if kind == "greater" and floor is not None:
        return int(expiration_value > float(floor))
    if kind == "between" and floor is not None and cap is not None:
        return int(float(floor) < expiration_value <= float(cap))
    if kind == "less" and (cap is not None or floor is not None):
        return int(expiration_value <= float(cap if cap is not None else floor))
    raise ValueError("incomplete contract payoff metadata")


def _threshold_crps(contracts: list[dict], expiration_value: float,
                    probability_key: str) -> float | None:
    pts = sorted((float(c["floor_strike"]), float(c[probability_key]))
                 for c in contracts if c.get("strike_type") == "greater"
                 and c.get("floor_strike") is not None
                 and c.get(probability_key) is not None)
    if len(pts) < 2 or pts[-1][0] <= pts[0][0]:
        return None
    total = 0.0
    for (k0, p0), (k1, p1) in zip(pts, pts[1:]):
        mid = (k0 + k1) / 2
        p = (p0 + p1) / 2
        total += (k1 - k0) * (p - int(expiration_value > mid)) ** 2
    return total / (pts[-1][0] - pts[0][0])


def strategy_calibration(con, model_version: str = config.MODEL_VERSION) -> dict:
    """Exact-version, event-balanced calibration from prospective surfaces."""
    rows = con.execute(
        """SELECT s.asset,s.settle_ts,s.contracts_json,o.expiration_value
           FROM surfaces s JOIN outcomes o
             ON o.asset=s.asset AND o.settle_ts=s.settle_ts
           WHERE s.model_version=? AND o.expiration_value IS NOT NULL
             AND s.ts=(SELECT MAX(s2.ts) FROM surfaces s2
                       WHERE s2.model_version=s.model_version
                         AND s2.asset=s.asset AND s2.settle_ts=s.settle_ts)""",
        (model_version,),
    ).fetchall()
    grouped: dict[tuple[str, float], list[dict]] = {}
    values: dict[tuple[str, float], float] = {}
    for asset, settle_ts, payload, expiration_value in rows:
        key = (asset, settle_ts)
        try:
            decoded = json.loads(payload or "[]")
            contracts = decoded.get("contracts", []) if isinstance(decoded, dict) else decoded
            grouped.setdefault(key, []).extend(contracts)
            values[key] = float(expiration_value)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    event_scores = []
    model_crps, market_crps = [], []
    tail = []
    contract_count = 0
    for key, contracts in grouped.items():
        scored = []
        unique = {c.get("ticker"): c for c in contracts if c.get("ticker")}.values()
        for contract in unique:
            try:
                fair = float(contract["fair"])
                mid = float(contract["market_mid"])
                realized = _yes_result(contract, values[key])
            except (KeyError, TypeError, ValueError):
                continue
            if not (0 <= fair <= 1 and 0 <= mid <= 1):
                continue
            scored.append(((fair - realized) ** 2, (mid - realized) ** 2))
            contract_count += 1
            if fair <= .25 or fair >= .75:
                tail.append((min(fair, 1 - fair),
                             realized if fair <= .5 else 1 - realized, key))
        if scored:
            event_scores.append((sum(x for x, _ in scored) / len(scored),
                                 sum(x for _, x in scored) / len(scored)))
        m = _threshold_crps(list(unique), values[key], "fair")
        q = _threshold_crps(list(unique), values[key], "market_mid")
        if m is not None and q is not None:
            model_crps.append(m)
            market_crps.append(q)
    timestamps = {settle for _, settle in grouped}
    days = {dt.datetime.fromtimestamp(settle, dt.UTC).date() for settle in timestamps}
    return {
        "model_version": model_version,
        "contracts": contract_count,
        "event_risk_keys": len(event_scores),
        "independent_outcomes": len(event_scores),  # compatibility alias; do not label independent
        "settlement_timestamps": len(timestamps),
        "utc_days": len(days),
        "model_brier": (sum(x for x, _ in event_scores) / len(event_scores)
                         if event_scores else None),
        "market_brier": (sum(x for _, x in event_scores) / len(event_scores)
                          if event_scores else None),
        "model_crps": sum(model_crps) / len(model_crps) if model_crps else None,
        "market_crps": sum(market_crps) / len(market_crps) if market_crps else None,
        "tail_contracts": len(tail),
        "tail_outcomes": len({key for *_, key in tail}),
        "tail_predicted": sum(x for x, _, _ in tail) / len(tail) if tail else None,
        "tail_realized": sum(x for _, x, _ in tail) / len(tail) if tail else None,
    }
