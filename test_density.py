"""Density engine correctness: BL survival on a flat-vol chain must reproduce
Black-Scholes digitals, and all sanity checks must pass."""
import time

import numpy as np
import pytest
from scipy.stats import norm

from engine.checks import DensityError, sanity
from engine.deribit import ExpirySlice
from engine.density import YEAR_S, SettleDensity, fair_ladder

F, IV, T_DAYS = 60_000.0, 0.50, 1.0


def make_slice(now):
    expiry = now + T_DAYS * 86400
    strikes = list(np.arange(40_000, 90_001, 1_000, dtype=float))
    return ExpirySlice(expiry_ts=expiry, forward=F,
                       strikes=strikes, ivs=[IV] * len(strikes))


def bs_digital(K, T):
    w = IV * IV * T
    return norm.cdf((np.log(F / K) - w / 2) / np.sqrt(w))


def test_flat_vol_reproduces_bs_digitals():
    now = time.time()
    sl = make_slice(now)
    d = SettleDensity(sl, settle_ts=sl.expiry_ts, now=now, avg_window_s=0)
    K = np.arange(50_000, 72_000, 100.0)
    err = np.abs(d.survival(K) - bs_digital(K, T_DAYS * 86400 / YEAR_S))
    assert err.max() < 5e-4, f"max digital error {err.max():.2e}"
    sanity(d)


def test_settle_before_expiry_scales_variance():
    now = time.time()
    sl = make_slice(now)
    settle = now + 3600  # hourly settle off the daily chain
    d = SettleDensity(sl, settle_ts=settle, now=now, avg_window_s=0, index_price=F)
    err = np.abs(d.survival(np.arange(58_000, 62_000, 100.0))
                 - bs_digital(np.arange(58_000, 62_000, 100.0), 3600 / YEAR_S))
    assert err.max() < 5e-4, f"max hourly digital error {err.max():.2e}"
    sanity(d)


def test_averaging_haircut_tightens_distribution():
    now = time.time()
    sl = make_slice(now)
    settle = now + 3600
    d0 = SettleDensity(sl, settle_ts=settle, now=now, avg_window_s=0, index_price=F)
    d1 = SettleDensity(sl, settle_ts=settle, now=now, avg_window_s=60, index_price=F)
    up, down = F * 1.01, F * 0.99
    assert d1.survival(up) < d0.survival(up), "haircut must thin upper tail"
    assert d1.survival(down) > d0.survival(down), "haircut must thin lower tail"


def test_averaging_haircut_uses_hour_containing_final_minute():
    settle = ((int(time.time()) // 3600) + 2) * 3600
    now = settle - 3600
    sl = make_slice(now)
    sl.expiry_ts = settle
    settle_hour = int(settle // 3600) % 24
    profile_a = [1.0] * 24
    profile_b = profile_a.copy()
    profile_b[settle_hour] = 4.0
    d_a = SettleDensity(sl, settle_ts=settle, now=now, profile=profile_a)
    d_b = SettleDensity(sl, settle_ts=settle, now=now, profile=profile_b)
    assert np.array_equal(d_a._D, d_b._D)


def test_seasonal_profile_normalization_does_not_change_density():
    now = ((int(time.time()) // 3600) + 1) * 3600
    sl = make_slice(now)
    d_one = SettleDensity(sl, settle_ts=sl.expiry_ts, now=now,
                          profile=[1.0] * 24)
    d_two = SettleDensity(sl, settle_ts=sl.expiry_ts, now=now,
                          profile=[2.0] * 24)
    assert np.array_equal(d_one._D, d_two._D)


def test_hourly_settle_centers_on_interpolated_forward():
    """Regression (R1): with futures basis, an hourly settle must center near
    spot, not the daily-expiry forward."""
    now = time.time()
    index = F
    sl = make_slice(now)
    sl.forward = F * 1.002           # 20bp of daily basis
    settle = now + 3600
    d = SettleDensity(sl, settle_ts=settle, now=now, avg_window_s=0, index_price=index)
    assert abs(float(d.survival(index)) - 0.5) < 0.02, "not centered on settle forward"
    assert float(d.survival(sl.forward)) < 0.42, "still centered on expiry forward"
    assert abs(d.median() / (index * 1.002 ** (1 / 24)) - 1) < 2e-4
    # without index_price (daily settle == expiry) old behavior is correct
    d_exp = SettleDensity(sl, settle_ts=sl.expiry_ts, now=now, avg_window_s=0,
                          index_price=index)
    assert abs(float(d_exp.survival(sl.forward)) - 0.5) < 0.02


def test_skewed_smile_stays_sane():
    now = time.time()
    sl = make_slice(now)
    sl.ivs = [0.60 - 0.20 * (k - 40_000) / 50_000 for k in sl.strikes]  # put skew
    d = SettleDensity(sl, settle_ts=now + 6 * 3600, now=now, index_price=F)
    from engine.checks import sanity
    sanity(d)  # monotone survival, bounded, wings covered — despite the skew


def test_pathological_smile_repair_is_rejected():
    now = time.time()
    strikes = list(np.arange(40_000, 90_001, 5_000, dtype=float))
    ivs = [0.2] * len(strikes)
    ivs[5] = 1.5
    sl = ExpirySlice(expiry_ts=now + 86400, forward=F, strikes=strikes, ivs=ivs)
    d = SettleDensity(sl, settle_ts=sl.expiry_ts, now=now, avg_window_s=0)
    assert d.repair_max > 0.01
    with pytest.raises(DensityError, match="repair"):
        sanity(d)


def test_probability_bound_clipping_counts_as_repair(monkeypatch):
    now = time.time()
    sl = make_slice(now)
    monkeypatch.setattr(
        "engine.density.black_call",
        lambda _forward, strikes, _variance: 1.02 * (strikes[-1] - strikes),
    )
    d = SettleDensity(sl, settle_ts=sl.expiry_ts, now=now, avg_window_s=0)
    assert d.repair_max == pytest.approx(0.02)
    with pytest.raises(DensityError, match="repair"):
        sanity(d)


def test_fair_ladder_monotone():
    now = time.time()
    sl = make_slice(now)
    d = SettleDensity(sl, settle_ts=sl.expiry_ts, now=now)
    ladder = fair_ladder(d, [59_899.99, 59_999.99, 60_099.99])
    vals = list(ladder.values())
    assert vals == sorted(vals, reverse=True)
    assert all(0.0 <= v <= 1.0 for v in vals)


if __name__ == "__main__":
    for fn in [test_flat_vol_reproduces_bs_digitals, test_settle_before_expiry_scales_variance,
               test_averaging_haircut_tightens_distribution, test_fair_ladder_monotone]:
        fn()
        print(f"PASS {fn.__name__}")
