"""Dashboard API + static page. Read-only: never touches the trading loop.
Run: .venv\\Scripts\\python.exe -m uvicorn dashboard.app:app --port 8777"""
import json
import math
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

import config
import db
from engine import rails
from kalshi.client import KalshiClient

app = FastAPI()
HERE = Path(__file__).parent
_last_status: dict = {}
HALT_FILE = config.ROOT / "state" / "HALT_UNTIL"


def q(con, sql, *args):
    return con.execute(sql, args).fetchall()


def _position_fair(yes_fair: float | None, count: float) -> float | None:
    if yes_fair is None:
        return None
    return 1.0 - yes_fair if count < 0 else yes_fair


def _read_status(keep_last: bool = False) -> dict:
    global _last_status
    try:
        status = json.loads((config.ROOT / "state" / "status.json").read_text())
        _last_status = status
        return status
    except (OSError, ValueError):
        return _last_status if keep_last else {}


def _fresh(ts, now: float) -> bool:
    try:
        age = now - float(ts)
    except (TypeError, ValueError):
        return False
    return 0 <= age < 300


def _arm_rejection(status: dict, now: float) -> str | None:
    if config.KILL_FILE.exists():
        return "KILL file is present"
    try:
        halt_until = rails.active_halt_until(now, HALT_FILE)
    except rails.RailsHalt as exc:
        return str(exc)
    if halt_until is not None:
        return "loss HALT is active"
    if not config.KALSHI_PEM.exists() or not config.KALSHI_KEY_ID_FILE.exists():
        return "Kalshi API keys are missing"
    required = ("ts", "error", "scan_ok", "portfolio_sync_ok",
                "portfolio_sync_ts", "clock_skew_s", "balance", "portfolio_value")
    missing = [key for key in required if key not in status]
    if missing:
        return "desk health is incomplete: " + ", ".join(missing)
    if not _fresh(status["ts"], now):
        return "desk status is stale or future-dated"
    if status["error"]:
        return "desk reports an error; check the system log"
    if status["scan_ok"] is not True:
        return "latest market scan failed"
    if status["portfolio_sync_ok"] is not True:
        return "latest authenticated portfolio sync failed"
    if not _fresh(status["portfolio_sync_ts"], now):
        return "portfolio sync is stale or future-dated"
    for key in ("balance", "portfolio_value"):
        try:
            value = float(status[key])
        except (TypeError, ValueError):
            return f"live {key.replace('_', ' ')} is unavailable"
        if not math.isfinite(value) or value < 0:
            return f"live {key.replace('_', ' ')} is invalid"
    try:
        skew = float(status["clock_skew_s"])
    except (TypeError, ValueError):
        return "Kalshi clock skew is unavailable"
    if not -30 <= skew <= 30:
        return f"system clock differs from Kalshi by {skew:+.0f}s"
    return None


@app.get("/")
def index():
    return FileResponse(HERE / "index.html")


@app.post("/api/arm")
def arm(payload: dict):
    """Dashboard toggle (localhost-only server). ARMED file gates the executor."""
    if not isinstance(payload.get("armed"), bool):
        raise HTTPException(status_code=422, detail="armed must be true or false")
    if payload["armed"] is True:
        reason = _arm_rejection(_read_status(), time.time())
        if reason is None:
            try:
                client = KalshiClient()
                client.balance()
            except Exception as exc:
                reason = f"live Kalshi balance preflight failed: {exc}"
            if reason is None and (client.clock_skew_s is None
                                   or not -30 <= client.clock_skew_s <= 30):
                reason = ("live Kalshi clock skew is unavailable" if client.clock_skew_s is None
                          else f"live system clock differs from Kalshi by "
                               f"{client.clock_skew_s:+.0f}s")
        if reason is not None:
            config.ARMED_FILE.unlink(missing_ok=True)
            raise HTTPException(status_code=409, detail=reason)
        con = db.connect()
        # write 0.00: the executor backfills session-start equity from LIVE
        # balance+positions on its first armed cycle. Seeding from status.json
        # here risked an arbitrarily stale drawdown baseline (audit H3).
        config.ARMED_FILE.parent.mkdir(exist_ok=True)
        config.ARMED_FILE.write_text("0.00")
        db.log_event(con, "armed", "trading ARMED via dashboard")
    else:
        con = db.connect()
        config.ARMED_FILE.unlink(missing_ok=True)
        db.log_event(con, "disarmed", "trading DISARMED via dashboard")
    con.close()
    return {"armed": config.ARMED_FILE.exists()}


@app.get("/api/state")
def state():
    con = db.connect()   # bootstraps schema on a fresh install
    now = time.time()
    try:
        halted = rails.active_halt_until(now, HALT_FILE) is not None
    except rails.RailsHalt:
        halted = True
    status = _read_status(keep_last=True)  # torn write: retain last good display state

    charts = {}
    for asset, s in config.SERIES.items():
        row = q(con, """SELECT event, MAX(ts) FROM snapshots
                        WHERE asset=? AND event LIKE ? AND settle_ts > ?
                        GROUP BY event ORDER BY settle_ts LIMIT 1""",
                asset, s["ladder"] + "%", now)
        if not row or not row[0][0]:
            continue
        ev, ts = row[0]
        pts = q(con, """SELECT strike, fair, bid, ask, settle_ts FROM snapshots
                        WHERE event=? AND ts=? ORDER BY strike""", ev, ts)
        charts[asset] = {
            "event": ev, "as_of": ts, "settle_ts": pts[0][4] if pts else None,
            "index": (status.get("index") or {}).get(asset),
            "points": [{"k": p[0], "fair": p[1], "bid": p[2], "ask": p[3]} for p in pts],
        }

    calibration = db.strategy_calibration(con)
    # current fair value for each open position (latest snapshot)
    pos_fairs = {}
    for p in (status.get("positions") or []):
        row = q(con, "SELECT fair FROM snapshots WHERE ticker=? ORDER BY ts DESC LIMIT 1",
                p["ticker"])
        pos_fairs[p["ticker"]] = _position_fair(row[0][0] if row else None,
                                                p["count"])
    from engine.scanner import threshold_factor
    cat_file = config.ROOT / "state" / "catalysts.json"
    cat_windows = []
    if cat_file.exists():
        try:
            cat_windows = [w for w in json.loads(cat_file.read_text()).get("windows", [])
                           if float(w.get("end_ts", 0)) > now]
        except (ValueError, TypeError):
            pass
    pnl_today = db.strategy_pnl(con, now - (now % 86400), now)
    latest_model_start = q(
        con, "SELECT COALESCE(MAX(ts),0) FROM events WHERE kind='start' "
             "AND detail LIKE '%model=r4%'"
    )[0][0]
    edges = q(con, """SELECT ts, ticker, side, fair, price, edge, acted FROM edges e
                      WHERE ts>=? AND ts = (SELECT MAX(ts) FROM edges WHERE ticker=e.ticker
                                  AND side=e.side)
                      ORDER BY ts DESC, edge DESC LIMIT 25""", latest_model_start)
    events = q(con, "SELECT ts, kind, detail FROM events ORDER BY ts DESC LIMIT 25")
    fills = q(con, """SELECT f.ts,f.ticker,f.side,f.action,f.count,f.price,f.fee,
                             CASE WHEN EXISTS (
                               SELECT 1 FROM orders o WHERE o.order_id=f.order_id)
                                  THEN 'desk' ELSE 'manual' END
                      FROM fills f ORDER BY f.ts DESC LIMIT 25""")
    eq = q(con, "SELECT ts, cash+open_cost FROM equity WHERE ts > ? ORDER BY ts",
           now - 48 * 3600)
    eq = eq[::max(1, len(eq) // 200)]                    # downsample for the sparkline
    last_arm = q(con, "SELECT MAX(ts) FROM events WHERE kind IN ('armed','disarmed')"
                 )[0][0] or 0
    last_auto = q(con, "SELECT MAX(ts) FROM events WHERE kind='auto_disarm'")[0][0] or 0
    con.close()
    return {
        "now": now,
        "status": status,
        "charts": charts,
        "calibration": {
            "model_version": calibration["model_version"],
            "n": calibration["contracts"],
            "brier": calibration["model_brier"],
            "event_keys": calibration["event_risk_keys"],
            "settlement_timestamps": calibration["settlement_timestamps"],
            "utc_days": calibration["utc_days"],
            "market_brier": calibration["market_brier"],
            "tail_ratio": (calibration["tail_predicted"]
                           / max(calibration["tail_realized"], 1e-12)
                           if calibration["tail_realized"] is not None else None),
        },
        "pnl_today": pnl_today,
        "pos_fairs": pos_fairs,
        "catalyst": {"factor_now": threshold_factor(now),
                     "windows": sorted(cat_windows, key=lambda w: w["start_ts"])[:5]},
        "edges": [dict(zip(["ts", "ticker", "side", "fair", "price", "edge", "acted"], e))
                  for e in edges],
        "events": [dict(zip(["ts", "kind", "detail"], e)) for e in events],
        "fills": [dict(zip(["ts", "ticker", "side", "action", "count", "price", "fee",
                            "source"], f))
                  for f in fills],
        "equity": [{"ts": t, "v": v} for t, v in eq],
        "rails": {
            "kill": config.KILL_FILE.exists(),
            "armed": config.ARMED_FILE.exists(),
            "auto_disarmed": (not config.ARMED_FILE.exists()) and last_auto > last_arm,
            "halted": halted,
            "shakeout": config.SHAKEOUT,
            "bankroll": status.get("balance"),
            "daily_loss_limit": config.DAILY_LOSS_LIMIT,
            "per_trade_cap": (config.SHAKEOUT_CAP if config.SHAKEOUT
                              else config.PER_TRADE_CAP),
            "can_trade": (config.KALSHI_PEM.exists()
                          and config.KALSHI_KEY_ID_FILE.exists()),
        },
    }
