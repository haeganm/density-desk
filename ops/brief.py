"""Write state/brief.md — the operator's situation report. Read-only on the desk."""
import datetime as dt
import sqlite3
import sys
import time

sys.path.insert(0, r"C:\density desk")
import config
import db
from scripts import attribution, profit_forecast


def write_brief() -> str:
    con = sqlite3.connect(config.DB_PATH)
    now = time.time()
    day0 = now - (now % 86400)

    calibration = db.strategy_calibration(con)
    evidence = profit_forecast.collect_evidence(con)
    pnl = db.strategy_pnl(con, day0, now)
    edges = con.execute(
        """SELECT e.ticker,e.side,e.fair,e.price,e.edge,e.acted,e.ts,
                  (SELECT settle_ts FROM snapshots WHERE ticker=e.ticker
                   ORDER BY ts DESC LIMIT 1)
           FROM edges e WHERE e.ts>? AND e.ts>=(SELECT COALESCE(MAX(ts),0) FROM events
               WHERE kind='start' AND detail LIKE '%model=r4%')
             AND e.ts=(SELECT MAX(e2.ts) FROM edges e2
               WHERE e2.ticker=e.ticker AND e2.side=e.side AND e2.ts>?)
           ORDER BY e.edge DESC LIMIT 10""", (now - 3600, now - 3600)).fetchall()
    latest_start = con.execute(
        "SELECT COALESCE(MAX(ts),0) FROM events WHERE kind='start'").fetchone()[0]
    anomaly_since = max(now - 6 * 3600, latest_start)
    anomalies = con.execute(
        """SELECT MAX(ts),kind,detail,COUNT(*) FROM events WHERE ts>? AND
                  kind IN ('halt','error','density_skip')
           GROUP BY kind,detail ORDER BY MAX(ts) DESC LIMIT 10""",
        (anomaly_since,)).fetchall()
    fills = con.execute(
        """SELECT f.ts,f.ticker,f.side,f.count,f.price,f.fee,
                  CASE WHEN EXISTS (SELECT 1 FROM orders o WHERE o.order_id=f.order_id)
                       THEN 'desk' ELSE 'manual' END
           FROM fills f WHERE f.ts>? ORDER BY f.ts DESC LIMIT 10""", (day0,)
    ).fetchall()
    try:
        attribution_text = attribution.render(con, days=7)
    except Exception as exc:                     # advisory section, never fatal
        attribution_text = f"attribution unavailable: {exc}"
    con.close()

    halted = (config.ROOT / "state" / "HALT_UNTIL").exists()
    mode = ("KILL" if config.KILL_FILE.exists() else "HALTED" if halted else
            "DISARMED" if not config.ARMED_FILE.exists() else
            "SHAKEOUT" if config.SHAKEOUT else "LIVE")
    model_brier = calibration["model_brier"]
    market_brier = calibration["market_brier"]
    L = [f"# Density Desk brief — {dt.datetime.now(dt.UTC):%Y-%m-%d %H:%MZ}", ""]
    L += [f"- Mode: {mode} | strategy realized P&L today: {pnl:+.2f}"
          f" | calibration: {calibration['contracts']} contracts across "
          f"{calibration['event_risk_keys']} {config.MODEL_VERSION} event keys"
          f" | Brier model/market: "
          f"{f'{model_brier:.3f}' if model_brier is not None else 'n/a'}/"
          f"{f'{market_brier:.3f}' if market_brier is not None else 'n/a'}"]
    # catalyst-calendar freshness: the desk fails closed past 48h, and the
    # reviewer repeatedly flagged that it could not see this from the brief
    import json
    cat_path = config.ROOT / "state" / "catalysts.json"
    try:
        cat = json.loads(cat_path.read_text())
        age_h = (now - float(cat.get("generated_ts", cat_path.stat().st_mtime))) / 3600
        stale = " ** STALE >48h: all orders blocked **" if age_h > 48 else ""
        L += [f"- Catalyst calendar: {len(cat.get('windows', []))} windows, "
              f"{age_h:.1f}h old{stale}"]
    except Exception as exc:
        L += [f"- Catalyst calendar: UNREADABLE ({exc}) - all orders blocked"]
    ready = (evidence["filled_event_keys"] >= 30
             and evidence["filled_settlement_timestamps"] >= 30
             and evidence["filled_utc_days"] >= 14)
    L += [f"- Evidence: {'thresholds met' if ready else 'insufficient live evidence'}"
          f" | {config.MODEL_VERSION} filled event keys/timestamps/days: "
          f"{evidence['filled_event_keys']}/{evidence['filled_settlement_timestamps']}/"
          f"{evidence['filled_utc_days']}"
          " | no daily/weekly profit probability is estimated"]
    L += ["", "## Fill attribution (promised vs realized edge — the adverse-selection check)"]
    L += ["```", attribution_text.rstrip(), "```"]
    L += ["", "## Latest edge observations (last hour; one per ticker/side)"]
    L += [f"- observed {dt.datetime.fromtimestamp(ts, dt.UTC):%H:%MZ} | "
          f"{'settles' if settle and settle > now else 'SETTLED'} "
          f"{dt.datetime.fromtimestamp(settle, dt.UTC):%H:%MZ} | "
          f"{t} {s} fair={f:.2f} px={p:.2f} edge={e:+.3f} {'ACTED' if a else ''}"
          for t, s, f, p, e, a, ts, settle in edges if settle] or ["- none"]
    L += ["", "## Fills today (source tagged)"]
    L += [f"- [{source}] {dt.datetime.fromtimestamp(ts, dt.UTC):%H:%MZ} "
          f"{t} {s} x{n} @ {p:.2f} fee {fe:.2f}"
          for ts, t, s, n, p, fe, source in fills] or ["- none"]
    L += ["", "## Anomalies (current desk boot only; repeated rows grouped)"]
    L += [f"- {dt.datetime.fromtimestamp(ts, dt.UTC):%H:%MZ} [{k}] x{n} "
          f"{(d or '')[:120]}" for ts, k, d, n in anomalies] or ["- none"]
    L += ["", "## Operator checklist",
          "1. Recommend KILL only for an unresolved current-boot fault; never modify files.",
          "2. Catalysts in next 12h not in state/catalysts.json? -> add window.",
          "3. Compare event-balanced model vs market scores; do not blend model versions.",
          "4. Fees on fills materially above model -> update MAKER_FEE_FRACTION/TAKER_FEE_MULT",
          "   (maker fills verified $0.00 live 2026-07-15; any non-zero maker fee is news).",
          "5. Attribution: promised >> realized on settled fills is adverse selection.",
          "   Report it; do not retune MODEL_TRUST or rails from a handful of fills."]
    text = "\n".join(L) + "\n"
    (config.ROOT / "state" / "brief.md").write_text(text, encoding="utf-8")
    return text


if __name__ == "__main__":
    print(write_brief())
