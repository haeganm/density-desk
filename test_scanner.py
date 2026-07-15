"""Scanner guard logic: catalyst threshold factors and the basis check."""
import json
import pathlib
import tempfile
import time

import config
import numpy as np
import pytest

_tmp = pathlib.Path(tempfile.mkdtemp(prefix="dd-scan-"))
config.ROOT = _tmp                                 # threshold_factor reads ROOT/state
(_tmp / "state").mkdir()

import engine.scanner as scanner  # noqa: E402
from engine import vol_profile  # noqa: E402
from engine.scanner import (  # noqa: E402
    ONE_SIDED,
    _basis_bps,
    catalyst_policy,
    _iqr,
    _liquid_ladder_quote,
    _surface_verdict,
    threshold_factor,
)

CATS = _tmp / "state" / "catalysts.json"


class FakeDensity:
    def __init__(self, med):
        self._m = med

    def median(self):
        return self._m


class FakeChain:
    index_price = 100.0

    @staticmethod
    def slice_for_settle(_settle):
        return object()


class FakeKalshi:
    def __init__(self, ask):
        self.ask = ask

    def markets(self, series):
        if series != "L":
            return []
        return [{
            "event_ticker": "L-EVENT",
            "ticker": "L-EVENT-T100",
            # 2h after the frozen `now` below: inside the catalyst calendar's
            # 48h horizon, so the scan is judged on edges, not on ignorance
            "close_time": "2027-01-15T10:00:00Z",
            "strike_type": "greater",
            "floor_strike": 100,
            "yes_bid_dollars": self.ask - 0.01,
            "yes_ask_dollars": self.ask,
        }]


def _threshold_scan(monkeypatch, price, fair, factor):
    now = 1_800_000_000.0
    CATS.write_text(json.dumps({"generated_ts": now, "windows": [{
        "start_ts": now - 1,
        "end_ts": now + 1,
        "factor": factor,
    }]}))

    class Density:
        def __init__(self, *_args, **_kwargs):
            pass

        @staticmethod
        def survival(_strike):
            return fair

        @staticmethod
        def median():
            return 100.0

    monkeypatch.setattr(config, "SERIES", {"BTC": {"ladder": "L", "range": "R"}})
    monkeypatch.setattr(scanner.time, "time", lambda: now)
    monkeypatch.setattr(scanner, "SettleDensity", Density)
    monkeypatch.setattr(scanner, "sanity", lambda _density: None)
    monkeypatch.setattr(scanner.fees, "taker_fee", lambda _price, _count: 0.0)
    monkeypatch.setattr(vol_profile, "load_profiles", lambda: {"BTC": [1.0] * 24})
    monkeypatch.setattr(scanner, "_surface_verdict", lambda *_args: (
        False, "ok", ((1.0, 1.0, ("L-EVENT-T100",)),)))
    return scanner.scan(FakeKalshi(price), {"BTC": FakeChain()})[0]


def test_overlap_takes_max_factor():
    """The live Jul-15 shape: routine event (3.0) overlapping a bigger one (3.5)."""
    now = time.time()
    CATS.write_text(json.dumps({"windows": [
        {"label": "BoC", "start_ts": now - 3600, "end_ts": now + 3600, "factor": 3.0},
        {"label": "Senate", "start_ts": now - 60, "end_ts": now + 7200, "factor": 3.5},
    ]}))
    assert threshold_factor(now) == 3.5
    assert threshold_factor(now + 7300) == 1.0


def test_catalyst_policy_uses_holding_interval_and_blocks_settlement():
    now = time.time()   # must be real: the calendar's horizon is real-clock based
    CATS.write_text(json.dumps({"windows": [
        {"start_ts": now + 100, "end_ts": now + 200, "factor": 2.0},
        {"start_ts": now + 300, "end_ts": now + 400, "factor": 3.5},
    ]}))
    assert catalyst_policy(now, now + 250) == (False, 2.0)
    assert catalyst_policy(now, now + 350) == (True, 3.5)


def test_string_timestamps_and_invalid_schedule_fail_closed():
    now = time.time()
    CATS.write_text(json.dumps({"windows": [
        {"label": "pasted", "start_ts": str(now - 60), "end_ts": str(now + 60),
         "factor": 2.0}]}))
    assert threshold_factor(now) == 2.0            # coerced, not crashed
    CATS.write_text("{ totally broken")
    assert threshold_factor(now) == 99.0
    assert catalyst_policy(now, now + 3600) == (True, 1.0)


def test_missing_or_stale_catalyst_schedule_fails_closed():
    now = time.time()
    CATS.unlink(missing_ok=True)
    assert catalyst_policy(now, now + 3600) == (True, 1.0)
    CATS.write_text(json.dumps({
        "generated_ts": now - scanner.CATALYST_MAX_AGE_S - 1,
        "windows": [],
    }))
    assert catalyst_policy(now, now + 3600) == (True, 1.0)


def test_model_fair_is_shrunk_toward_the_market_mid():
    from engine.scanner import _trusted_fair
    # 2026-07-15: fills realized -0.195 below predicted win rate. Where we disagree
    # with the mid, the mid carries information; only the surviving gap is edge.
    assert _trusted_fair(0.90, 0.50, "yes") == pytest.approx(0.7 * 0.90 + 0.3 * 0.50)
    # the no side blends against the no-mid (1 - mid), never the raw yes-mid
    assert _trusted_fair(0.90, 0.50, "no") == pytest.approx(0.7 * 0.90 + 0.3 * 0.50)
    assert _trusted_fair(0.80, 0.25, "no") == pytest.approx(0.7 * 0.80 + 0.3 * 0.75)
    # agreement shrinks to itself; no consensus -> the model stands alone
    assert _trusted_fair(0.42, 0.42, "yes") == pytest.approx(0.42)
    for junk in (None, 0.0, 1.0):
        assert _trusted_fair(0.42, junk, "yes") == 0.42
    # shrinkage always moves toward the mid, never past it
    assert 0.50 < _trusted_fair(0.90, 0.50, "yes") < 0.90


def test_strict_three_cent_and_catalyst_thresholds(monkeypatch):
    # fairs below are MODEL fairs; the scanner trades only the disagreement that
    # survives shrinkage to the mid (MODEL_TRUST). The fake book is bid=ask-0.01,
    # so mid=ask-0.005 and a raw 3c model edge is NOT a 3c tradable edge.
    assert config.MODEL_TRUST == 0.70            # the arithmetic below assumes it
    assert _threshold_scan(monkeypatch, 0.20, 0.24, 1.0) == []      # blended +2.7c
    accepted = _threshold_scan(monkeypatch, 0.20, 0.25, 1.0)        # blended +3.4c
    assert len(accepted) == 1
    assert (accepted[0].strike_type, accepted[0].floor_strike,
            accepted[0].model_version) == ("greater", 100.0, "r4")
    assert accepted[0].fair == pytest.approx(0.2335)   # shrunk, not the raw 0.25
    json.dumps(accepted[0].scenarios)       # prospective metadata is JSON-safe
    assert _threshold_scan(monkeypatch, 0.21, 0.29, 2.0) == []      # catalyst x2
    assert len(_threshold_scan(monkeypatch, 0.21, 0.31, 2.0)) == 1
    # the humility band rejects outright at BOTH ends, regardless of edge
    assert _threshold_scan(monkeypatch, 0.1499, 0.30, 1.0) == []
    # 0.86 with a huge model fair: upside is 14c, so any "edge" here is smaller
    # than our own model error -> never buy it (live cost: -$1.64 on 4 contracts)
    assert _threshold_scan(monkeypatch, 0.86, 0.99, 1.0) == []
    assert len(_threshold_scan(monkeypatch, 0.85, 0.95, 1.0)) == 1   # boundary is inclusive


def test_controlled_aggression_preserves_other_rails():
    assert config.TAKER_EDGE_MIN == 0.03
    assert config.PER_EVENT_CAP == 5.00
    assert config.SHAKEOUT is False
    assert (config.PER_TRADE_CAP, config.MAX_OPEN_EXPOSURE) == (2.00, 12.00)
    assert (config.KELLY_FRACTION, config.DAILY_LOSS_LIMIT) == (0.25, 5.00)
    assert (config.MIN_BUY_PRICE, config.MAX_BUY_PRICE) == (0.15, 0.85)
    assert config.MAX_FAIR_RATIO == 2.0
    assert config.BASIS_ALARM_BPS == 10
    assert scanner.STAND_DOWN_FINAL_S == 45 * 60


def test_basis_crossing_interpolation():
    d = FakeDensity(60_625.0)
    mids = [(60_000, 0.80), (60_500, 0.55), (60_750, 0.45)]  # crossing at 60,625
    assert abs(_basis_bps(d, mids)) < 0.5          # model agrees -> ~0 bps
    assert _basis_bps(FakeDensity(60_700.0), mids) > 10  # model 75 above -> >10bps


def test_ladder_distribution_rejects_crossed_or_sizeless_quotes():
    quote = {"yes_bid_size_fp": "5.00", "yes_ask_size_fp": "5.00"}
    assert _liquid_ladder_quote(quote, .48, .52)
    assert not _liquid_ladder_quote(quote, .53, .52)
    assert not _liquid_ladder_quote({**quote, "yes_ask_size_fp": "0"}, .48, .52)


def test_basis_one_sided_and_thin():
    d = FakeDensity(60_000.0)
    assert _basis_bps(d, [(61_000, 0.40), (61_250, 0.30)]) == ONE_SIDED
    assert _basis_bps(d, [(61_000, 0.40)]) is None  # single quote: can't judge


class SurfaceDensity:
    def __init__(self, lower=75.0, upper=125.0):
        self.K_grid = np.array([50.0, lower, 100.0, upper, 150.0])
        self._D = np.array([0.9, 0.75, 0.5, 0.25, 0.1])

    def median(self):
        return 100.0

    def survival(self, strike):
        return np.interp(strike, self.K_grid, self._D)


def test_iqr_width_guard_blocks_1_9x_surface_and_accepts_bounds():
    market = [(50, .9), (75, .75), (100, .5), (125, .25), (150, .1)]
    assert _iqr(market) == 50
    bad, detail, _ = _surface_verdict(SurfaceDensity(52.5, 147.5), market, [])
    assert bad and "1.90" in detail
    assert not _surface_verdict(SurfaceDensity(62.5, 137.5), market, [
        {"ticker": "G", "strike_type": "greater", "floor_strike": 100},
    ])[0]
    lower_market = [(50, .9), (70, .75), (100, .5), (130, .25), (150, .1)]
    contract = [{"ticker": "G", "strike_type": "greater", "floor_strike": 100}]
    assert not _surface_verdict(SurfaceDensity(80, 120), lower_market, contract)[0]
    assert _surface_verdict(SurfaceDensity(81, 119), lower_market, contract)[0]


def test_non_monotone_ladder_fails_closed():
    market = [(50, .9), (75, .7), (100, .71), (125, .25), (150, .1)]
    assert _surface_verdict(SurfaceDensity(), market, [])[0]


def test_surface_scenarios_are_exhaustive_and_shared_contract_truth():
    market = [(50, .9), (75, .75), (100, .5), (125, .25), (150, .1)]
    markets = [
        {"ticker": "G75", "strike_type": "greater", "floor_strike": 75},
        {"ticker": "G100", "strike_type": "greater", "floor_strike": 100},
        {"ticker": "B75", "strike_type": "between", "floor_strike": 75,
         "cap_strike": 100},
    ]
    bad, _, scenarios = _surface_verdict(SurfaceDensity(), market, markets)
    assert not bad
    assert sum(row[0] for row in scenarios) == 1
    assert sum(row[1] for row in scenarios) == 1
    assert any(set(winners) == {"G75", "B75"} for _, _, winners in scenarios)


def test_ladder_without_usable_quotes_blocks_costelling_range(monkeypatch):
    now = 1_800_000_000.0
    close = "2030-01-01T00:00:00Z"

    class Markets:
        def markets(self, series):
            if series == "L":
                return [{"event_ticker": "L-E", "ticker": "L-E-T", "close_time": close,
                         "strike_type": "greater", "floor_strike": 100,
                         "yes_bid_dollars": 0, "yes_ask_dollars": 1}]
            return [{"event_ticker": "R-E", "ticker": "R-E-B", "close_time": close,
                     "strike_type": "between", "floor_strike": 90, "cap_strike": 110,
                     "yes_bid_dollars": .10, "yes_ask_dollars": .20}]

    class Density:
        K_grid = np.array([50, 100, 150])
        _D = np.array([.9, .5, .1])

        def __init__(self, *_args, **_kwargs):
            pass

        @staticmethod
        def survival(_strike):
            return .5

        @staticmethod
        def bucket(_floor, _cap):
            return .35

        @staticmethod
        def median():
            return 100

    monkeypatch.setattr(config, "SERIES", {"BTC": {"ladder": "L", "range": "R"}})
    monkeypatch.setattr(scanner.time, "time", lambda: now)
    monkeypatch.setattr(scanner, "SettleDensity", Density)
    monkeypatch.setattr(scanner, "sanity", lambda _density: None)
    monkeypatch.setattr(scanner.fees, "taker_fee", lambda *_args: 0.0)
    monkeypatch.setattr(vol_profile, "load_profiles", lambda: {"BTC": [1.0] * 24})
    assert scanner.scan(Markets(), {"BTC": FakeChain()})[0] == []


if __name__ == "__main__":
    for fn in [test_overlap_takes_max_factor,
               test_string_timestamps_and_invalid_schedule_fail_closed,
               test_basis_crossing_interpolation, test_basis_one_sided_and_thin,
               test_iqr_width_guard_blocks_1_9x_surface_and_accepts_bounds,
               test_surface_scenarios_are_exhaustive_and_shared_contract_truth]:
        fn()
        print(f"PASS {fn.__name__}")
