"""Seasonal-time scaling: flat profile must equal calendar time exactly;
dead hours must collapse the distribution; drawdown auto-disarm must fire."""
import json
import math
import pathlib
import tempfile
import time

import numpy as np

import config
from engine.deribit import ExpirySlice
from engine.density import SettleDensity
from engine import vol_profile
from engine.vol_profile import seasonal_tau

F = 60_000.0


def make_slice(now, iv=0.5):
    strikes = list(np.arange(40_000, 90_001, 1_000, dtype=float))
    return ExpirySlice(expiry_ts=now + 86_400, forward=F,
                       strikes=strikes, ivs=[iv] * len(strikes))


def test_flat_profile_equals_calendar():
    now = time.time()
    flat = [1.0] * 24
    assert abs(seasonal_tau(flat, now, now + 5_000) - 5_000) < 1e-6
    sl = make_slice(now)
    a = SettleDensity(sl, now + 3600, now=now, index_price=F)
    b = SettleDensity(sl, now + 3600, now=now, index_price=F, profile=flat)
    K = np.arange(58_000, 62_000, 250.0)
    assert np.max(np.abs(a.survival(K) - b.survival(K))) < 1e-9


def test_dead_hours_collapse_tails():
    now = (time.time() // 3600) * 3600 + 10          # just past an hour boundary
    hour = int(now // 3600) % 24
    prof = [1.0] * 24
    prof[hour] = prof[(hour + 1) % 24] = 1e-6        # next two hours: dead
    sl = make_slice(now)
    d = SettleDensity(sl, now + 3600, now=now, index_price=F, profile=prof)
    # almost no variance can elapse before settle -> tails nearly impossible
    assert float(d.survival(F * 1.02)) < 0.001
    assert float(d.survival(F * 0.98)) > 0.999


def test_profile_validation_and_legacy_shrinkage():
    raw = [2.0] + [22 / 23] * 23
    shrunk = vol_profile._profile(raw, legacy=True)
    assert shrunk[0] == 1.5
    for bad in ([1.0] * 23, [1.0] * 23 + [0], [1.0] * 23 + [math.nan]):
        try:
            vol_profile._profile(bad)
            raise AssertionError("invalid profile accepted")
        except ValueError:
            pass


def test_fetch_filters_gaps_and_incomplete_candle(monkeypatch):
    hour = 3_600_000
    n = 24 * 32
    end = (n + 1) * hour
    ticks = [i * hour for i in range(n + 1)]
    closes = [math.exp(i * .001) for i in range(n + 1)]
    del ticks[100]
    del closes[100]
    ticks.append(end - hour // 2)       # current, incomplete candle
    closes.append(1e9)                  # would dominate if accidentally included

    class Response:
        @staticmethod
        def raise_for_status():
            pass

        @staticmethod
        def json():
            return {"result": {"ticks": ticks, "close": closes}}

    class Client:
        @staticmethod
        def get(*_args, **_kwargs):
            return Response()

    monkeypatch.setattr(vol_profile.time, "time", lambda: end / 1000)
    weights = vol_profile.fetch_weights("BTC", client=Client())
    assert max(abs(weight - 1.0) for weight in weights) < 1e-10


def test_cache_freshness_grace_and_invalid_asset_fail_closed(monkeypatch, tmp_path):
    now = 1_800_000_000.0
    cache = tmp_path / "vol_profile.json"
    monkeypatch.setattr(vol_profile, "PROFILE_FILE", cache)
    monkeypatch.setattr(vol_profile.time, "time", lambda: now)
    monkeypatch.setattr(vol_profile, "_last_fetch_fail", now)
    valid = [1.0] * 24

    cache.write_text(json.dumps({"version": 2, "computed_at": now - 25 * 3600,
                                 "weights": {"BTC": valid, "ETH": valid[:-1]}}))
    assert vol_profile.load_profiles() == {"BTC": valid}
    assert vol_profile.STATUS == "stale"

    cache.write_text(json.dumps({"version": 2, "computed_at": now - 49 * 3600,
                                 "weights": {"BTC": valid}}))
    assert vol_profile.load_profiles() == {}
    assert vol_profile.STATUS == "missing"


def test_drawdown_auto_disarm():
    from engine import rails
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="dd-dd-"))
    config.ARMED_FILE = tmp / "ARMED"
    config.DB_PATH = tmp / "t.db"
    import db
    con = db.connect()
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


if __name__ == "__main__":
    for fn in [test_flat_profile_equals_calendar, test_dead_hours_collapse_tails,
               test_drawdown_auto_disarm]:
        fn()
        print(f"PASS {fn.__name__}")
