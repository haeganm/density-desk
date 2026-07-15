"""Deribit public-API chain fetch. One HTTP call per currency; no auth."""
from __future__ import annotations
import math
import re
import time
from dataclasses import dataclass, field

import httpx

import config

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}
_NAME_RE = re.compile(r"^(\w+)-(\d{1,2})([A-Z]{3})(\d{2})-(\d+(?:\.\d+)?)-([CP])$")


@dataclass
class ExpirySlice:
    expiry_ts: float                      # unix seconds (08:00 UTC)
    forward: float                        # Deribit underlying_price for this expiry
    strikes: list[float] = field(default_factory=list)
    ivs: list[float] = field(default_factory=list)   # decimal (0.65), avg of C/P mark_iv


@dataclass
class Chain:
    ccy: str
    index_price: float
    fetched_ts: float
    slices: dict[float, ExpirySlice]      # keyed by expiry_ts, sorted access via .expiries

    @property
    def expiries(self) -> list[float]:
        return sorted(self.slices)

    def slice_for_settle(self, settle_ts: float) -> ExpirySlice:
        """Smallest expiry at/after the settle time."""
        for ts in self.expiries:
            if ts >= settle_ts - 1:
                return self.slices[ts]
        raise ValueError(f"no {self.ccy} expiry at/after {settle_ts}")


def _expiry_unix(day: str, mon: str, yy: str) -> float:
    import datetime as dt
    d = dt.datetime(2000 + int(yy), _MONTHS[mon], int(day), 8, 0, tzinfo=dt.UTC)
    return d.timestamp()


def fetch_chain(ccy: str, client: httpx.Client | None = None) -> Chain:
    c = client or httpx.Client(timeout=20)
    try:
        r = c.get(f"{config.DERIBIT_BASE}/public/get_book_summary_by_currency",
                  params={"currency": ccy, "kind": "option"})
        r.raise_for_status()
        rows = r.json()["result"]
        ri = c.get(f"{config.DERIBIT_BASE}/public/get_index_price",
                   params={"index_name": f"{ccy.lower()}_usd"})
        ri.raise_for_status()
        index_price = float(ri.json()["result"]["index_price"])
    finally:
        if client is None:
            c.close()
    if not math.isfinite(index_price) or index_price <= 0:
        raise ValueError(f"invalid {ccy} index price")

    # collect per (expiry, strike): mark_iv of C and P, then average
    acc: dict[tuple[float, float], list[float]] = {}
    fwd: dict[float, float] = {}
    now = time.time()
    for row in rows:
        m = _NAME_RE.match(str(row.get("instrument_name", "")))
        try:
            iv = float(row["mark_iv"])
            bid = float(row["bid_price"])
            ask = float(row["ask_price"])
            mark = float(row["mark_price"])
            created = float(row["creation_timestamp"]) / 1000
            underlying = float(row["underlying_price"])
        except (KeyError, TypeError, ValueError):
            continue
        if (not m or not all(math.isfinite(x) for x in
                             (iv, bid, ask, mark, created, underlying))
                or not (1.0 < iv < 500.0)
                or not (0 < bid <= mark <= ask)
                or underlying <= 0
                or abs(now - created) > config.STALE_DATA_HALT_S):
            continue
        ts = _expiry_unix(m.group(2), m.group(3), m.group(4))
        strike = float(m.group(5))
        acc.setdefault((ts, strike), []).append(iv / 100.0)
        fwd[ts] = underlying

    slices: dict[float, ExpirySlice] = {}
    for (ts, strike), ivs in sorted(acc.items()):
        sl = slices.setdefault(ts, ExpirySlice(expiry_ts=ts, forward=fwd[ts]))
        sl.strikes.append(strike)
        sl.ivs.append(sum(ivs) / len(ivs))
    if not slices:
        raise ValueError(f"no fresh two-sided {ccy} option quotes")
    return Chain(ccy=ccy, index_price=index_price, fetched_ts=now, slices=slices)
