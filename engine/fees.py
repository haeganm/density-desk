"""Kalshi fee model, verified against live fills."""
import math

import config


def _centicent(amount: float) -> float:
    """Round dollars upward to Kalshi's $0.0001 fee increment."""
    return math.ceil(round(amount * 10_000, 9)) / 10_000


def taker_fee(price: float, count: int) -> float:
    """Taker fee in dollars for the whole order."""
    raw = config.TAKER_FEE_MULT * price * (1 - price) * count
    return _centicent(raw)


def maker_fee(price: float, count: int) -> float:
    raw = config.TAKER_FEE_MULT * config.MAKER_FEE_FRACTION * price * (1 - price) * count
    return _centicent(raw)
