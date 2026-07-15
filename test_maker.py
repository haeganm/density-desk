"""Maker quoting: candidate math, fail-safe cancels, journal-before-send posts."""
import json
import time

import httpx
import pytest

import config
import db


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
    from engine import rails
    monkeypatch.setattr(rails, "HALT_FILE", state / "HALT_UNTIL")
    return db.connect()


class FakeKalshi:
    can_trade = True

    def __init__(self, resting=None):
        self.resting = resting or []
        self.canceled = []
        self.created = []

    def orders(self, status="resting"):
        return list(self.resting)

    def positions(self):
        return []

    def cancel_order(self, oid):
        self.canceled.append(oid)
        return {}

    def create_order(self, ticker, buy_side, count, price, post_only=False,
                     client_order_id=None):
        self.created.append((ticker, buy_side, count, price, post_only))
        return {"order": {"order_id": "srv-mk-1"}}


def test_maker_candidate_math():
    from engine.scanner import _maker_candidate
    px, edge = _maker_candidate("yes", 0.30, 0.40, 0.38, 1.0)   # improve bid 1 tick
    assert px == 0.31 and edge == pytest.approx(0.07)           # maker fills are free
    px, edge = _maker_candidate("no", 0.30, 0.40, 0.68, 1.0)    # no-price = 1 - 0.39
    assert px == 0.61 and edge == pytest.approx(0.07)
    assert _maker_candidate("yes", 0.40, 0.41, 0.60, 1.0) is None   # no room inside
    assert _maker_candidate("yes", 0.30, 0.40, 0.38, 3.5) is None   # catalyst margin
    assert _maker_candidate("yes", 0.0, 0.40, 0.38, 1.0) is None    # one-sided book


def test_disarmed_cancels_every_resting_quote(tmp_path, monkeypatch):
    con = _desk(tmp_path, monkeypatch)
    from engine import maker
    k = FakeKalshi(resting=[{"client_order_id": "dd-mk-abc", "order_id": "srv-1",
                             "ticker": "T-1", "side": "bid",
                             "yes_price_dollars": "0.3100",
                             "remaining_count_fp": "2.00"}])
    log = maker.manage(k, con, [], {"cash": 25, "equity": 25,
                                    "pos_cost": {}, "pos_count": {}, "resting": []})
    assert k.canceled == ["srv-1"] and "disarmed" in log[0]


def test_edge_gone_cancels(tmp_path, monkeypatch):
    con = _desk(tmp_path, monkeypatch)
    from engine import maker
    config.ARMED_FILE.write_text("25.00")
    k = FakeKalshi(resting=[{"client_order_id": "dd-mk-abc", "order_id": "srv-1",
                             "ticker": "T-1", "side": "bid",
                             "yes_price_dollars": "0.3100",
                             "remaining_count_fp": "2.00"}])
    maker.manage(k, con, [], {"cash": 25, "equity": 25,
                              "pos_cost": {}, "pos_count": {}, "resting": []})
    assert k.canceled == ["srv-1"]
    why = con.execute("SELECT detail FROM events WHERE kind='maker_cancel'").fetchone()
    assert "edge_gone" in why[0]


def test_still_clears_holds_quote_when_our_own_bid_hid_the_edge(tmp_path, monkeypatch):
    con = _desk(tmp_path, monkeypatch)
    from engine.maker import _still_clears
    now = time.time()
    con.execute("INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?)",
                (now, "T-1", "EV", "BTC", 100.0, now + 3600, 0.40, 0.30, 0.42, 100.0))
    con.commit()
    assert _still_clears(con, "T-1", "yes", 0.30, now) is True    # 0.40-0.30 >= 0.01
    assert _still_clears(con, "T-1", "yes", 0.395, now) is False  # margin gone
    assert _still_clears(con, "T-1", "no", 0.55, now) is True     # no-fair = 0.60
    assert _still_clears(con, "T-1", "yes", 0.30, now + 400) is False   # stale fair
    assert _still_clears(con, "MISSING", "yes", 0.10, now) is False


def test_amend_only_improves_price_and_survives_failure(tmp_path, monkeypatch):
    con = _desk(tmp_path, monkeypatch)
    from engine import maker
    from engine.scanner import Edge

    order = {"order_id": "srv-1", "client_order_id": "dd-mk-x", "ticker": "T-1"}
    con.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?)",
                (time.time(), "srv-1", "dd-mk-x", "T-1", "yes", "buy", 2, .35, .50,
                 "resting"))
    con.commit()
    better = Edge("T-1", "EV", "BTC", "yes", .31, .50, .18, time.time() + 3600)

    class Amender(FakeKalshi):
        def __init__(self):
            super().__init__()
            self.amended = []

        def amend_order(self, oid, ticker, buy_side, count, price,
                        client_order_id=None):
            self.amended.append((oid, ticker, buy_side, count, price))
            return {"order_id": oid}

    k = Amender()
    assert maker._amend(k, con, order, better, 2) == .31
    assert k.amended == [("srv-1", "T-1", "yes", 2, .31)]
    assert con.execute("SELECT price FROM orders WHERE order_id='srv-1'"
                       ).fetchone()[0] == .31

    class Broken(FakeKalshi):
        def amend_order(self, *a, **kw):
            raise httpx.ConnectError("boom")

    assert maker._amend(Broken(), con, order, better, 2) is None   # quote survives
    assert con.execute("SELECT detail FROM events WHERE kind='maker_amend_fail'"
                       ).fetchone() is not None


def test_quote_reserves_worst_case_against_the_daily_loss_rail(tmp_path, monkeypatch):
    # a resting quote is a loss we have already offered; it must be reserved
    # BEFORE posting, or the day's realized loss can overshoot the limit.
    con = _desk(tmp_path, monkeypatch)
    from engine import maker, rails
    from engine.scanner import Edge
    config.ARMED_FILE.write_text("25.00")
    settle = time.time() + 3 * 3600
    scenarios = ((0.40, 0.42, ()), (0.60, 0.58, ("T-1",)))
    edge = Edge("T-1", "EV", "BTC", "yes", 0.30, 0.60, 0.29, settle,
                scenarios=scenarios, maker=True)
    k = FakeKalshi()
    state = {"cash": 25, "equity": 25, "pos_cost": {}, "pos_count": {}, "resting": []}
    assert maker.manage(k, con, [edge], dict(state)) != []       # baseline: posts
    k.created.clear()

    # the regression: maker.manage shipped WITHOUT consulting the daily reserve,
    # so a resting quote could commit worst-case loss past the day's limit.
    # (Wall-clock note: a real -$5 day cannot be faked here — the reserve budgets
    # by the SETTLE's UTC day, which `now + 3h` may not share. Assert the call.)
    seen = {}

    def refuse(con_, settle_ts, reserved):
        seen["settle_ts"], seen["reserved"] = settle_ts, reserved
        raise rails.RailsHalt("daily reserve exhausted")

    monkeypatch.setattr(rails, "check_daily_reserve", refuse)
    assert maker.manage(k, con, [edge], dict(state)) == []
    assert k.created == []
    assert seen["settle_ts"] == settle
    assert seen["reserved"] > 0.20               # the quote's premium is reserved

    # and the reserve counts what is already resting for that settle's day
    con.execute("INSERT INTO contracts VALUES(?,?,?,?,?,?,?)",
                ("T-9", "EV9", "BTC", settle, "greater", 100.0, None))
    con.commit()
    assert maker._day_reserve(con, settle, [("T-9", "no", 2.90)], {}, 0.30) == \
        pytest.approx(3.20)
    # a contract settling on another day is not this day's risk
    con.execute("UPDATE contracts SET settle_ts=? WHERE ticker='T-9'",
                (settle + 3 * 86400,))
    con.commit()
    assert maker._day_reserve(con, settle, [("T-9", "no", 2.90)], {}, 0.30) == \
        pytest.approx(0.30)


def test_post_journals_before_send_and_respects_gates(tmp_path, monkeypatch):
    con = _desk(tmp_path, monkeypatch)
    from engine import maker
    from engine.scanner import Edge
    config.ARMED_FILE.write_text("25.00")
    settle = time.time() + 3 * 3600
    scenarios = ((0.40, 0.42, ()), (0.60, 0.58, ("T-1",)))
    good = Edge("T-1", "EV", "BTC", "yes", 0.30, 0.60, 0.29, settle,
                scenarios=scenarios, maker=True)
    # market says only 50% profitable -> below the 0.55 floor, must not post
    weak = Edge("T-2", "EV2", "BTC", "yes", 0.30, 0.60, 0.29, settle,
                scenarios=((0.40, 0.50, ()), (0.60, 0.50, ("T-2",))), maker=True)
    k = FakeKalshi()
    state = {"cash": 25, "equity": 25, "pos_cost": {}, "pos_count": {}, "resting": []}
    log = maker.manage(k, con, [weak], dict(state))
    assert k.created == [] and log == []
    log = maker.manage(k, con, [good], dict(state))
    assert len(k.created) == 1
    ticker, side, n, price, post_only = k.created[0]
    assert (ticker, side, price, post_only) == ("T-1", "yes", 0.30, True)
    assert n * price <= config.PER_TRADE_CAP + 1e-9
    row = con.execute("SELECT order_id,status FROM orders WHERE ticker='T-1'").fetchone()
    assert row == ("srv-mk-1", "resting")
    assert con.execute("SELECT order_id FROM order_evidence WHERE ticker='T-1'"
                       ).fetchone()[0] == "srv-mk-1"
    assert log and log[0].startswith("QUOTE yes T-1")


if __name__ == "__main__":
    test_maker_candidate_math()
    print("PASS test_maker_candidate_math")
