"""Read-only historical edge evidence, without resampled profit forecasts."""
import datetime as dt
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402 - direct script execution needs the repository root
from engine import fees  # noqa: E402

CLEAN_TS = int(dt.datetime(2026, 7, 13, 8, 29, tzinfo=dt.UTC).timestamp())


def report(path=config.DB_PATH) -> str:
    con = sqlite3.connect(path)
    lines = [
        "HISTORICAL EDGE EVIDENCE",
        "forward-fix cutoff 2026-07-13T08:29:00Z",
    ]
    try:
        rows = con.execute(
            """SELECT s.realized, sn.fair, (sn.bid+sn.ask)/2,
                      sn.asset, sn.settle_ts
               FROM settles s JOIN snapshots sn ON sn.ticker=s.ticker
               WHERE sn.ts >= ? AND s.realized IS NOT NULL
                 AND sn.bid > 0 AND sn.ask < 1
                 AND sn.ts=(SELECT MAX(s2.ts) FROM snapshots s2
                            WHERE s2.ticker=sn.ticker AND s2.ts >= ?)""",
            (CLEAN_TS, CLEAN_TS),
        ).fetchall()
        lines.extend(["", "CALIBRATION AFTER FORWARD FIX"])
        if rows:
            n = len(rows)
            groups = {}
            for out, fair, mid, asset, settle_ts in rows:
                groups.setdefault((asset, settle_ts), []).append(
                    ((fair - out) ** 2, (mid - out) ** 2))
            scores = [(sum(x for x, _ in values) / len(values),
                       sum(x for _, x in values) / len(values))
                      for values in groups.values()]
            model_brier = sum(x for x, _ in scores) / len(scores)
            market_brier = sum(x for _, x in scores) / len(scores)
            timestamps = {settle for _, settle in groups}
            days = {dt.datetime.fromtimestamp(ts, dt.UTC).date() for ts in timestamps}
            lines.extend([
                f"  contract observations: {n}",
                f"  asset-settlement keys: {len(groups)}",
                f"  settlement timestamps / UTC days: {len(timestamps)} / {len(days)}",
                f"  event-balanced Brier model/market-mid: "
                f"{model_brier:.4f}/{market_brier:.4f}",
            ])
        else:
            lines.append("  no settled observations")

        for label, cutoff in (("ALL", 0), ("POST-FIX", CLEAN_TS)):
            edges = con.execute(
                """SELECT e.side, e.price, e.edge, s.realized,
                          sn.asset, sn.settle_ts
                   FROM edges e JOIN settles s ON s.ticker=e.ticker
                   LEFT JOIN snapshots sn ON sn.ticker=e.ticker
                     AND sn.ts=(SELECT MAX(s2.ts) FROM snapshots s2
                                WHERE s2.ticker=e.ticker AND s2.ts >= ?)
                   WHERE e.ts >= ? AND s.realized IS NOT NULL
                     AND e.ts=(SELECT MAX(e2.ts) FROM edges e2
                               WHERE e2.ticker=e.ticker AND e2.side=e.side
                                 AND e2.ts >= ?)""",
                (cutoff, cutoff, cutoff),
            ).fetchall()
            lines.extend(["", f"FILTERED SIGNALS {label} (HYPOTHETICAL, NOT FILLS)"])
            if not edges:
                lines.append("  no settled signals")
                continue
            pnl = [
                (realized if side == "yes" else 1 - realized)
                - price - fees.taker_fee(price, 1)
                for side, price, _, realized, _, _ in edges
            ]
            keys = {(asset, settle_ts) for *_, asset, settle_ts in edges}
            lines.extend([
                f"  contract signals: {len(pnl)} across {len(keys)} asset-settlement keys",
                f"  total one-contract P&L: {sum(pnl):+.2f}",
                f"  mean P&L/contract: {sum(pnl) / len(pnl):+.3f}",
                f"  profitable signals: {sum(value > 0 for value in pnl)}/{len(pnl)}",
            ])
    finally:
        con.close()
    lines.extend(["", "No Monte Carlo or profit probability is produced."])
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    print(report(), end="")
