"""Fee model vs hand-computed and observed live values."""
from engine.fees import maker_fee, taker_fee


def test_taker_hand_computed():
    assert taker_fee(0.50, 1) == 0.0175
    assert taker_fee(0.50, 100) == 1.75
    assert taker_fee(0.01, 1) == 0.0007
    assert taker_fee(0.05, 10) == 0.0333


def test_maker_is_free_on_these_series():
    # verified live 2026-07-15: every maker fill on KXBTC/KXETH charged $0.0000.
    # If MAKER_FEE_FRACTION is ever raised again, the scaling must still hold.
    assert maker_fee(0.50, 100) == 0.0
    assert maker_fee(0.50, 1) == 0.0


def test_taker_matches_ten_observed_fills():
    observed = [
        (0.16, 3, 0.0283),
        (0.25, 2, 0.0263),
        (0.77, 1, 0.0124),
        (0.14, 4, 0.0338),
        (0.77, 2, 0.0248),
        (0.12, 6, 0.0444),
        (0.24, 3, 0.0384),
        (0.85, 2, 0.0179),
        (0.62, 1, 0.0165),
        (0.62, 1, 0.0165),
    ]
    assert [taker_fee(price, count) for price, count, _ in observed] == [
        fee for _, _, fee in observed
    ]


if __name__ == "__main__":
    for fn in [test_taker_hand_computed, test_maker_quarter_of_taker,
               test_taker_matches_ten_observed_fills]:
        fn()
        print(f"PASS {fn.__name__}")
