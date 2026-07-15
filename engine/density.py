"""Options-implied settle distribution.

Method: fit total implied variance w(k) = iv^2 * T in log-moneyness k = ln(K/F)
with a shape-preserving PCHIP spline (flat wings), scale total variance linearly
in time to the Kalshi settle timestamp, subtract the settlement-averaging
variance haircut, price a dense undiscounted call grid, and read the survival
function P(settle > K) = -dC/dK numerically. No lognormal assumption beyond the
smile itself; skew/fat tails come straight from Deribit marks.
"""
from __future__ import annotations
import time

import numpy as np
from scipy.interpolate import PchipInterpolator
from scipy.stats import norm

import config
from engine.deribit import ExpirySlice

YEAR_S = 365.0 * 24 * 3600
GRID_N = 4001
GRID_PAD_SD = 6.0        # grid spans ±6 sd of ATM vol around the forward
MIN_W = 1e-10


def black_call(F: float, K: np.ndarray, w: np.ndarray) -> np.ndarray:
    """Undiscounted Black call; w = total variance per strike."""
    w = np.maximum(w, MIN_W)
    sq = np.sqrt(w)
    d1 = (np.log(F / K) + w / 2) / sq
    return F * norm.cdf(d1) - K * norm.cdf(d1 - sq)


class SettleDensity:
    """Implied distribution of the settle price at settle_ts, from one Deribit expiry slice."""

    def __init__(self, sl: ExpirySlice, settle_ts: float, now: float | None = None,
                 avg_window_s: float = config.SETTLEMENT_AVG_WINDOW_S,
                 index_price: float | None = None,
                 profile: list[float] | None = None):
        now = now or time.time()
        T_exp = (sl.expiry_ts - now) / YEAR_S
        T_set = (settle_ts - now) / YEAR_S
        if T_exp <= 0 or T_set <= 0:
            raise ValueError("expiry/settle in the past")
        if T_set > T_exp + 1e-12:
            raise ValueError("settle after slice expiry — pick a later slice")
        # Forward to the SETTLE time, not the slice expiry: geometric interpolation
        # of the futures basis between spot index and the expiry forward. Without
        # this, an hourly settle inherits up to 24h of basis (~cents of fake edge) —
        # so a sub-expiry settle REQUIRES the index; silent fallback would quietly
        # reintroduce the bug for any caller that forgets the kwarg.
        if index_price:
            F_set = index_price * (sl.forward / index_price) ** (T_set / T_exp)
        elif T_set < T_exp - 1e-9:
            raise ValueError("index_price required when settle is before slice expiry")
        else:
            F_set = sl.forward
        self.settle_ts, self.forward = settle_ts, F_set
        self.T_set = T_set

        ks = np.log(np.asarray(sl.strikes) / sl.forward)
        w_exp = (np.asarray(sl.ivs) ** 2) * T_exp
        order = np.argsort(ks)
        ks, w_exp = ks[order], w_exp[order]
        if len(ks) < 4:
            raise ValueError(f"only {len(ks)} usable strikes")
        spline = PchipInterpolator(ks, w_exp, extrapolate=False)
        k_lo, k_hi, w_lo, w_hi = ks[0], ks[-1], w_exp[0], w_exp[-1]

        # variance haircut: settle = avg of final avg_window_s -> Var = sigma^2*(T - 2W/3)
        i_atm = int(np.argmin(np.abs(ks)))
        # the averaging window sits at the settle hour: weight its variance rate
        # by that hour's seasonal weight, matching the scaled total variance
        # The settlement average ends at settle_ts, so an on-the-hour settle's
        # final minute belongs to the preceding UTC hour, not the next one.
        avg_midpoint = settle_ts - avg_window_s / 2
        w_hour = profile[int(avg_midpoint // 3600) % 24] if profile else 1.0
        # variance to the settle: seasonal vol-time when a profile exists (quiet
        # overnight hours realize far less variance than the daily average —
        # live-fire lesson 2026-07-14), calendar time otherwise
        if profile:
            from engine.vol_profile import seasonal_tau
            tau_exp = max(seasonal_tau(profile, now, sl.expiry_ts), 1e-12)
            scale = seasonal_tau(profile, now, settle_ts) / tau_exp
            # w_exp is the full-expiry variance; divide by the same seasonal
            # clock used for scaling before applying the final-minute weight.
            haircut = (w_exp[i_atm] / tau_exp) * w_hour * (2.0 / 3.0) * avg_window_s
        else:
            scale = T_set / T_exp
            var_rate_atm = w_exp[i_atm] / T_exp                   # per year
            haircut = var_rate_atm * (2.0 / 3.0) * (avg_window_s / YEAR_S)

        def w_settle(k: np.ndarray) -> np.ndarray:
            w = np.where(k < k_lo, w_lo, np.where(k > k_hi, w_hi, np.nan))
            inner = np.isnan(w)
            w[inner] = spline(k[inner])
            return np.maximum(w * scale - haircut, MIN_W)

        sd_atm = np.sqrt(max(w_exp[i_atm] * scale, MIN_W))
        k_grid = np.linspace(min(k_lo, -GRID_PAD_SD * sd_atm),
                             max(k_hi, GRID_PAD_SD * sd_atm), GRID_N)
        # price around the settle forward; vol looked up at settle moneyness
        # ln(K/F_set) (sticky-moneyness; differs from expiry moneyness by the bps
        # of interpolated basis — negligible for vol lookup)
        self.K_grid = F_set * np.exp(k_grid)
        C = black_call(F_set, self.K_grid, w_settle(k_grid))
        raw_D = -np.gradient(C, self.K_grid)                       # P(S > K)
        self._D = np.minimum.accumulate(np.clip(raw_D, 0.0, 1.0))
        # Count both probability-bound clipping and monotonicity repair. Measuring
        # only after clipping could silently accept a surface with D < 0 or D > 1.
        self.repair_max = float(np.max(np.abs(raw_D - self._D)))

    def survival(self, K) -> np.ndarray:
        """P(settle > K). Scalar or array in, matching shape out."""
        return np.interp(np.asarray(K, dtype=float), self.K_grid, self._D)

    def bucket(self, floor: float, cap: float) -> float:
        """P(floor < settle <= cap) for range markets."""
        return float(self.survival(floor) - self.survival(cap))

    def median(self) -> float:
        """Strike where P(settle > K) = 0.5 — the model's implied settle center."""
        return float(np.interp(-0.5, -self._D, self.K_grid))  # negate: interp needs ascending

    def pdf(self) -> tuple[np.ndarray, np.ndarray]:
        """(K, density) for the dashboard chart."""
        d = -np.gradient(self._D, self.K_grid)
        return self.K_grid, np.maximum(d, 0.0)


def fair_ladder(density: SettleDensity, floor_strikes: list[float]) -> dict[float, float]:
    """Fair yes-price for each 'greater' contract: P(settle > floor_strike)."""
    vals = density.survival(floor_strikes)
    return {k: float(v) for k, v in zip(floor_strikes, np.atleast_1d(vals))}
