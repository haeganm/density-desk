"""The weather model: calibration, discrete-integer semantics, partition identity."""
import datetime as dt
import math

import pytest

from engine import weather
from engine.weather import TempDistribution, WeatherError, seasonal_params

# A fitted-shaped calibration. Real KNYC lead-1d numbers (2023-2026, n=1076).
CAL = {"by_month": {str(m): {"bias": b, "sd": s, "n": 150} for m, b, s in [
    (1, -1.85, 2.75), (2, -1.89, 3.38), (3, -2.72, 3.88), (4, -2.72, 3.64),
    (5, -0.58, 3.31), (6, 1.92, 2.86), (7, 2.42, 3.06), (8, 1.38, 2.48),
    (9, 0.97, 2.46), (10, 0.41, 2.12), (11, -1.79, 2.55), (12, -2.05, 2.43),
]}}


def _members(centre, sd=1.6, n=61):
    """Deterministic pseudo-ensemble, symmetric about `centre` so the pooled mean
    is exactly `centre` (an off-by-half-a-step helper makes every assertion lie)."""
    step = sd * 2 / n
    return [centre + (i - (n - 1) / 2) * step for i in range(n)]


def test_bias_is_seasonal_not_annual():
    # THE trap this model exists to avoid: KNYC bias runs Mar -2.72F to Jul +2.42F
    # (5.1F = 2.5 buckets). The annual mean (-0.51F) is ~2.4F wrong every summer
    # day -- more than a whole bucket -- while annual PIT coverage still looks fine
    # because the seasons cancel.
    jul, _ = seasonal_params(CAL, dt.date(2026, 7, 15))
    mar, _ = seasonal_params(CAL, dt.date(2026, 3, 15))
    assert jul == pytest.approx(2.42, abs=0.01)
    assert mar == pytest.approx(-2.72, abs=0.01)
    assert jul - mar > 5.0
    # interpolated between month centres, never a jump at the boundary
    mid, _ = seasonal_params(CAL, dt.date(2026, 6, 30))
    assert 1.92 < mid < 2.42
    # and it wraps the year end without exploding
    assert seasonal_params(CAL, dt.date(2026, 1, 1))[0] == pytest.approx(-1.95, abs=0.15)


def test_survival_is_reported_integer_space_with_continuity_correction():
    d = TempDistribution.from_members(_members(90.0), bias=0.0, sd=3.0, fetched_ts=0.0)
    # P(reported > 90) = P(latent > 90.5), NOT P(latent > 90)
    assert d.survival(90) == pytest.approx(1 - 0.5 * (1 + math.erf(0.5 / 3.0 / math.sqrt(2))))
    assert d.survival(200) < 1e-6 and d.survival(-50) > 1 - 1e-6
    # monotone decreasing, always in [0,1]
    vals = [d.survival(k) for k in range(70, 110)]
    assert all(a >= b for a, b in zip(vals, vals[1:]))
    assert all(0 <= v <= 1 for v in vals)
    assert d.repair_max == 0.0          # an ECDF/normal CDF needs no repair


def test_bias_shifts_the_center_by_a_full_bucket():
    # Summer bias is ~+2.4F, wider than a whole 2F bucket, so correcting it is not
    # a refinement -- it relocates the distribution. Raw GFS said 99.0F for NY on
    # 2026-07-15; corrected, 96.6F.
    hot = TempDistribution.from_members(_members(99.0), bias=2.39, sd=3.0, fetched_ts=0.0)
    raw = TempDistribution.from_members(_members(99.0), bias=0.0, sd=3.0, fetched_ts=0.0)
    assert hot.median() == pytest.approx(96.61)
    assert raw.median() == pytest.approx(99.0)
    # and that shift swings P(>=97) by ~28 points -- the difference between
    # "very likely" and "a coin flip" on a leg we would be pricing.
    assert raw.survival(96) == pytest.approx(0.798, abs=0.01)
    assert hot.survival(96) == pytest.approx(0.515, abs=0.01)
    assert raw.survival(96) - hot.survival(96) > 0.25


def test_ensemble_may_widen_but_never_narrow():
    tight = TempDistribution.from_members(_members(90.0, sd=0.4), bias=0.0, sd=3.0,
                             fetched_ts=0.0, typical_spread=1.6)
    assert tight.sd == pytest.approx(3.0)      # narrow spread buys NO confidence
    assert tight.spread_widen == 1.0
    wide = TempDistribution.from_members(_members(90.0, sd=4.8), bias=0.0, sd=3.0,
                            fetched_ts=0.0, typical_spread=1.6)
    assert wide.sd > 3.0                       # a genuinely uncertain day widens us
    assert wide.spread_widen > 1.0


def test_from_point_is_the_traded_center_and_matches_from_members():
    # PRODUCTION CENTERS ON from_point. The calibration was fitted against the
    # DETERMINISTIC forecast (previous-runs archives no ensemble), so centering on
    # the pooled ensemble mean would wear a bias correction never measured for it
    # — and at ±2.4°F seasonal, a 1°F estimator mismatch is half a bucket.
    # from_point is also the only center the backtest can reconstruct, so the
    # backtest validates exactly what we trade.
    point = TempDistribution.from_point(99.0, bias=2.39, sd=3.0)
    assert point.median() == pytest.approx(96.61)
    # identical distribution to from_members when the pooled mean equals the point
    pooled = TempDistribution.from_members(_members(99.0), bias=2.39, sd=3.0,
                                           fetched_ts=0.0)
    assert point.median() == pytest.approx(pooled.median())
    assert point.sd == pytest.approx(pooled.sd)
    assert point.survival(96) == pytest.approx(pooled.survival(96))
    # the ensemble's ONLY job is widening, and it can never narrow us
    assert weather.pooled_widen(_members(90.0, sd=0.4), 1.6) == 1.0
    assert weather.pooled_widen(_members(90.0, sd=4.8), 1.6) > 1.0
    assert weather.pooled_widen([], 1.6) == 1.0            # dead ensemble -> no widen
    assert weather.pooled_widen(_members(90.0), None) == 1.0
    widened = TempDistribution.from_point(99.0, bias=2.39, sd=3.0, spread_widen=1.5)
    assert widened.sd == pytest.approx(4.5) and widened.median() == pytest.approx(96.61)


def test_degraded_inputs_fail_closed():
    with pytest.raises(WeatherError):
        TempDistribution.from_point(float("nan"), bias=0, sd=3)
    with pytest.raises(WeatherError):
        TempDistribution.from_point(90.0, bias=float("nan"), sd=3)
    with pytest.raises(WeatherError):
        TempDistribution.from_point(90.0, bias=0, sd=0)
    with pytest.raises(WeatherError):
        TempDistribution.from_members(_members(90.0, n=10), bias=0, sd=3, fetched_ts=0)   # thin pool
    with pytest.raises(WeatherError):
        TempDistribution.from_members(_members(90.0), bias=0, sd=0, fetched_ts=0)         # sd<=0
    with pytest.raises(WeatherError):
        TempDistribution.from_members(_members(90.0), bias=float("nan"), sd=3, fetched_ts=0)


# The live KXHIGHNY-26JUL16 partition, verbatim from the API 2026-07-15.
PARTITION = [
    {"ticker": "T89", "strike_type": "less", "cap_strike": 89},        # "88 or below"
    {"ticker": "B89.5", "strike_type": "between", "floor_strike": 89, "cap_strike": 90},
    {"ticker": "B91.5", "strike_type": "between", "floor_strike": 91, "cap_strike": 92},
    {"ticker": "B93.5", "strike_type": "between", "floor_strike": 93, "cap_strike": 94},
    {"ticker": "B95.5", "strike_type": "between", "floor_strike": 95, "cap_strike": 96},
    {"ticker": "T96", "strike_type": "greater", "floor_strike": 96},   # "97 or above"
]


def test_kalshi_weather_semantics_differ_from_crypto():
    # Inheriting the crypto conventions would misprice every `less` leg by a full
    # degree and drop the bottom integer of every bracket.
    less, between, greater = PARTITION[0], PARTITION[1], PARTITION[5]
    assert weather._wins(less, 88) and not weather._wins(less, 89)      # NOT x <= cap
    assert weather._wins(between, 89) and weather._wins(between, 90)    # inclusive BOTH ends
    assert not weather._wins(between, 88) and not weather._wins(between, 91)
    assert weather._wins(greater, 97) and not weather._wins(greater, 96)


def test_partition_probabilities_sum_to_exactly_one():
    # This identity is the cross-contract consistency guard: exactly one leg pays,
    # so if the model legs do not sum to 1 either our pricing is inconsistent or
    # Kalshi's partition has a gap -- and either way we must not quote.
    for centre in (85.0, 92.3, 99.0):
        d = TempDistribution.from_members(_members(centre), bias=0.0, sd=3.4, fetched_ts=0.0)
        total = sum(weather.fair(d, m) for m in PARTITION)
        assert total == pytest.approx(1.0, abs=1e-9), (centre, total)


def test_every_reported_integer_is_won_by_exactly_one_leg():
    for t in range(70, 115):
        assert sum(1 for m in PARTITION if weather._wins(m, t)) == 1, t


def test_fair_matches_wins_by_monte_carlo():
    """fair() must be the probability of the event _wins() describes."""
    d = TempDistribution.from_members(_members(92.0), bias=0.0, sd=3.4, fetched_ts=0.0)
    for m in PARTITION:
        # integrate _wins over the reported-integer distribution
        p = sum((d.survival(t - 1) - d.survival(t)) for t in range(60, 130)
                if weather._wins(m, t))
        assert weather.fair(d, m) == pytest.approx(p, abs=1e-6), m["ticker"]
