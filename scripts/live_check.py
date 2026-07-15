"""Phase 1 live check: Deribit-implied fair vs Kalshi quotes for open ladder markets."""
import datetime as dt
import sys

sys.path.insert(0, r"C:\density desk")
import httpx

import config
from engine.checks import sanity
from engine.deribit import fetch_chain
from engine.density import SettleDensity


def print_event(asset, chain, ev, ems):
    settle = dt.datetime.fromisoformat(ems[0]["close_time"].replace("Z", "+00:00")).timestamp()
    sl = chain.slice_for_settle(settle)
    d = SettleDensity(sl, settle_ts=settle, index_price=chain.index_price)
    sanity(d)
    ttm_min = (settle - chain.fetched_ts) / 60
    print(f"\n{asset} {ev}: index={chain.index_price:,.2f}  settle={ems[0]['close_time']} "
          f"({ttm_min:.0f} min away)  slice_fwd={sl.forward:,.2f}  strikes_used={len(sl.strikes)}")
    print(f"{'strike':>12} {'fair':>6} {'bid':>6} {'ask':>6} {'mid':>6} {'fair-mid':>8}")
    for m in sorted(ems, key=lambda x: x["floor_strike"]):
        K = m["floor_strike"]
        fair = float(d.survival(K))
        bid, ask = float(m.get("yes_bid_dollars") or 0), float(m.get("yes_ask_dollars") or 1)
        mid = (bid + ask) / 2
        flag = " <-- wide" if ask - bid > 0.10 else ""
        if 0.02 < fair < 0.98 or 0.02 < mid < 0.98:
            print(f"{K:>12,.2f} {fair:>6.2f} {bid:>6.2f} {ask:>6.2f} {mid:>6.2f} {fair - mid:>+8.2f}{flag}")


def main():
    c = httpx.Client(timeout=30)
    for asset, s in config.SERIES.items():
        chain = fetch_chain(s["deribit_ccy"], c)
        r = c.get(f"{config.KALSHI_BASE}/markets",
                  params={"series_ticker": s["ladder"], "status": "open", "limit": 200})
        mkts = [m for m in r.json().get("markets", []) if m.get("strike_type") == "greater"]
        if not mkts:
            print(f"{asset}: no open ladder markets right now")
            continue
        events: dict[str, list] = {}
        for m in mkts:
            events.setdefault(m["event_ticker"], []).append(m)
        for ev, ems in sorted(events.items()):
            print_event(asset, chain, ev, ems)


if __name__ == "__main__":
    main()
