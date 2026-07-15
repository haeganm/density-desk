import math

from engine import regime


def test_regime_uses_only_completed_adjacent_bars():
    base = 1_800_000_000_000
    bars = [(base, 100.0), (base + regime.BAR_MS, 101.0),
            (base + 3 * regime.BAR_MS, 150.0),
            (base + 4 * regime.BAR_MS, 151.0)]
    returns = regime._returns(bars)
    assert len(returns) == 2
    assert returns == [math.log(1.01), math.log(151 / 150)]
    assert regime.metrics(bars)["rv_1h"] is None  # the gap cannot form a fake window


def test_surface_ratios_are_shadow_diagnostics():
    out = regime.surface_ratios({"rv_24h": .20}, 60_000, 3600, 300, 150)
    assert out["rv_implied_iqr"] > 0
    assert out["model_iqr_to_rv"] == 2 * out["market_iqr_to_rv"]
