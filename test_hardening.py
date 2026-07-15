"""Next-run hardening regressions for reconciliation, sizing, and IOC accounting."""
from __future__ import annotations

import datetime as dt
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from kalshi.client import KalshiAPIError

import config
import db
import run_desk
from engine import executor, rails
from engine.scanner import Edge


def _desk(tmp_path, monkeypatch):
    root = tmp_path / "desk"
    state = root / "state"
    state.mkdir(parents=True)
    (state / "catalysts.json").write_text(json.dumps({
        "generated_ts": time.time(), "windows": [],
    }))
    monkeypatch.setattr(config, "ROOT", root)
    monkeypatch.setattr(config, "DB_PATH", root / "desk.db")
    monkeypatch.setattr(config, "KILL_FILE", root / "KILL")
    monkeypatch.setattr(config, "ARMED_FILE", state / "ARMED")
    monkeypatch.setattr(config, "SHAKEOUT", False)
    monkeypatch.setattr(config, "PER_EVENT_CAP", 10.0)
    monkeypatch.setattr(config, "ORDERS_PER_CYCLE", 2)
    monkeypatch.setattr(rails, "HALT_FILE", state / "HALT_UNTIL")
    config.ARMED_FILE.write_text("0.00")
    return db.connect()


def _edge(ticker, event, settle, asset="BTC", price=.25, fair=.49):
    scenarios = ((.75, .65, (ticker,)), (.25, .35, ()))
    return Edge(ticker, event, asset, "yes", price, fair, fair - price, settle,
                scenarios)


def _record_edge(con, edge, ts=None):
    con.execute(
        "INSERT INTO edges VALUES(?,?,?,?,?,?,?,?,?)",
        (ts or time.time(), edge.ticker, edge.side, edge.fair, edge.price,
         edge.edge, 0, 0, "scan"),
    )
    con.commit()


class FakeKalshi:
    can_trade = True

    def __init__(self, *, positions=(), cash=27.78, portfolio=0.0, fills=()):
        self._positions = list(positions)
        self._fills = list(fills)
        self.cash = cash
        self.portfolio = portfolio
        self.created = []

    def balance(self):
        return {
            "balance_dollars": f"{self.cash:.4f}",
            "portfolio_value_dollars": f"{self.portfolio:.4f}",
            "portfolio_value": round(self.portfolio * 100),
        }

    def positions(self):
        return self._positions

    def orders(self, status="resting"):
        return []

    def fills(self, **_kwargs):
        return []

    def settlements(self, **_kwargs):
        return []

    def orderbook(self, _ticker):
        return {"orderbook_fp": {"no_dollars": [["0.7500", "20.00"]]}}

    def create_order(self, ticker, buy_side, count, price, client_order_id):
        index = len(self.created)
        filled = self._fills[index] if index < len(self._fills) else count
        self.created.append((ticker, count))
        return {"order": {
            "order_id": f"o{index}",
            "fill_count_fp": str(filled),
            "average_fill_price": str(price),
            "average_fee_paid": "0.001",
        }}


def test_event_cap_combines_asset_settlement_but_not_later_settlement(tmp_path,
                                                                      monkeypatch):
    con = _desk(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "PER_EVENT_CAP", 1.0)
    now = time.time()
    settle = now + 3600
    held = "KXBTCD-HELD-T60000"
    con.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?)",
                (now, "old", "old", held, "yes", "buy", 2, .5, .6, "filled"))
    con.execute("INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?)",
                (now, held, "LADDER", "BTC", 60_000, settle, .6, .5, .6, 60_000))
    same = _edge("KXBTC-SAME-B60000", "RANGE", settle)
    later = _edge("KXBTC-LATER-B60000", "OTHER", settle + 3600)
    _record_edge(con, same, now + 1)
    _record_edge(con, later, now + 2)
    kalshi = FakeKalshi(positions=[{
        "ticker": held,
        "market_exposure_dollars": "1.0000",
        "position_fp": "2.00",
    }])

    executor.execute(kalshi, con, [same, later], 0)

    assert [ticker for ticker, _ in kalshi.created] == [later.ticker]
    assert 0 < kalshi.created[0][1] * later.price <= config.PER_TRADE_CAP
    reasons = dict(con.execute("SELECT ticker,reason FROM edges"))
    assert reasons[same.ticker] == "event_cap"
    assert reasons[later.ticker] == "full_fill"


def test_unmapped_strategy_position_fails_closed_for_its_asset(tmp_path, monkeypatch):
    con = _desk(tmp_path, monkeypatch)
    now = time.time()
    held = "KXBTCD-UNMAPPED-T60000"
    con.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?)",
                (now, "old", "old", held, "yes", "buy", 1, .5, .6, "filled"))
    edge = _edge("KXBTC-NEW-B60000", "RANGE", now + 3600)
    _record_edge(con, edge)
    kalshi = FakeKalshi(positions=[{
        "ticker": held,
        "market_exposure_dollars": "0.5000",
        "position_fp": "1.00",
    }])

    executor.execute(kalshi, con, [edge], 0)

    assert not kalshi.created
    assert con.execute("SELECT reason FROM edges WHERE ticker=?",
                       (edge.ticker,)).fetchone()[0] == "unmapped_position"


def test_unmapped_manual_position_only_counts_toward_global_exposure(tmp_path,
                                                                     monkeypatch):
    con = _desk(tmp_path, monkeypatch)
    edge = _edge("KXBTC-NEW-B60000", "RANGE", time.time() + 3600)
    _record_edge(con, edge)
    kalshi = FakeKalshi(positions=[{
        "ticker": "KXBTCD-MANUAL-T60000",  # no strategy order: user-owned position
        "market_exposure_dollars": "0.5000",
        "position_fp": "1.00",
    }])

    executor.execute(kalshi, con, [edge], 0)

    assert [ticker for ticker, _ in kalshi.created] == [edge.ticker]
    assert con.execute("SELECT reason FROM edges WHERE ticker=?",
                       (edge.ticker,)).fetchone()[0] == "full_fill"


def test_manual_change_to_versioned_desk_ticker_fails_closed(tmp_path, monkeypatch):
    con = _desk(tmp_path, monkeypatch)
    now, settle = time.time(), time.time() + 3600
    held = "KXBTCD-R4-T60000"
    con.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?)",
                (now, "old", "coid", held, "yes", "buy", 2, .4, .7, "filled"))
    db.record_order_evidence(
        con, client_order_id="coid", order_id="old", ticker=held, asset="BTC",
        settle_ts=settle, event_profit_model=.7, event_profit_market=.6,
        expected_pnl=.5, worst_loss=.81,
    )
    con.execute("INSERT INTO fills VALUES(?,?,?,?,?,?,?,?,?,?)",
                (now, "old", held, "yes", "buy", 2, .4, .01, .7, "r4-fill"))
    con.execute("INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?)",
                (now, held, "LADDER", "BTC", 60_000, settle, .7, .4, .5, 60_000))
    edge = _edge("KXBTC-NEW-B60000", "RANGE", settle)
    _record_edge(con, edge)
    kalshi = FakeKalshi(positions=[{
        "ticker": held, "market_exposure_dollars": "1.2000",
        "position_fp": "3.00",  # one contract did not come from the desk order
    }])

    executor.execute(kalshi, con, [edge], 0)

    assert not kalshi.created
    assert con.execute("SELECT reason FROM edges WHERE ticker=?", (edge.ticker,)) \
        .fetchone()[0] == "unmapped_position"


def test_sync_failure_blocks_execute_but_not_public_scan(tmp_path, monkeypatch):
    con = _desk(tmp_path, monkeypatch)
    calls = []
    chain = SimpleNamespace(fetched_ts=time.time())
    edge = _edge("T", "E", time.time() + 3600)
    monkeypatch.setattr(run_desk.scanner, "fetch_all_chains",
                        lambda: calls.append("scan") or {"BTC": chain})
    monkeypatch.setattr(run_desk.scanner, "scan",
                        lambda *_args: ([edge], edge.settle_ts))
    monkeypatch.setattr(run_desk, "sync_portfolio",
                        lambda *_args: (_ for _ in ()).throw(RuntimeError("auth failed")))
    monkeypatch.setattr(run_desk, "record_settlements", lambda *_args: None)
    monkeypatch.setattr(run_desk.executor, "execute",
                        lambda *_args, **_kwargs: calls.append("execute"))

    _, edges, _, err, _, _, _ = run_desk.run_cycle(FakeKalshi(), con)

    assert calls == ["scan"]
    assert edges == [edge]
    assert "auth failed" in err


def test_authoritative_portfolio_value_drives_drawdown_and_dynamic_exposure(tmp_path,
                                                                            monkeypatch):
    con = _desk(tmp_path, monkeypatch)
    edge = _edge("KXBTC-NEW-B60000", "RANGE", time.time() + 3600)
    _record_edge(con, edge)
    kalshi = FakeKalshi(
        cash=1.0,
        portfolio=19.0,
        positions=[{
            "ticker": "MANUAL",
            "market_exposure_dollars": "8.0000",
            "position_fp": "1.00",
        }],
    )
    monkeypatch.setattr(executor, "kelly_contracts", lambda *_args, **_kwargs: 1)

    executor.execute(kalshi, con, [edge], 0)

    assert kalshi.created == [(edge.ticker, 1)]
    assert float(config.ARMED_FILE.read_text()) == 20.0


def test_status_records_authoritative_equity_and_health(tmp_path, monkeypatch):
    con = _desk(tmp_path, monkeypatch)
    state_dir = config.ROOT / "state"
    monkeypatch.setattr(run_desk, "STATE", state_dir)
    portfolio = {"cash": 5.0, "equity": 20.0, "positions": [],
                 "pos_cost": {}, "pos_count": {}, "resting": []}
    health = {"scan_ok": True, "portfolio_sync_ok": True,
              "portfolio_sync_ts": time.time(), "clock_skew_s": 0.5}

    run_desk.write_status([], [], {}, portfolio=portfolio, health=health, con=con)

    assert con.execute("SELECT cash,open_cost FROM equity").fetchone() == (5.0, 15.0)
    status = json.loads((state_dir / "status.json").read_text())
    assert status["portfolio_value"] == 20.0
    assert status["portfolio_sync_ok"] is True and status["clock_skew_s"] == .5


def test_settlement_overwrites_calibration_timestamp_result_and_pnl(tmp_path,
                                                                    monkeypatch):
    con = _desk(tmp_path, monkeypatch)
    ticker = "KXBTCD-X-T60000"
    con.execute("INSERT INTO settles VALUES(?,?,?,?,?)",
                (1.0, ticker, .62, 1, 0.0))
    con.commit()
    settled_time = "2026-07-15T00:30:00Z"

    class SettlementKalshi(FakeKalshi):
        def settlements(self, **_kwargs):
            return [{
                "ticker": ticker,
                "market_result": "no",
                "revenue_dollars": "1.0000",
                "yes_total_cost_dollars": "0.0000",
                "no_total_cost_dollars": "0.6000",
                "fee_cost": "0.0100",
                "settled_time": settled_time,
            }]

    run_desk.sync_portfolio(SettlementKalshi(), con)

    ts, predicted, realized, pnl = con.execute(
        "SELECT ts,predicted,realized,pnl FROM settles WHERE ticker=?", (ticker,)
    ).fetchone()
    assert ts == dt.datetime.fromisoformat(settled_time.replace("Z", "+00:00")).timestamp()
    assert predicted == .62 and realized == 0
    assert pnl == pytest.approx(.39)


@pytest.mark.parametrize(
    "first_fill,expected_sizes,first_reason",
    [(0, [0, 3], "zero_fill"),
     (.5, [.5, 3], "partial_fill"),
     (1, [1, 3], "partial_fill")],
)
def test_ioc_accounting_and_one_submission_per_event(
        tmp_path, monkeypatch, first_fill, expected_sizes, first_reason):
    con = _desk(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "PER_EVENT_CAP", .80)
    now = time.time()
    settle = now + 3600
    first = _edge("FIRST", "LADDER", settle)
    second = _edge("SECOND", "RANGE", settle)
    paced = _edge("PACED", "LATER", settle + 3600)
    _record_edge(con, first, now - 10)  # previous scan must remain untouched
    _record_edge(con, first, now)
    _record_edge(con, second, now)
    _record_edge(con, paced, now)
    kalshi = FakeKalshi(fills=[first_fill])

    executor.execute(kalshi, con, [first, second, paced], 0)

    assert [count for _, count in kalshi.created] == [3, 3]
    first_rows = con.execute(
        "SELECT acted,size,reason FROM edges WHERE ticker=? ORDER BY ts", (first.ticker,)
    ).fetchall()
    assert first_rows == [(0, 0, "scan"), (1, expected_sizes[0], first_reason)]
    assert con.execute(
        "SELECT acted,size,reason FROM edges WHERE ticker=?", (second.ticker,)
    ).fetchone() == (0, 0, "event_pacing")
    assert con.execute(
        "SELECT acted,size,reason FROM edges WHERE ticker=?", (paced.ticker,)
    ).fetchone() == (1, expected_sizes[1], "full_fill")


def test_no_fill_price_is_inverted_before_consuming_event_room(tmp_path, monkeypatch):
    con = _desk(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "PER_EVENT_CAP", 1.0)
    monkeypatch.setattr(executor, "kelly_contracts", lambda *_args, **_kwargs: 1)
    settle = time.time() + 3600
    scenarios = ((.05, .10, ("NO-FIRST",)), (.95, .90, ()))
    first = Edge("NO-FIRST", "LADDER", "BTC", "no", .85, .95, .09, settle,
                 scenarios)
    second = _edge("YES-SECOND", "RANGE", settle)
    _record_edge(con, first)
    _record_edge(con, second)

    class NoFillPriceKalshi(FakeKalshi):
        def orderbook(self, ticker):
            side = "yes_dollars" if ticker == first.ticker else "no_dollars"
            price = ".1500" if ticker == first.ticker else ".7500"
            return {"orderbook_fp": {side: [[price, "20.00"]]}}

        def create_order(self, ticker, **kwargs):
            self.created.append((ticker, kwargs["count"]))
            return {"order": {"order_id": "o", "fill_count_fp": "1.00",
                               "average_fill_price": ".1500"}}

    kalshi = NoFillPriceKalshi()
    log = executor.execute(kalshi, con, [first, second], 0)

    assert kalshi.created == [(first.ticker, 1)]
    assert "avg 0.85" in log[0]
    assert con.execute("SELECT reason FROM edges WHERE ticker=?", (second.ticker,)) \
        .fetchone()[0] == "event_pacing"


def test_executor_records_principal_preorder_skip_reasons(tmp_path, monkeypatch):
    con = _desk(tmp_path, monkeypatch)
    now = time.time()
    edges = [
        _edge("TICKER_CAP", "A", now + 3600),
        _edge("MOVED", "B", now + 7200),
        _edge("HUMILITY", "C", now + 10_800, price=.10, fair=.21),
        _edge("NO_DEPTH", "D", now + 14_400),
        _edge("ZERO_KELLY", "E", now + 18_000, fair=.26),
    ]
    for edge in edges:
        _record_edge(con, edge, now)

    class SkipKalshi(FakeKalshi):
        def orderbook(self, ticker):
            if ticker == "MOVED":
                return {"orderbook_fp": {"no_dollars": [["0.7000", "20.00"]]}}
            if ticker == "HUMILITY":
                return {"orderbook_fp": {"no_dollars": [["0.9000", "20.00"]]}}
            if ticker == "NO_DEPTH":
                return {"orderbook_fp": {"no_dollars": []}}
            return super().orderbook(ticker)

    kalshi = SkipKalshi(positions=[{
        "ticker": "TICKER_CAP",
        "market_exposure_dollars": "2.0000",
        "position_fp": "4.00",
    }])
    executor.execute(kalshi, con, edges, 0)

    assert not kalshi.created
    assert dict(con.execute("SELECT ticker,reason FROM edges")) == {
        "TICKER_CAP": "ticker_cap",
        "MOVED": "moved_book",
        "HUMILITY": "humility",
        "NO_DEPTH": "no_depth",
        "ZERO_KELLY": "zero_kelly",
    }


def test_executor_records_global_cap_and_api_rejection(tmp_path, monkeypatch):
    con = _desk(tmp_path, monkeypatch)
    now = time.time()
    global_cap = _edge("GLOBAL", "A", now + 3600)
    rejected = _edge("REJECTED", "B", now + 7200)
    _record_edge(con, global_cap, now)
    _record_edge(con, rejected, now)

    class RejectKalshi(FakeKalshi):
        def create_order(self, ticker, *args, **kwargs):
            if ticker == rejected.ticker:
                self.created.append((ticker, kwargs.get("count", args[1] if args else None)))
                raise KalshiAPIError("rejected", 400, "bad order")
            return super().create_order(ticker, *args, **kwargs)

    # First pass is at the $12 global rail and must not submit.
    capped = RejectKalshi(portfolio=30, positions=[{
        "ticker": "MANUAL",
        "market_exposure_dollars": "12.0000",
        "position_fp": "1.00",
    }])
    executor.execute(capped, con, [global_cap], 0)
    assert not capped.created
    assert con.execute("SELECT acted,size,reason FROM edges WHERE ticker='GLOBAL'").fetchone() \
        == (0, 0, "global_cap")

    # The API-rejected request was attempted but filled nothing.
    config.ARMED_FILE.write_text("0.00")
    rejected_client = RejectKalshi()
    executor.execute(rejected_client, con, [rejected], 0)
    assert len(rejected_client.created) == 1
    assert con.execute(
        "SELECT acted,size,reason FROM edges WHERE ticker='REJECTED'"
    ).fetchone() == (1, 0, "api_rejection")


def test_ambiguous_order_failure_stops_cycle_until_reconciliation(tmp_path, monkeypatch):
    con = _desk(tmp_path, monkeypatch)
    now = time.time()
    edges = [_edge("UNCERTAIN", "A", now + 3600),
             _edge("MUST-NOT-SEND", "B", now + 7200)]
    for edge in edges:
        _record_edge(con, edge)

    class TimeoutKalshi(FakeKalshi):
        def create_order(self, ticker, *args, **kwargs):
            self.created.append((ticker, kwargs["count"]))
            raise httpx.ReadTimeout("response lost after send")

    kalshi = TimeoutKalshi()
    log = executor.execute(kalshi, con, edges, 0)

    assert [ticker for ticker, _ in kalshi.created] == ["UNCERTAIN"]
    assert dict(con.execute("SELECT ticker,reason FROM edges")) == {
        "UNCERTAIN": "uncertain_order", "MUST-NOT-SEND": "uncertain_order"}
    assert log[-1].startswith("HALT: uncertain outcome")
    assert con.execute("SELECT status FROM orders").fetchone()[0].startswith("uncertain:")


@pytest.mark.parametrize("stop_kind,reason", [("kill", "kill"),
                                                ("disarm", "disarmed")])
def test_operator_stop_is_rechecked_before_every_order(tmp_path, monkeypatch,
                                                       stop_kind, reason):
    con = _desk(tmp_path, monkeypatch)
    now = time.time()
    edges = [_edge("FIRST", "A", now + 3600), _edge("SECOND", "B", now + 7200)]
    for edge in edges:
        _record_edge(con, edge)

    class StopAfterFirst(FakeKalshi):
        def create_order(self, *args, **kwargs):
            response = super().create_order(*args, **kwargs)
            if stop_kind == "kill":
                config.KILL_FILE.write_text("")
            else:
                config.ARMED_FILE.unlink()
            return response

    kalshi = StopAfterFirst()
    executor.execute(kalshi, con, edges, 0)

    assert [ticker for ticker, _ in kalshi.created] == ["FIRST"]
    assert con.execute("SELECT reason FROM edges WHERE ticker='SECOND'").fetchone()[0] \
        == reason


def test_executor_rechecks_final_window_at_send_time(tmp_path, monkeypatch):
    con = _desk(tmp_path, monkeypatch)
    edge = _edge("TOO-LATE", "A", time.time() + 44 * 60)
    _record_edge(con, edge)
    kalshi = FakeKalshi()

    executor.execute(kalshi, con, [edge], 0)

    assert not kalshi.created
    assert con.execute("SELECT reason FROM edges").fetchone()[0] == "final_window"


@pytest.mark.parametrize("policy,fair,reason", [
    ((True, 2.0), .49, "catalyst_window"),
    ((False, 2.0), .30, "catalyst_threshold"),
])
def test_executor_rechecks_catalyst_at_send_time(tmp_path, monkeypatch,
                                                  policy, fair, reason):
    con = _desk(tmp_path, monkeypatch)
    edge = _edge("CATALYST", "A", time.time() + 3600, fair=fair)
    _record_edge(con, edge)
    monkeypatch.setattr(executor, "catalyst_policy", lambda *_args: policy)
    kalshi = FakeKalshi()

    executor.execute(kalshi, con, [edge], 0)

    assert not kalshi.created
    assert con.execute("SELECT reason FROM edges").fetchone()[0] == reason


def test_second_order_kelly_uses_cash_after_first_fill(tmp_path, monkeypatch):
    con = _desk(tmp_path, monkeypatch)
    now = time.time()
    edges = [_edge("FIRST", "A", now + 3600), _edge("SECOND", "B", now + 7200)]
    for edge in edges:
        _record_edge(con, edge)
    seen_bankrolls = []
    monkeypatch.setattr(executor, "kelly_contracts",
                        lambda _fair, _price, bankroll: seen_bankrolls.append(bankroll) or 1)

    executor.execute(FakeKalshi(fills=[1, 1]), con, edges, 0)

    assert seen_bankrolls == pytest.approx([27.78, 27.529])


def test_global_room_truncates_order_instead_of_wasting_capacity(tmp_path, monkeypatch):
    con = _desk(tmp_path, monkeypatch)
    edge = _edge("ROOM", "A", time.time() + 3600)
    _record_edge(con, edge)
    monkeypatch.setattr(executor, "kelly_contracts", lambda *_args, **_kwargs: 8)
    kalshi = FakeKalshi(portfolio=30, positions=[{
        "ticker": "MANUAL", "market_exposure_dollars": "11.5000",
        "position_fp": "1.00"}])

    executor.execute(kalshi, con, [edge], 0)

    assert [ticker for ticker, _count in kalshi.created] == [edge.ticker]


def test_kelly_is_final_position_target_and_opposite_side_is_blocked(tmp_path,
                                                                     monkeypatch):
    con = _desk(tmp_path, monkeypatch)
    now, settle = time.time(), time.time() + 3600
    ticker = "KXBTCD-TARGET-T60000"
    con.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?)",
                (now, "old", "old", ticker, "yes", "buy", 4, .2, .7, "filled"))
    con.execute("INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?)",
                (now, ticker, "LADDER", "BTC", 60_000, settle, .75, .2, .25, 60_000))
    con.commit()
    edge = _edge(ticker, "LADDER", settle)
    _record_edge(con, edge)
    monkeypatch.setattr(executor, "kelly_contracts", lambda *_args, **_kwargs: 5)
    held = {"ticker": ticker, "market_exposure_dollars": ".8000",
            "position_fp": "4.00"}

    kalshi = FakeKalshi(positions=[held])
    executor.execute(kalshi, con, [edge], 0)
    assert kalshi.created == [(ticker, 1)]

    config.ARMED_FILE.write_text("0.00")
    opposite = _edge(ticker, "LADDER", settle)
    _record_edge(con, opposite, now + 1)
    held["position_fp"] = "-4.00"
    kalshi = FakeKalshi(positions=[held])
    executor.execute(kalshi, con, [opposite], 0)
    assert not kalshi.created
    assert con.execute("SELECT reason FROM edges WHERE ticker=? ORDER BY ts DESC",
                       (ticker,)).fetchone()[0] == "opposite_position"


def test_fees_count_toward_event_cap(tmp_path, monkeypatch):
    con = _desk(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "PER_EVENT_CAP", 1.0)
    now, settle = time.time(), time.time() + 3600
    held = "KXBTCD-FEE-T60000"
    con.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?)",
                (now, "old", "old", held, "yes", "buy", 2, .49, .7, "filled"))
    con.execute("INSERT INTO fills VALUES(?,?,?,?,?,?,?,?,?,?)",
                (now, "old", held, "yes", "buy", 2, .49, .03, .7, "fill-old"))
    con.execute("INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?)",
                (now, held, "LADDER", "BTC", 60_000, settle, .7, .4, .5, 60_000))
    con.commit()
    edge = _edge("KXBTC-NEW-B60000", "RANGE", settle)
    _record_edge(con, edge)
    kalshi = FakeKalshi(positions=[{
        "ticker": held, "market_exposure_dollars": ".9800", "position_fp": "2.00"}])

    executor.execute(kalshi, con, [edge], 0)

    assert not kalshi.created
    assert con.execute("SELECT reason FROM edges WHERE ticker=?", (edge.ticker,)) \
        .fetchone()[0] == "event_cap"


def test_open_worst_loss_reserves_the_utc_settlement_day(tmp_path, monkeypatch):
    con = _desk(tmp_path, monkeypatch)
    day_start = (int(time.time()) // 86400 + 1) * 86400
    first_settle, second_settle = day_start + 3600, day_start + 7200
    held = "KXBTCD-RESERVE-T60000"
    con.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?)",
                (time.time(), "old", "old", held, "yes", "buy", 8, .6, .7, "filled"))
    con.execute("INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?)",
                (time.time(), held, "LADDER", "BTC", 60_000, first_settle,
                 .7, .5, .6, 60_000))
    con.commit()
    edge = _edge("KXBTCD-NEXT-T61000", "LATER", second_settle)
    _record_edge(con, edge)
    kalshi = FakeKalshi(positions=[{
        "ticker": held, "market_exposure_dollars": "4.8000", "position_fp": "8.00"}])

    executor.execute(kalshi, con, [edge], 0)

    assert not kalshi.created
    assert con.execute("SELECT reason FROM edges WHERE ticker=?", (edge.ticker,)) \
        .fetchone()[0] == "daily_reserve"


def test_versioned_hedged_event_reserves_joint_worst_loss_not_sum_of_costs(
        tmp_path, monkeypatch):
    con = _desk(tmp_path, monkeypatch)
    now = time.time()
    day_start = (int(now) // 86400 + 1) * 86400
    first_settle, second_settle = day_start + 3600, day_start + 7200
    positions = []
    for index, (ticker, side, signed) in enumerate((
        ("KXBTCD-HEDGE-A", "yes", 5), ("KXBTCD-HEDGE-B", "no", -5),
    )):
        order_id, client_id = f"old-{index}", f"client-{index}"
        con.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?)", (
            now + index, order_id, client_id, ticker, side, "buy", 5, .49, .70,
            "filled",
        ))
        db.record_order_evidence(
            con, client_order_id=client_id, order_id=order_id, ticker=ticker,
            asset="BTC", settle_ts=first_settle, event_profit_model=.8,
            event_profit_market=.7, expected_pnl=.2, worst_loss=.10,
        )
        con.execute("INSERT INTO fills VALUES(?,?,?,?,?,?,?,?,?,?)", (
            now + index, order_id, ticker, side, "buy", 5, .49, .01, .70,
            f"fill-{index}",
        ))
        con.execute("INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?)", (
            now, ticker, "LADDER", "BTC", 60_000 + index, first_settle,
            .70, .48, .50, 60_000,
        ))
        positions.append({
            "ticker": ticker, "market_exposure_dollars": "2.4500",
            "position_fp": f"{signed:.2f}",
        })
    con.commit()
    edge = _edge("KXBTCD-NEXT-T61000", "LATER", second_settle)
    _record_edge(con, edge)
    kalshi = FakeKalshi(positions=positions)

    executor.execute(kalshi, con, [edge], 0)

    assert [ticker for ticker, _count in kalshi.created] == [edge.ticker]


def test_arm_clock_health_keeps_authenticated_sync_sample(tmp_path, monkeypatch):
    con = _desk(tmp_path, monkeypatch)
    chain = SimpleNamespace(fetched_ts=time.time())

    class ClockKalshi(FakeKalshi):
        clock_skew_s = None

        def balance(self):
            self.clock_skew_s = .4
            return super().balance()

        def positions(self):
            self.clock_skew_s = .4
            return super().positions()

        def orders(self, status="resting"):
            self.clock_skew_s = .4
            return []

    kalshi = ClockKalshi()
    monkeypatch.setattr(run_desk.scanner, "fetch_all_chains", lambda: {"BTC": chain})

    def public_scan(*_args):
        kalshi.clock_skew_s = 12.0
        return [], time.time() + 3600

    monkeypatch.setattr(run_desk.scanner, "scan", public_scan)
    monkeypatch.setattr(run_desk, "record_settlements", lambda *_args: None)

    *_, health = run_desk.run_cycle(kalshi, con)

    assert health["clock_skew_s"] == .4
