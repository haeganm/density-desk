"""Scanner: shrinkage, maker candidates, partition scenarios, D+0 exclusion."""
import datetime as dt
import pathlib
import tempfile

import pytest

import config

config.ROOT = pathlib.Path(tempfile.mkdtemp(prefix="dd-scan-"))
(config.ROOT / "state").mkdir()

import engine.scanner as scanner  # noqa: E402
from engine.weather import TempDistribution  # noqa: E402
from test_weather import PARTITION, _members  # noqa: E402


def _quoted(mids):
    """The live KXHIGHNY partition with a two-sided book at each given mid."""
    out = []
    for m, mid in zip(PARTITION, mids):
        q = dict(m)
        q["yes_bid_dollars"] = f"{max(0.01, mid - 0.01):.2f}"
        q["yes_ask_dollars"] = f"{min(0.99, mid + 0.01):.2f}"
        q["yes_bid_size_fp"] = "50.00"
        q["yes_ask_size_fp"] = "50.00"
        out.append(q)
    return out


def test_event_date_parses_the_local_calendar_day():
    assert scanner.event_date("KXHIGHNY-26JUL16") == dt.date(2026, 7, 16)
    assert scanner.event_date("KXHIGHNY-26JAN01") == dt.date(2026, 1, 1)
    for junk in ("", None, "KXHIGHNY", "KXHIGHNY-26XXX16", "KXHIGHNY-26JUL99"):
        assert scanner.event_date(junk) is None


def test_model_fair_is_shrunk_toward_the_market_mid():
    # The mid is the crowd's estimate; ours is a model's. We only trade the part
    # of the disagreement that survives shrinkage.
    assert config.MODEL_TRUST == 0.70
    f = scanner._trusted_fair
    assert f(0.90, 0.50, "yes") == pytest.approx(0.7 * 0.90 + 0.3 * 0.50)
    assert f(0.80, 0.25, "no") == pytest.approx(0.7 * 0.80 + 0.3 * 0.75)  # no-mid!
    assert f(0.42, 0.42, "yes") == pytest.approx(0.42)
    for junk in (None, 0.0, 1.0):
        assert f(0.42, junk, "yes") == 0.42          # no consensus -> model alone
    assert 0.50 < f(0.90, 0.50, "yes") < 0.90        # moves toward, never past


def test_maker_candidate_needs_room_inside_the_spread():
    mk = scanner._maker_candidate
    px, edge = mk("yes", 0.30, 0.40, 0.38)
    assert px == 0.31 and edge == pytest.approx(0.07)   # maker fills are free
    px, edge = mk("no", 0.30, 0.40, 0.68)
    assert px == 0.61 and edge == pytest.approx(0.07)
    assert mk("yes", 0.40, 0.41, 0.60) is None         # 1c spread: no room
    assert mk("yes", 0.0, 0.40, 0.38) is None          # one-sided book


def test_liquid_quote_never_reads_liquidity_dollars():
    # liquidity_dollars is "0.0000" on EVERY weather market; gating on it would
    # block every trade forever.
    m = {"yes_bid_size_fp": "50.00", "yes_ask_size_fp": "50.00",
         "liquidity_dollars": "0.0000"}
    assert scanner._liquid_quote(m, 0.30, 0.32)
    assert not scanner._liquid_quote(m, 0.0, 0.32)        # one-sided
    assert not scanner._liquid_quote(m, 0.30, 0.60)       # 30c spread
    thin = {"yes_bid_size_fp": "0", "yes_ask_size_fp": "50.00"}
    assert not scanner._liquid_quote(thin, 0.30, 0.32)


def test_scenarios_are_one_per_leg_and_fail_closed_on_a_broken_partition():
    d = TempDistribution(_members(92.0), bias=0.0, sd=3.4, fetched_ts=0.0)
    markets = _quoted([0.22, 0.35, 0.24, 0.13, 0.04, 0.02])
    sc = scanner._scenarios(d, markets)
    assert sc is not None and len(sc) == len(PARTITION)
    assert sum(s[0] for s in sc) == pytest.approx(1.0)   # model probabilities
    assert sum(s[1] for s in sc) == pytest.approx(1.0)   # market probabilities
    assert all(len(s[2]) == 1 for s in sc)               # exactly one winner per leg

    # a gap in the partition must fail closed: drop the 91-92 bucket and the model
    # legs no longer sum to 1, which is the cross-contract consistency guard
    holed = [m for m in markets if m["ticker"] != "B91.5"]
    assert scanner._scenarios(d, holed) is None
    # an unquoted leg is also fatal -- we cannot value a book we cannot price
    unquoted = [dict(m) for m in markets]
    unquoted[2]["yes_bid_dollars"] = "0"
    assert scanner._scenarios(d, unquoted) is None


def test_surface_verdict_compares_centers_in_degrees_not_bps():
    # The crypto relative-bps test would be 0.09F at 90F -- absurd for weather.
    assert config.BASIS_ALARM_F == 3.0
    markets = _quoted([0.22, 0.35, 0.24, 0.13, 0.04, 0.02])
    agree = TempDistribution(_members(89.8), bias=0.0, sd=3.4, fetched_ts=0.0)
    bad, detail, sc = scanner._surface_verdict(agree, markets)
    assert not bad and sc, detail
    # a model 10F above the market's center is a blown model, not an edge
    wild = TempDistribution(_members(101.0), bias=0.0, sd=3.4, fetched_ts=0.0)
    bad, detail, sc = scanner._surface_verdict(wild, markets)
    assert bad and "center" in detail and sc == ()


def test_market_points_use_reported_upper_edges():
    # 'less' cap=89 pays on <=88, so its upper edge is 88 -- not 89.
    legs = [(m, p) for m, p in zip(PARTITION, [0.2, 0.4, 0.2, 0.1, 0.05, 0.05])]
    pts = scanner._market_points(legs)
    assert [k for k, _ in pts] == [88.0, 90.0, 92.0, 94.0, 96.0]
    assert pts[0][1] == pytest.approx(0.8)     # P(>88) = 1 - 0.2
    assert pts[-1][1] == pytest.approx(0.05)   # P(>96) = the open top leg
    assert all(a[1] >= b[1] for a, b in zip(pts, pts[1:]))   # monotone


def test_cadence_and_rails_are_reset_for_a_daily_market():
    # Temperature does not tick: a 5-minute scan is already ~70x faster than the
    # ~6h ensemble refresh. This slow clock IS the structural thesis.
    assert config.REFRESH_S == 300
    assert scanner.STAND_DOWN_FINAL_S == 30 * 60
    assert config.MODEL_VERSION == "w1"
    assert set(config.CITIES) == {"NYC"}
    assert config.CITIES["NYC"]["series"] == "KXHIGHNY"
    # unchanged, and re-verified for weather: $0 maker fee, same taker formula
    assert (config.TAKER_FEE_MULT, config.MAKER_FEE_FRACTION) == (0.07, 0.0)
    assert (config.PER_TRADE_CAP, config.DAILY_LOSS_LIMIT) == (2.00, 5.00)
