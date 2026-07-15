"""Deribit option-chain trust-boundary validation."""
import pytest

from engine import deribit


class Response:
    def __init__(self, result):
        self.result = result

    @staticmethod
    def raise_for_status():
        pass

    def json(self):
        return {"result": self.result}


class Client:
    def __init__(self, rows, index=60_000):
        self.rows = rows
        self.index = index

    def get(self, url, **_kwargs):
        if url.endswith("get_index_price"):
            return Response({"index_price": self.index})
        return Response(self.rows)


def row(now, strike, **updates):
    value = {
        "instrument_name": f"BTC-31DEC27-{strike}-C",
        "mark_iv": 50.0,
        "bid_price": .01,
        "ask_price": .03,
        "mark_price": .02,
        "creation_timestamp": now * 1000,
        "underlying_price": 60_100.0,
    }
    value.update(updates)
    return value


def test_chain_rejects_stale_one_sided_and_outside_marks(monkeypatch):
    now = 1_800_000_000.0
    monkeypatch.setattr(deribit.time, "time", lambda: now)
    rows = [
        row(now, 60_000),
        row(now, 61_000, creation_timestamp=(now - 301) * 1000),
        row(now, 62_000, bid_price=None),
        row(now, 63_000, mark_price=.04),
        row(now, 64_000, underlying_price=float("nan")),
    ]
    chain = deribit.fetch_chain("BTC", Client(rows))
    sl = next(iter(chain.slices.values()))
    assert sl.strikes == [60_000]
    assert sl.ivs == [.5]
    assert sl.forward == 60_100


def test_chain_fails_closed_without_valid_surface(monkeypatch):
    now = 1_800_000_000.0
    monkeypatch.setattr(deribit.time, "time", lambda: now)
    with pytest.raises(ValueError, match="no fresh two-sided"):
        deribit.fetch_chain("BTC", Client([row(now, 60_000, ask_price=None)]))
    with pytest.raises(ValueError, match="index price"):
        deribit.fetch_chain("BTC", Client([row(now, 60_000)], index=float("nan")))
