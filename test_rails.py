"""Rails must block: KILL file, oversized orders, exposure breach, daily loss.
Isolated from the live desk: temp KILL/HALT/DB so running tests never halts
a production cycle or pollutes the journal."""
import pathlib
import tempfile
import time

import config

_tmp = pathlib.Path(tempfile.mkdtemp(prefix="dd-test-"))
config.KILL_FILE = _tmp / "KILL"
config.DB_PATH = _tmp / "test.db"

import db  # noqa: E402 - config must point at the isolated test DB before import
from engine import rails  # noqa: E402

rails.HALT_FILE = _tmp / "state" / "HALT_UNTIL"


def test_kill_file_halts():
    con = db.connect()
    config.KILL_FILE.write_text("")
    try:
        try:
            rails.check_global(con, 0, 0)
            assert False, "KILL file did not halt"
        except rails.RailsHalt:
            pass
    finally:
        config.KILL_FILE.unlink()


def test_per_trade_cap():
    try:
        rails.check_order(cost=rails.per_trade_cap() + 0.01, open_exposure=0)
        assert False, "cap not enforced"
    except rails.RailsHalt:
        pass
    rails.check_order(cost=rails.per_trade_cap() - 0.01, open_exposure=0)


def test_exposure_cap():
    try:
        rails.check_order(cost=1.0, open_exposure=config.MAX_OPEN_EXPOSURE)
        assert False, "exposure cap not enforced"
    except rails.RailsHalt:
        pass


def test_daily_loss_halts_and_writes_halt_file():
    con = db.connect()
    # a big loss on a ticker the desk NEVER traded must not trip the rail
    # (2026-07-14 regression: manual WTI settle masked desk losses)
    con.execute("INSERT INTO settles VALUES(?,?,?,?,?)",
                (time.time(), "FOREIGN-TICKER", 0.5, 0, -(config.DAILY_LOSS_LIMIT + 5)))
    con.commit()
    rails.check_global(con, 0, 0)          # no halt: not desk-traded
    # same loss on a desk-traded ticker must halt
    con.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?)",
                (time.time(), "o1", "c1", "TEST-TICKER", "yes", "buy", 1, 0.5, 0.6, "x"))
    con.execute("INSERT INTO settles VALUES(?,?,?,?,?)",
                (time.time(), "TEST-TICKER", 0.5, 0, -(config.DAILY_LOSS_LIMIT + 5)))
    con.commit()
    try:
        try:
            rails.check_global(con, 0, 0)
            assert False, "daily loss did not halt"
        except rails.RailsHalt:
            pass
        assert rails.HALT_FILE.exists(), "HALT_UNTIL not written"
        # and the halt persists on the next check
        try:
            rails.check_global(con, 0, 0)
            assert False, "halt not sticky"
        except rails.RailsHalt:
            pass
    finally:
        rails.HALT_FILE.unlink(missing_ok=True)
        con.execute("DELETE FROM settles")
        con.execute("DELETE FROM orders")
        con.commit()


def test_epoch_bump_does_not_forgive_todays_desk_losses():
    # 2026-07-14 regression: rail read +0.50 on a -9.88 day because every
    # losing order predated a same-day STRATEGY_EPOCH bump
    con = db.connect()
    con.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?)",
                (config.STRATEGY_EPOCH - 3600, "o-old", "c-old", "PRE-EPOCH-TICKER",
                 "yes", "buy", 1, 0.5, 0.6, "x"))
    con.execute("INSERT INTO settles VALUES(?,?,?,?,?)",
                (time.time(), "PRE-EPOCH-TICKER", 0.5, 0,
                 -(config.DAILY_LOSS_LIMIT + 5)))
    con.commit()
    try:
        try:
            rails.check_global(con, 0, 0)
            assert False, "pre-epoch desk losses were forgiven"
        except rails.RailsHalt:
            pass
    finally:
        rails.HALT_FILE.unlink(missing_ok=True)
        con.execute("DELETE FROM settles")
        con.execute("DELETE FROM orders")
        con.commit()


def test_strategy_pnl_counts_no_side_entry_fills_journaled_as_sell():
    # Kalshi reports a buy-no entry fill as action='sell'; the exact branch
    # must match on side, not action, or NO-side losses vanish from the rail
    con = db.connect()
    now = time.time()
    con.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?)",
                (now, "o-no", "c-no", "NO-TICKER", "no", "buy", 2, 0.85, 0.90,
                 "ioc filled 2/2"))
    db.record_order_evidence(
        con, client_order_id="c-no", ticker="NO-TICKER", asset="BTC",
        settle_ts=now + 3600, event_profit_model=.7, event_profit_market=.6,
        expected_pnl=.1, worst_loss=1.71, model_version="test")
    db.link_order_evidence(con, "c-no", "o-no")
    con.execute("INSERT INTO fills VALUES(?,?,?,?,?,?,?,?,?,?)",
                (now, "o-no", "NO-TICKER", "no", "sell", 2, 0.85, 0.01, 0.90, "t-no"))
    con.execute("INSERT INTO settles VALUES(?,?,?,?,?)",
                (now, "NO-TICKER", 0.9, 1, 0.0))   # yes won -> the no entry lost
    con.commit()
    try:
        pnl = db.strategy_pnl(con, now - 60)
        assert abs(pnl - (-1.71)) < 1e-9, pnl     # (0 - .85)*2 - .01
    finally:
        for t in ("settles", "fills", "orders", "order_evidence"):
            con.execute(f"DELETE FROM {t}")
        con.commit()


def test_drawdown_auto_disarm():
    # rescued from test_vol_profile.py during the weather conversion: it tests
    # rails.drawdown_check and was misfiled, so deleting the crypto vol-profile
    # module would have silently taken the auto-disarm rail's only test with it.
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="dd-dd-"))
    armed_before = config.ARMED_FILE
    config.ARMED_FILE = tmp / "ARMED"
    con = db.connect()
    try:
        config.ARMED_FILE.write_text("10.00")
        rails.drawdown_check(con, 9.0)                   # above 65% -> fine
        assert config.ARMED_FILE.exists()
        try:
            rails.drawdown_check(con, 6.0)               # below 65% of 10 -> disarm
            raise AssertionError("drawdown did not halt")
        except rails.RailsHalt:
            pass
        assert not config.ARMED_FILE.exists(), "ARMED file not removed"
        assert con.execute("SELECT COUNT(*) FROM events WHERE kind='auto_disarm'"
                           ).fetchone()[0] == 1
        # empty ARMED file gets backfilled with current equity
        config.ARMED_FILE.write_text("")
        rails.drawdown_check(con, 12.34)
        assert config.ARMED_FILE.read_text() == "12.34"
    finally:
        config.ARMED_FILE = armed_before
        con.execute("DELETE FROM events")
        con.commit()


if __name__ == "__main__":
    for fn in [test_kill_file_halts, test_per_trade_cap, test_exposure_cap,
               test_daily_loss_halts_and_writes_halt_file,
               test_epoch_bump_does_not_forgive_todays_desk_losses,
               test_strategy_pnl_counts_no_side_entry_fills_journaled_as_sell,
               test_drawdown_auto_disarm]:
        fn()
        print(f"PASS {fn.__name__}")
