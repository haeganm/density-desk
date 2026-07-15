"""Fill-level attribution: where does promised edge actually go?

The institutional loop's measure step. Every fill is tagged maker/taker and
compared against the edge the model promised at entry, so adverse selection
shows up as a number instead of a hunch. Read-only.

    .venv\\Scripts\\python.exe scripts\\attribution.py [--days N]
"""
import datetime as dt
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import db  # noqa: E402


def rows(con, since: float) -> list[dict]:
    """Desk fills with entry fair, settlement result, and maker/taker tag."""
    out = []
    for (ts, ticker, side, count, price, fee, fair, oid, coid,
         realized) in con.execute(
            """SELECT f.ts,f.ticker,f.side,f.count,f.price,f.fee,f.fair_at_entry,
                      f.order_id,o.client_order_id,s.realized
               FROM fills f
               JOIN orders o ON o.order_id=f.order_id AND o.side=f.side
               LEFT JOIN settles s ON s.ticker=f.ticker
               WHERE f.ts>=? ORDER BY f.ts""", (since,)):
        if fair is None:
            continue
        maker = (coid or "").startswith("dd-mk-")
        promised = (fair - price) * count - (fee or 0.0)
        rec = {"ts": ts, "ticker": ticker, "side": side, "count": count,
               "price": price, "fee": fee or 0.0, "fair": fair, "maker": maker,
               "promised": promised, "realized_pnl": None, "won": None}
        if realized is not None:
            won = (realized == 1) == (side == "yes")
            rec["won"] = won
            rec["realized_pnl"] = ((1.0 - price) * count - (fee or 0.0) if won
                                   else -price * count - (fee or 0.0))
        out.append(rec)
    return out


def _agg(records: list[dict]) -> dict:
    settled = [r for r in records if r["realized_pnl"] is not None]
    contracts = sum(r["count"] for r in settled)
    won = sum(r["count"] for r in settled if r["won"])
    return {
        "orders": len(records),
        "settled": len(settled),
        "open": len(records) - len(settled),
        "contracts": contracts,
        "fees": sum(r["fee"] for r in records),
        "promised": sum(r["promised"] for r in settled),
        "realized": sum(r["realized_pnl"] for r in settled),
        "pred_win": (sum(r["fair"] * r["count"] for r in settled) / contracts
                     if contracts else None),
        "real_win": won / contracts if contracts else None,
    }


def _fmt(label: str, a: dict) -> list[str]:
    if not a["orders"]:
        return [f"   {label:<8} (none)"]
    line = (f"   {label:<8} {a['orders']:>3} orders / {a['contracts']:>4g} contracts"
            f" | fees ${a['fees']:.2f}")
    if not a["settled"]:
        return [line + f" | {a['open']} open, none settled yet"]
    capture = (a["realized"] / a["promised"]) if abs(a["promised"]) > 1e-9 else None
    return [
        line,
        f"            P(win) predicted {a['pred_win']:.3f} realized {a['real_win']:.3f}"
        f" | promised ${a['promised']:+.2f} realized ${a['realized']:+.2f}"
        + (f" | capture {capture:.0%}" if capture is not None else ""),
    ]


def render(con, days: int = 7) -> str:
    since = max(config.STRATEGY_EPOCH, dt.datetime.now(dt.UTC).timestamp() - days * 86400)
    records = rows(con, since)
    L = [f"DENSITY DESK ATTRIBUTION — {config.MODEL_VERSION}",
         f"as of {dt.datetime.now(dt.UTC):%Y-%m-%d %H:%MZ}"
         f" | window: {days}d since strategy epoch", ""]
    if not records:
        return "\n".join(L + ["No desk fills in window.", ""])

    L += ["A. EXECUTION STYLE  (the maker/taker question)"]
    L += _fmt("maker", _agg([r for r in records if r["maker"]]))
    L += _fmt("taker", _agg([r for r in records if not r["maker"]]))
    L += _fmt("ALL", _agg(records))

    L += ["", "B. ENTRY PRICE BUCKET  (where the model is trustworthy)"]
    for lo, hi in ((0.0, 0.15), (0.15, 0.35), (0.35, 0.65), (0.65, 0.85), (0.85, 1.01)):
        sub = [r for r in records if lo <= r["price"] < hi]
        if sub:
            L += _fmt(f"{lo:.2f}-{hi:.2f}", _agg(sub))

    L += ["", "C. UTC HOUR"]
    for h in range(24):
        sub = [r for r in records
               if dt.datetime.fromtimestamp(r["ts"], dt.UTC).hour == h]
        if sub:
            L += _fmt(f"{h:02d}Z", _agg(sub))

    a = _agg(records)
    L += ["", "D. VERDICT"]
    if not a["settled"]:
        L += ["   No settled fills yet — no edge conclusion available."]
    else:
        capture = (a["realized"] / a["promised"]) if abs(a["promised"]) > 1e-9 else None
        drift = a["real_win"] - a["pred_win"]
        L += [f"   Edge capture: {capture:.0%} of promised" if capture is not None
              else "   Edge capture: n/a",
              f"   Calibration drift on fills: {drift:+.3f}"
              f" (realized minus predicted win rate)",
              "   Adverse selection is the gap between promised and realized;",
              "   a negative drift means we fill when the market knows better."]
    L += ["", f"   (settled fills only; {a['open']} still open)", ""]
    return "\n".join(L)


def main(path=config.DB_PATH, days: int = 7) -> str:
    con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        report = render(con, days)
    finally:
        con.close()
    print(report, end="")
    return report


if __name__ == "__main__":
    n = 7
    if "--days" in sys.argv:
        n = int(sys.argv[sys.argv.index("--days") + 1])
    main(days=n)
