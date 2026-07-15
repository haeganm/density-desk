"""sync_portfolio against doc-shaped Kalshi responses (fp/dollars string fields)."""
import pathlib
import tempfile
import time

import config


def _fresh_db():
    config.DB_PATH = pathlib.Path(tempfile.mkdtemp(prefix="dd-sync-")) / "t.db"
    import db
    return db.connect()


class FakeKalshi:
    can_trade = True

    def fills(self, **kw):
        return [
            {"fill_id": "f1", "order_id": "o1", "ticker": "KXBTCD-X-T60000",
             "outcome_side": "yes", "count_fp": "3.00",
             "yes_price_dollars": "0.5500", "no_price_dollars": "0.4500",
             "is_taker": True, "fee_cost": "0.06",
             "created_time": "2026-07-13T08:00:00Z"},
            {"order_id": "o2", "ticker": "NOID", "outcome_side": "no",
             "no_price_dollars": "0.2000", "count_fp": "1.00"},   # no fill_id -> skipped
        ]

    def settlements(self, **kw):
        return [{"ticker": "KXBTCD-X-T60000", "market_result": "yes",
                 "revenue": 300, "yes_total_cost_dollars": "1.650000",
                 "no_total_cost_dollars": "0", "fee_cost": "0.06",
                 "settled_time": "2026-07-13T09:00:00Z"}]


def test_fill_and_settlement_parsing_and_idempotency():
    from run_desk import sync_portfolio
    con = _fresh_db()
    k = FakeKalshi()
    sync_portfolio(k, con)
    sync_portfolio(k, con)      # re-run must not duplicate or drift
    rows = con.execute("SELECT ticker, side, count, price, fee, trade_id "
                       "FROM fills").fetchall()
    assert len(rows) == 1, f"dedup/skip failed: {rows}"
    assert rows[0] == ("KXBTCD-X-T60000", "yes", 3.0, 0.55, 0.06, "f1")
    settles = con.execute("SELECT ticker, realized, pnl FROM settles").fetchall()
    assert len(settles) == 1
    t, realized, pnl = settles[0]
    assert realized == 1
    # 3 yes @ .55 -> revenue $3.00 - cost $1.65 - fee $0.06 = +$1.29
    assert abs(pnl - 1.29) < 1e-9


def test_uncertain_order_is_relinked_before_fill_ingestion():
    import db
    from run_desk import sync_portfolio

    con = _fresh_db()
    con.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?)", (
        1_800_000_000.0, "?", "client-1", "T", "yes", "buy", 2, .40, .70,
        "uncertain: timeout",
    ))
    db.record_order_evidence(
        con, client_order_id="client-1", ticker="T", asset="BTC",
        settle_ts=1_800_003_600.0, event_profit_model=.7,
        event_profit_market=.6, expected_pnl=.5, worst_loss=.82,
    )
    con.commit()

    class ReconciledKalshi(FakeKalshi):
        def orders(self, status=None):
            assert status is None
            return [{"client_order_id": "client-1", "order_id": "server-1",
                     "status": "executed"}]

        def fills(self, **kw):
            return [{
                "fill_id": "linked-fill", "order_id": "server-1", "ticker": "T",
                "outcome_side": "yes", "action": "buy", "count_fp": "2.00",
                "yes_price_dollars": "0.4000", "fee_cost": "0.02",
                "created_time": "2026-07-13T08:00:00Z",
            }]

        def settlements(self, **kw):
            return []

    sync_portfolio(ReconciledKalshi(), con)
    assert con.execute(
        "SELECT order_id,status FROM orders WHERE client_order_id='client-1'"
    ).fetchone() == ("server-1", "reconciled:executed")
    assert con.execute(
        "SELECT order_id FROM order_evidence WHERE client_order_id='client-1'"
    ).fetchone()[0] == "server-1"
    assert con.execute(
        "SELECT order_id,fair_at_entry FROM fills WHERE trade_id='linked-fill'"
    ).fetchone() == ("server-1", .70)


def test_netted_position_is_not_booked_as_a_total_loss():
    """2026-07-15 live: two WINNING tickers booked -$4.04 and tripped the daily
    rail while the account balance never moved. Kalshi pays $1 per matched pair
    when offsetting lots net, and reports revenue=0 for the flattened ticker."""
    from run_desk import sync_portfolio

    con = _fresh_db()

    class Netted(FakeKalshi):
        def fills(self, **kw):     # desk bought NO x2 @ .85; a manual close bought YES x2 @ .12
            return [
                {"fill_id": "a", "order_id": "o1", "ticker": "T", "outcome_side": "no",
                 "action": "sell", "count_fp": "2.00", "no_price_dollars": "0.8500",
                 "fee_cost": "0.03", "created_time": "2026-07-14T21:43:00Z"},
                {"fill_id": "b", "order_id": "manual", "ticker": "T", "outcome_side": "yes",
                 "action": "sell", "count_fp": "2.00", "yes_price_dollars": "0.1200",
                 "fee_cost": "0.00", "created_time": "2026-07-15T02:07:00Z"},
            ]

        def settlements(self, **kw):
            return [{"ticker": "T", "market_result": "no", "revenue": 0,
                     "yes_total_cost_dollars": "0.240000",
                     "no_total_cost_dollars": "1.700000", "fee_cost": "0.032700",
                     "settled_time": "2026-07-15T21:03:00Z"}]

    sync_portfolio(Netted(), con)
    pnl = con.execute("SELECT pnl FROM settles WHERE ticker='T'").fetchone()[0]
    # 2 matched pairs pay $2.00: 2.00 - 0.24 - 1.70 - 0.0327 = +0.0273, NOT -1.97
    assert abs(pnl - 0.0273) < 1e-6, pnl

    # a plain held-to-settlement winner is unaffected (only one side has cost)
    con2 = _fresh_db()
    sync_portfolio(FakeKalshi(), con2)
    held = con2.execute(
        "SELECT pnl FROM settles WHERE ticker='KXBTCD-X-T60000'").fetchone()[0]
    assert abs(held - (3.00 - 1.65 - 0.06)) < 1e-6, held


def test_stranded_sent_order_is_reconciled_or_fails_sync():
    # crash (or 2xx with junk body) between journal and ack leaves status='sent';
    # it must be hunted exactly like an uncertain order, never silently ignored
    import db
    import pytest
    from run_desk import sync_portfolio

    con = _fresh_db()
    con.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?)", (
        1_800_000_000.0, "?", "crashed-client", "T", "yes", "buy", 1, .4, .7, "sent",
    ))
    con.commit()

    class MissingOrder(FakeKalshi):
        def orders(self, status=None):
            return []

    with pytest.raises(RuntimeError, match="unresolved uncertain orders"):
        sync_portfolio(MissingOrder(), con)

    class ReconciledKalshi(FakeKalshi):
        def orders(self, status=None):
            return [{"client_order_id": "crashed-client", "order_id": "server-9",
                     "status": "executed"}]

        def fills(self, **kw):
            return []

        def settlements(self, **kw):
            return []

    sync_portfolio(ReconciledKalshi(), con)
    assert con.execute(
        "SELECT order_id,status FROM orders WHERE client_order_id='crashed-client'"
    ).fetchone() == ("server-9", "reconciled:executed")


def test_unresolved_uncertain_order_fails_portfolio_sync():
    import db
    import pytest
    from run_desk import sync_portfolio

    con = _fresh_db()
    con.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?)", (
        1_800_000_000.0, "?", "missing-client", "T", "yes", "buy", 1, .4, .7,
        "uncertain: timeout",
    ))
    db.record_order_evidence(
        con, client_order_id="missing-client", ticker="T", asset="BTC",
        settle_ts=1_800_003_600.0, event_profit_model=.7,
        event_profit_market=.6, expected_pnl=.2, worst_loss=.41,
    )
    con.commit()

    class MissingOrder(FakeKalshi):
        def orders(self, status=None):
            return []

    with pytest.raises(RuntimeError, match="unresolved uncertain orders"):
        sync_portfolio(MissingOrder(), con)


def test_outcome_waits_for_finality_and_survives_long_delay():
    import datetime as dt
    import run_desk

    con = _fresh_db()
    planned = time.time() - 10 * 86400
    ticker = "OLD-T100"
    con.execute("INSERT INTO contracts VALUES(?,?,?,?,?,?,?)",
                (ticker, "E", "BTC", planned, "greater", 100.0, None))
    con.execute("INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?)",
                (planned - 60, ticker, "E", "BTC", 100.0, planned,
                 .60, .58, .62, 100.0))
    con.commit()

    class MarketLookup:
        def __init__(self, status):
            self.status = status

        def markets_by_tickers(self, tickers):
            assert tickers == [ticker]
            return [{
                "ticker": ticker, "status": self.status, "result": "yes",
                "expiration_value": "101.25",
                "settlement_ts": "2026-07-15T03:00:00Z",
            }]

    run_desk.record_settlements(MarketLookup("determined"), con)
    assert con.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0] == 0

    run_desk.record_settlements(MarketLookup("finalized"), con)
    expected = dt.datetime.fromisoformat("2026-07-15T03:00:00+00:00").timestamp()
    assert con.execute(
        "SELECT expiration_value,finalized_ts FROM outcomes WHERE asset='BTC'"
    ).fetchone() == (101.25, expected)
    assert con.execute(
        "SELECT ts,realized FROM settles WHERE ticker=?", (ticker,)
    ).fetchone() == (expected, 1)


def test_scan_failure_still_runs_portfolio_sync(monkeypatch):
    import run_desk

    con = _fresh_db()
    calls = []

    def fail_scan():
        raise RuntimeError("scanner broke")

    monkeypatch.setattr(run_desk.scanner, "fetch_all_chains", fail_scan)
    monkeypatch.setattr(run_desk, "sync_portfolio",
                        lambda kalshi, connection: calls.append("sync"))
    monkeypatch.setattr(run_desk, "record_settlements",
                        lambda kalshi, connection: calls.append("settles"))
    chains, edges, actions, err, next_settle, portfolio, health = run_desk.run_cycle(
        object(), con)

    assert (chains, edges, actions, next_settle) == (None, [], [], None)
    assert portfolio is None and not health["scan_ok"]
    assert "scanner broke" in err
    assert calls == ["sync", "settles"]


if __name__ == "__main__":
    test_fill_and_settlement_parsing_and_idempotency()
    print("PASS test_fill_and_settlement_parsing_and_idempotency")
