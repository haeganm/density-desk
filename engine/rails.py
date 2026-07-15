"""Hard risk rails. Enforced in code before every order; the operator LLM has no
override path. A raised RailsHalt aborts the executor cycle."""
from __future__ import annotations
import datetime as dt
import time

import config
import db


class RailsHalt(RuntimeError):
    pass


HALT_FILE = config.ROOT / "state" / "HALT_UNTIL"   # written on daily-loss breach


def _utc_midnight() -> float:
    now = dt.datetime.now(dt.UTC)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def per_trade_cap() -> float:
    return config.SHAKEOUT_CAP if config.SHAKEOUT else config.PER_TRADE_CAP


def active_halt_until(now: float | None = None, path=None) -> float | None:
    """Return an active daily-loss halt, cleaning up an expired sentinel."""
    path = path or HALT_FILE
    try:
        raw = path.read_text().strip()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RailsHalt(f"cannot read daily-loss HALT file: {exc}") from exc
    try:
        until = float(raw or 0)
    except ValueError as exc:
        raise RailsHalt("invalid daily-loss HALT file") from exc
    if (time.time() if now is None else now) < until:
        return until
    path.unlink(missing_ok=True)
    return None


def check_global(con, model_age_s: float, kalshi_age_s: float) -> None:
    """Run once per executor cycle before any order."""
    if config.KILL_FILE.exists():
        raise RailsHalt("KILL file present")
    until = active_halt_until()
    if until is not None:
        raise RailsHalt(f"daily-loss halt until {dt.datetime.fromtimestamp(until, dt.UTC)}")
    if model_age_s > config.STALE_DATA_HALT_S or kalshi_age_s > config.STALE_DATA_HALT_S:
        raise RailsHalt(f"stale data (forecast {model_age_s:.0f}s, kalshi {kalshi_age_s:.0f}s)")
    midnight = _utc_midnight()
    # min(): the version-scoped view AND the whole desk book must both be
    # inside the limit — an epoch bump can never re-open the day's loss budget
    realized = min(db.strategy_pnl(con, midnight), db.desk_pnl(con, midnight))
    if realized <= -config.DAILY_LOSS_LIMIT:
        until = _utc_midnight() + 86400
        HALT_FILE.parent.mkdir(exist_ok=True)
        HALT_FILE.write_text(str(until))
        raise RailsHalt(f"daily loss limit breached ({realized:+.2f})")


def check_order(cost: float, open_exposure: float,
                max_exposure: float | None = None,
                risk_cost: float | None = None) -> None:
    """Per-order check. ``cost`` is principal for global exposure; ``risk_cost``
    optionally includes fees for the per-trade cap."""
    cap = per_trade_cap()
    limit = min(config.MAX_OPEN_EXPOSURE,
                max_exposure if max_exposure is not None else config.MAX_OPEN_EXPOSURE)
    capped_cost = cost if risk_cost is None else risk_cost
    if capped_cost > cap + 1e-9:
        raise RailsHalt(f"order cost {capped_cost:.2f} > per-trade cap {cap:.2f}")
    if open_exposure + cost > limit:
        raise RailsHalt(f"exposure {open_exposure + cost:.2f} > max {limit:.2f}")


def check_daily_reserve(con, settle_ts: float, reserved_loss: float) -> None:
    """Keep realized P&L plus unresolved worst-case loss inside the UTC-day rail."""
    day = dt.datetime.fromtimestamp(settle_ts, dt.UTC)
    start = day.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    until = min(time.time(), start + 86400)
    realized = min(db.strategy_pnl(con, start, until), db.desk_pnl(con, start, until))
    if realized - max(0.0, reserved_loss) <= -config.DAILY_LOSS_LIMIT + 1e-9:
        raise RailsHalt(
            f"daily loss reserve {reserved_loss:.2f} with realized {realized:+.2f} "
            f">= limit {config.DAILY_LOSS_LIMIT:.2f}"
        )


DRAWDOWN_DISARM_FRAC = 0.65      # auto-disarm when equity < 65% of session start


def drawdown_check(con, equity: float) -> None:
    """Auto-disarm on session drawdown. Session-start equity lives inside the
    ARMED file (written at arm time; executor backfills if empty). Re-arming
    after an auto-disarm is always a human act on the dashboard."""
    try:
        raw = config.ARMED_FILE.read_text().strip()
    except FileNotFoundError:            # dashboard disarm raced us — fine
        return
    try:
        start = float(raw)
    except ValueError:
        start = 0.0
    if start <= 0:
        config.ARMED_FILE.write_text(f"{equity:.2f}")
        return
    if equity < DRAWDOWN_DISARM_FRAC * start:
        config.ARMED_FILE.unlink(missing_ok=True)
        con.execute("INSERT INTO events VALUES(?,?,?)",
                    (time.time(), "auto_disarm",
                     f"equity {equity:.2f} < {DRAWDOWN_DISARM_FRAC:.0%} of session "
                     f"start {start:.2f} — trading disarmed, re-arm manually"))
        con.commit()
        raise RailsHalt("auto-disarm: session drawdown limit")
