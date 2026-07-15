"""Read-only, exact-version strategy evidence report. No profit forecast."""
import datetime as dt
import math
import random
import sqlite3
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402 - direct script execution needs the repository root
import db  # noqa: E402


def _event_pnl(con, model_version: str) -> list[tuple[str, float, float]]:
    return con.execute(
        """SELECT oe.city,oe.settle_ts,
                  SUM(f.count *
                    ((CASE WHEN f.side='yes' THEN s.realized ELSE 1-s.realized END)
                     - f.price) - COALESCE(f.fee,0))
           FROM fills f
           JOIN orders o ON o.order_id=f.order_id
           JOIN order_evidence oe ON oe.client_order_id=o.client_order_id
           JOIN settles s ON s.ticker=f.ticker
           WHERE oe.model_version=? AND f.side=o.side AND s.realized IS NOT NULL
           GROUP BY oe.city,oe.settle_ts ORDER BY oe.settle_ts""",
        (model_version,),
    ).fetchall()


def _block_bootstrap_mean_ci(values: list[float], samples: int = 2000):
    """Circular moving-block bootstrap over ordered settlement-time clusters."""
    if len(values) < 2:
        return None
    rng = random.Random(0)
    n = len(values)
    block = min(n, max(2, round(math.sqrt(n))))
    means = []
    for _ in range(samples):
        draw = []
        while len(draw) < n:
            start = rng.randrange(n)
            draw.extend(values[(start + offset) % n] for offset in range(block))
        means.append(statistics.fmean(draw[:n]))
    means.sort()
    return means[int(.025 * samples)], means[int(.975 * samples)]


def collect_evidence(con, model_version: str = config.MODEL_VERSION) -> dict:
    calibration = db.strategy_calibration(con, model_version)
    fills = con.execute(
        """SELECT COUNT(*),COALESCE(SUM(f.count),0),oe.city,oe.settle_ts
           FROM order_evidence oe
           JOIN orders o ON o.client_order_id=oe.client_order_id
           LEFT JOIN fills f ON f.order_id=oe.order_id AND f.side=o.side
           WHERE oe.model_version=?
           GROUP BY oe.city,oe.settle_ts""",
        (model_version,),
    ).fetchall()
    filled = [(city, settle, records, contracts)
              for records, contracts, city, settle in fills if contracts]
    timestamps = {settle for _, settle, *_ in filled}
    days = {dt.datetime.fromtimestamp(ts, dt.UTC).date() for ts in timestamps}
    event_pnl = _event_pnl(con, model_version)
    values = [float(row[2]) for row in event_pnl]
    by_settlement = {}
    for _asset, settle, pnl in event_pnl:
        by_settlement[float(settle)] = by_settlement.get(float(settle), 0.0) + float(pnl)
    event_metrics = {}
    for row in con.execute(
        """SELECT o.ts,oe.city,oe.settle_ts,oe.event_profit_model,
                  oe.event_profit_market,oe.expected_pnl,oe.worst_loss
           FROM order_evidence oe JOIN orders o
             ON o.client_order_id=oe.client_order_id
           WHERE oe.model_version=? AND EXISTS (
             SELECT 1 FROM fills f WHERE f.order_id=oe.order_id
               AND f.side=o.side AND f.count>0)
           ORDER BY o.ts""",
        (model_version,),
    ):
        event_metrics[(row[1], row[2])] = tuple(row[1:])
    return {
        "model_version": model_version,
        "calibration": calibration,
        "filled_event_keys": len(filled),
        "filled_settlement_timestamps": len(timestamps),
        "filled_utc_days": len(days),
        "fill_records": sum(int(row[2]) for row in filled),
        "filled_contracts": sum(float(row[3]) for row in filled),
        "realized_events": len(event_pnl),
        "realized_pnl": sum(values),
        "settlement_clusters": len(by_settlement),
        "mean_event_pnl_ci": _block_bootstrap_mean_ci(
            [by_settlement[key] for key in sorted(by_settlement)]
        ),
        "event_metrics": list(event_metrics.values()),
    }


def _metric(value) -> str:
    return "n/a" if value is None else f"{value:.4f}"


def render(evidence: dict) -> str:
    c = evidence["calibration"]
    ci = evidence["mean_event_pnl_ci"]
    lines = [
        f"DENSITY DESK {evidence['model_version'].upper()} EVIDENCE REPORT",
        f"as of {dt.datetime.now(dt.UTC).isoformat(timespec='seconds')}",
        "",
        "A. EVENT-BALANCED MODEL VS MARKET",
        f"   scored contract observations: {c['contracts']}",
        f"   scored city-settlement keys: {c['event_risk_keys']}",
        f"   distinct settlement timestamps: {c['settlement_timestamps']}",
        f"   distinct UTC days: {c['utc_days']}",
        f"   model/market Brier: {_metric(c['model_brier'])} / {_metric(c['market_brier'])}",
        f"   model/market threshold CRPS: {_metric(c['model_crps'])} / {_metric(c['market_crps'])}",
        f"   symmetric-tail contracts/events: {c['tail_contracts']} / {c['tail_outcomes']}",
        f"   tail predicted/realized rate: {_metric(c['tail_predicted'])} / {_metric(c['tail_realized'])}",
        "",
        "B. ACTUAL VERSIONED EXECUTION",
        f"   filled event-risk keys: {evidence['filled_event_keys']}",
        f"   distinct filled settlement timestamps: {evidence['filled_settlement_timestamps']}",
        f"   distinct filled UTC days: {evidence['filled_utc_days']}",
        f"   fill records/contracts: {evidence['fill_records']} / {evidence['filled_contracts']:g}",
        f"   finalized events: {evidence['realized_events']}",
        f"   autonomous realized P&L: {evidence['realized_pnl']:+.4f}",
        "   manual trades and raw account deposits are excluded",
    ]
    metrics = evidence["event_metrics"]
    if metrics:
        lines += ["", "C. LATEST POST-FILL EVENT METRICS"]
        lines += [
            f"   {city} {dt.datetime.fromtimestamp(settle, dt.UTC):%Y-%m-%d %H:%MZ}"
            f" | profit p model/market {model_p:.3f}/{market_p:.3f}"
            f" | expected P&L {expected:+.4f} | worst loss {worst:.4f}"
            for city, settle, model_p, market_p, expected, worst in metrics
        ]
    if ci:
        lines.append(
            "   moving-block bootstrap mean settlement-cluster P&L interval: "
            f"[{ci[0]:+.4f}, {ci[1]:+.4f}]"
        )
    ready = (evidence["filled_event_keys"] >= 30
             and evidence["filled_settlement_timestamps"] >= 30
             and evidence["filled_utc_days"] >= 14)
    lines += [
        "",
        ("ASSESSMENT: EVIDENCE COLLECTION THRESHOLDS MET; model promotion still "
         "requires explicit review." if ready else
         "ASSESSMENT: INSUFFICIENT LIVE EVIDENCE (need 30 filled event keys, "
         "30 settlement timestamps, and 14 UTC days)."),
        "No daily or weekly profit probability is estimated.",
    ]
    return "\n".join(lines) + "\n"


def main(path=config.DB_PATH) -> str:
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        report = render(collect_evidence(con))
    finally:
        con.close()
    print(report, end="")
    return report


if __name__ == "__main__":
    main()
