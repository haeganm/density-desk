import time

import pytest

import config
import db
from scripts import profit_forecast


def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "desk.db")
    return db.connect()


def _surface(ts, version="w1"):
    return {
        "ts": ts, "event": "LADDER", "city": "BTC", "settle_ts": ts + 3600,
        "model_version": version, "model_center": 100, "model_iqr": 20,
        "market_center": 100, "market_iqr": 18, "width_ratio": 20 / 18,
        "contracts": [
            {"ticker": "LOW", "strike_type": "greater", "floor_strike": 90,
             "cap_strike": None, "fair": .75, "market_mid": .70},
            {"ticker": "HIGH", "strike_type": "greater", "floor_strike": 110,
             "cap_strike": None, "fair": .25, "market_mid": .30},
        ],
    }


def test_calibration_is_event_balanced_and_version_isolated(tmp_path, monkeypatch):
    con = _db(tmp_path, monkeypatch)
    ts = 1_800_000_000.0
    db.record_surface(con, _surface(ts, "w1"))
    db.record_surface(con, _surface(ts + 1, "r3"))
    db.record_outcome(con, "BTC", ts + 3600, 100.0, ts + 3700)
    con.commit()

    evidence = db.strategy_calibration(con, "w1")
    assert evidence["contracts"] == 2
    assert evidence["event_risk_keys"] == 1
    assert evidence["settlement_timestamps"] == 1
    assert evidence["utc_days"] == 1
    assert evidence["model_brier"] == pytest.approx((.25**2 + .25**2) / 2)
    assert evidence["market_brier"] == pytest.approx((.30**2 + .30**2) / 2)



def test_versioned_pnl_uses_exact_entry_fills_not_manual_settlement_aggregate(
        tmp_path, monkeypatch):
    con = _db(tmp_path, monkeypatch)
    now = time.time()
    monkeypatch.setattr(config, "STRATEGY_EPOCH", now - 10)
    con.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?)",
                (now, "o1", "c1", "T", "yes", "buy", 1, .40, .70, "filled"))
    db.record_order_evidence(
        con, client_order_id="c1", order_id="o1", ticker="T", city="BTC",
        settle_ts=now + 60, event_profit_model=.7, event_profit_market=.6,
        expected_pnl=.29, worst_loss=.41,
    )
    con.execute("INSERT INTO fills VALUES(?,?,?,?,?,?,?,?,?,?)",
                (now, "o1", "T", "yes", "buy", 1, .40, .01, .70, "desk-fill"))
    con.execute("INSERT INTO fills VALUES(?,?,?,?,?,?,?,?,?,?)",
                (now + 1, "manual", "T", "yes", "sell", 1, .80, .01, None,
                 "manual-fill"))
    con.execute("INSERT INTO settles VALUES(?,?,?,?,?)", (now + 60, "T", .7, 1, 99.0))
    con.commit()

    assert db.strategy_pnl(con, now - 1, now + 61) == pytest.approx(.59)
    evidence = profit_forecast.collect_evidence(con)
    assert evidence["event_metrics"] == [
        ("BTC", now + 60, .7, .6, .29, .41),
    ]
    report = profit_forecast.render(evidence)
    assert "profit p model/market 0.700/0.600" in report
    assert "expected P&L +0.2900 | worst loss 0.4100" in report


def test_fill_fair_backfill_requires_one_exact_order(tmp_path, monkeypatch):
    con = _db(tmp_path, monkeypatch)
    now = time.time()
    con.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?)",
                (now, "exact", "c1", "T1", "yes", "buy", 1, .4, .7, "filled"))
    for client in ("c2", "c3"):
        con.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (now, "ambiguous", client, "T2", "yes", "buy", 1, .4, .8, "filled"))
    con.execute("INSERT INTO fills VALUES(?,?,?,?,?,?,?,?,?,?)",
                (now, "exact", "T1", "yes", "buy", 1, .4, .01, None, "f1"))
    con.execute("INSERT INTO fills VALUES(?,?,?,?,?,?,?,?,?,?)",
                (now, "ambiguous", "T2", "yes", "buy", 1, .4, .01, None, "f2"))
    con.commit()

    assert db.backfill_fill_fairs(con) == 1
    assert con.execute("SELECT fair_at_entry FROM fills WHERE trade_id='f1'").fetchone()[0] == .7
    assert con.execute("SELECT fair_at_entry FROM fills WHERE trade_id='f2'").fetchone()[0] is None


def test_report_withholds_readiness_and_is_windows_safe(tmp_path, monkeypatch):
    con = _db(tmp_path, monkeypatch)
    evidence = profit_forecast.collect_evidence(con)
    report = profit_forecast.render(evidence)
    assert "INSUFFICIENT LIVE EVIDENCE" in report
    assert "independent" not in report.lower()
    assert "profit probability is estimated" in report
    report.encode("cp1252")
