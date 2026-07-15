"""Cross-component regressions for the 2026-07-14 correctness audit."""
import datetime as dt
import json
import time
from types import SimpleNamespace

import pytest

import config
import db
from dashboard import app as dashboard
from engine import executor, rails
from engine.scanner import Edge
from ops import brief, operator_review
from scripts import edge_analysis, profit_forecast


def _isolated_desk(tmp_path, monkeypatch):
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
    monkeypatch.setattr(rails, "HALT_FILE", state / "HALT_UNTIL")
    dashboard._last_status = {}
    return root, state, db.connect()


def test_exact_utc_strategy_timestamps():
    assert config.STRATEGY_EPOCH == int(
        dt.datetime(2026, 7, 14, 14, 30, tzinfo=dt.UTC).timestamp())
    assert edge_analysis.CLEAN_TS == int(
        dt.datetime(2026, 7, 13, 8, 29, tzinfo=dt.UTC).timestamp())


def test_strategy_pnl_is_consistent_and_fees_count_once(tmp_path, monkeypatch):
    _, state, con = _isolated_desk(tmp_path, monkeypatch)
    now = time.time()
    settle = now + 3600
    con.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?)",
                (now, "o1", "c1", "DESK", "yes", "buy", 1, .5, .6, "filled"))
    con.execute("INSERT INTO settles VALUES(?,?,?,?,?)", (now, "DESK", .6, 0, -4.99))
    con.execute("INSERT INTO fills VALUES(?,?,?,?,?,?,?,?,?,?)",
                (now, "o1", "DESK", "yes", "buy", 1, .5, 2.0, .6, "f1"))
    con.execute("INSERT INTO fills VALUES(?,?,?,?,?,?,?,?,?,?)",
                (now, "manual-order", "MANUAL", "yes", "sell", 1, .5, .01,
                 None, "f-manual"))
    con.execute("INSERT INTO settles VALUES(?,?,?,?,?)",
                (now, "MANUAL-WTI", .5, 1, 100.0))
    con.execute("INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?)",
                (now, "DESK", "EVENT", "BTC", 60_000, settle, .8, .7, .9, 60_000))
    con.commit()

    day0 = now - (now % 86400)
    assert db.strategy_pnl(con, day0, now) == -4.99
    rails.check_global(con, 0, 0)  # fill fee is already included in settlement pnl

    (state / "status.json").write_text(json.dumps({
        "ts": now, "utc": "2026-07-14T20:00:00+00:00", "index": {},
        "positions": [{"ticker": "DESK", "count": -1, "cost": .2}],
        "edges": [], "actions": [], "balance": 25.78,
    }), encoding="utf-8")
    view = dashboard.state()
    assert view["pnl_today"] == -4.99
    assert view["pos_fairs"]["DESK"] == pytest.approx(.2)
    assert view["rails"]["bankroll"] == 25.78
    assert {row["ticker"]: row["source"] for row in view["fills"]} == {
        "DESK": "desk", "MANUAL": "manual"}

    text = brief.write_brief()
    assert "Mode: DISARMED" in text
    assert "strategy realized P&L today: -4.99" in text

    con.execute("UPDATE settles SET pnl=-5.00 WHERE ticker='DESK'")
    con.commit()
    with pytest.raises(rails.RailsHalt, match="daily loss"):
        rails.check_global(con, 0, 0)
    assert rails.HALT_FILE.exists()


def test_executor_sizes_from_live_cash(tmp_path, monkeypatch):
    _, state, con = _isolated_desk(tmp_path, monkeypatch)
    (state / "ARMED").write_text("0")

    class Kalshi:
        can_trade = True
        count = None

        def balance(self):
            return {"balance_dollars": "27.7803"}

        def positions(self):
            return []

        def orders(self, status="resting"):
            return []

        def orderbook(self, ticker):
            return {"orderbook_fp": {"no_dollars": [["0.6000", "100.00"]]}}

        def create_order(self, ticker, buy_side, count, price, client_order_id):
            self.count = count
            return {"order": {"order_id": "o", "fill_count_fp": "0"}}

    kalshi = Kalshi()
    scenarios = ((.7, .65, ("T",)), (.3, .35, ()))
    edge = Edge("T", "E", "BTC", "yes", .4, .7, .29,
                time.time() + 3600, scenarios)
    assert executor.execute(kalshi, con, [edge], 0)[0].startswith("IOC")
    assert kalshi.count == 4  # fee-aware Kelly keeps total entry risk under the $2 cap
    evidence = con.execute(
        "SELECT order_id,model_version,event_profit_model,event_profit_market "
        "FROM order_evidence"
    ).fetchone()
    assert evidence[:2] == ("o", "r4")
    assert evidence[2:] == (0.0, 0.0)  # zero IOC fill leaves no event exposure


def test_evidence_excludes_unversioned_history_and_withholds_probability(tmp_path,
                                                                         monkeypatch):
    _, _, con = _isolated_desk(tmp_path, monkeypatch)
    epoch = config.STRATEGY_EPOCH
    for i, (asset, settle_ts, fair, mid, realized) in enumerate([
        ("BTC", epoch + 3600, .10, .12, 0),
        ("BTC", epoch + 3600, .20, .18, 0),
        ("ETH", epoch + 3600, .70, .65, 1),
    ]):
        ticker = f"T{i}"
        con.execute("INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (epoch + 10, ticker, asset, asset, 100 + i, settle_ts,
                     fair, mid - .01, mid + .01, 100))
        con.execute("INSERT INTO settles VALUES(?,?,?,?,?)",
                    (settle_ts, ticker, fair, realized, 0.0))
    con.commit()

    evidence = profit_forecast.collect_evidence(con)
    assert evidence["calibration"]["contracts"] == 0
    assert evidence["calibration"]["event_risk_keys"] == 0
    assert evidence["filled_contracts"] == 0
    report = profit_forecast.render(evidence)
    assert "independent" not in report.lower()
    assert "INSUFFICIENT LIVE EVIDENCE" in report
    assert "%" not in report
    report.encode("cp1252")


def test_operator_brief_deduplicates_edges_and_uses_explicit_settle(tmp_path,
                                                                    monkeypatch):
    _, _, con = _isolated_desk(tmp_path, monkeypatch)
    now = time.time()
    ticker = "KXBTCD-26JUL1417-T64000"
    con.execute("INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?)",
                (now, ticker, "EV", "BTC", 64_000, now + 3600, .6, .5, .6, 64_000))
    for ts, edge in ((now - 100, .05), (now - 50, .08)):
        con.execute("INSERT INTO edges VALUES(?,?,?,?,?,?,?,?,?)",
                    (ts, ticker, "yes", .6, .5, edge, 0, 0, "scan"))
    con.execute("INSERT INTO events VALUES(?,?,?)", (now - 200, "start", "boot"))
    for _ in range(3):
        con.execute("INSERT INTO events VALUES(?,?,?)",
                    (now - 10, "density_skip", "ETH repair over limit"))
    con.commit()

    text = brief.write_brief()
    assert text.count(ticker) == 1
    assert "one per ticker/side" in text
    assert "settles" in text and "observed" in text
    assert "[density_skip] x3" in text


def test_operator_review_runs_claude_without_tools(tmp_path, monkeypatch):
    _, _, con = _isolated_desk(tmp_path, monkeypatch)
    con.close()
    seen = {}

    monkeypatch.setattr(operator_review, "write_brief", lambda: "brief")
    monkeypatch.setattr(operator_review.shutil, "which", lambda name: "claude")

    def fake_run(args, **kwargs):
        seen["args"] = args
        return SimpleNamespace(stdout="VERDICT: OK", stderr="")

    monkeypatch.setattr(operator_review.subprocess, "run", fake_run)
    operator_review.main()
    args = seen["args"]
    assert args[args.index("--tools") + 1] == ""
    assert "--safe-mode" in args and "--no-session-persistence" in args
    assert "NEVER create, edit, or delete files" in operator_review.PROMPT


def test_operator_review_output_is_safe_for_windows_console():
    assert operator_review.console_safe("loss \u22120.10", "cp1252") == "loss ?0.10"
