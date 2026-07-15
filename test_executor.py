"""Executor money path: live-captured orderbook shape, Kelly caps, exposure math."""
import pytest

import config
from engine.executor import (_ask_depth, edge_priority, joint_metrics, joint_quantity,
                             kelly_contracts, open_exposure)
from engine.scanner import Edge

# captured live 2026-07-13 from /markets/KXBTCD-26JUL1317-T62749.99/orderbook
LIVE_BOOK = {"orderbook_fp": {
    "no_dollars": [["0.3600", "202.80"], ["0.3700", "202.00"], ["0.3800", "299.99"],
                   ["0.3900", "6844.39"], ["0.4000", "1725.00"]],
    "yes_dollars": [["0.5500", "5200.00"], ["0.5600", "1414.72"], ["0.5700", "1668.00"],
                    ["0.5800", "500.00"], ["0.5900", "2.24"]]}}


def test_ask_depth_parses_live_shape():
    ask, depth = _ask_depth(LIVE_BOOK, "yes")   # crosses best no bid 0.40
    assert abs(ask - 0.60) < 1e-9 and abs(depth - 1725.0) < 1e-9
    ask, depth = _ask_depth(LIVE_BOOK, "no")    # crosses best yes bid 0.59
    assert abs(ask - 0.41) < 1e-9 and abs(depth - 2.24) < 1e-9
    assert _ask_depth({}, "yes") == (1.0, 0.0)
    assert _ask_depth({"orderbook_fp": {"no_dollars": []}}, "yes") == (1.0, 0.0)


def test_kelly_respects_caps():
    from engine import rails
    cap = rails.per_trade_cap()
    # big bankroll: .62 @ .55 Kelly stake 0.25*500*(0.07/0.45)≈$19.4, capped per trade
    assert kelly_contracts(0.62, 0.55, bankroll=500) == int(min(19.44, cap) / 0.55)
    # Integer lots may not exceed the fee-aware quarter-Kelly target.
    assert kelly_contracts(0.62, 0.55, bankroll=14) == 0
    assert kelly_contracts(0.50, 0.55) == 0     # no edge -> no trade
    assert kelly_contracts(0.99, 0.0) == 0      # degenerate price guarded


class FakeKalshi:
    def positions(self):   # doc-shaped: fp/dollars strings
        return [{"ticker": "A", "market_exposure_dollars": "10.000000",
                 "position_fp": "20.00"}]

    def orders(self, status="resting"):
        return [{"outcome_side": "no", "no_price_dollars": "0.3000",
                 "yes_price_dollars": "0.7000", "remaining_count_fp": "5.00"}]


def test_open_exposure_aggregate_not_per_contract():
    # $10 aggregate position + 5 resting no @ 30c = $1.50 (NOT the yes-complement 70c)
    assert abs(open_exposure(FakeKalshi()) - 11.50) < 1e-9


class LegacyKalshi:
    def positions(self):   # pre-migration cents fields must still parse
        return [{"ticker": "A", "market_exposure": 1000}]

    def orders(self, status="resting"):
        return [{"side": "no", "no_price": 30, "yes_price": 70, "remaining_count": 5}]


def test_open_exposure_legacy_fields():
    assert abs(open_exposure(LegacyKalshi()) - 11.50) < 1e-9


def test_open_exposure_maps_v2_bid_ask_to_purchased_outcome():
    resting = [{
        "ticker": "YES-BID", "side": "bid", "yes_price_dollars": ".9000",
        "no_price_dollars": ".1000", "remaining_count_fp": "2.00",
    }, {
        "ticker": "NO-BID", "book_side": "ask", "yes_price_dollars": ".8000",
        "no_price_dollars": ".2000", "remaining_count_fp": "3.00",
    }]
    assert open_exposure(None, {}, resting) == pytest.approx(2.40)


def test_zeroed_api_positions_do_not_create_ghost_risk():
    from engine.executor import position_state

    positions = [{"ticker": "CLOSED", "market_exposure_dollars": "0.0000",
                  "position_fp": "0.00"}]
    assert position_state(None, positions) == ({}, {})


def test_joint_cluster_exposes_low_hit_rate_and_rejects_tail_addition():
    tail, bucket1, bucket2 = "TAIL", "B1", "B2"
    scenarios = (
        (.15, .20, ()),
        (.17, .30, (bucket1,)),
        (.35, .275, (bucket2,)),
        (.33, .225, (tail,)),
    )
    positions = {tail: (10, 3.20), bucket1: (-2, .8555), bucket2: (-2, .8555)}
    metrics = joint_metrics(scenarios, positions, bankroll=25)

    assert metrics is not None
    assert metrics.model_profit_p == pytest.approx(.33)
    assert metrics.market_profit_p == pytest.approx(.225)
    assert metrics.worst_loss == pytest.approx(2.911)

    edge = Edge(tail, "E", "BTC", "yes", .25, .33, .06, 1, scenarios)
    n, reason, _ = joint_quantity(edge, {}, 5, .25, 25)
    assert n == 0 and reason == "profit_probability"
    assert joint_metrics(((1.0, 1.0, ("OTHER",)),),
                         {tail: (1, .25)}, bankroll=25) is None


def test_joint_addition_requires_positive_ev_and_log_growth_under_both_distributions():
    # Both hit-rate floors pass, but the ladder-implied distribution says a 70c
    # taker buy is negative EV.  It must not consume the event/day risk budget.
    scenarios = ((.20, .44, ()), (.80, .56, ("T",)))
    edge = Edge("T", "E", "BTC", "yes", .70, .80, .08, 1, scenarios)
    assert joint_quantity(edge, {}, 1, .70, 25)[:2] == (0, "joint_ev")

    agreed = ((.30, .40, ()), (.70, .60, ("T",)))
    edge = Edge("T", "E", "BTC", "yes", .50, .70, .18, 1, agreed)
    assert joint_quantity(edge, {}, 1, .50, 25)[0] == 1


def test_positive_day_probability_ranks_ahead_of_raw_edge():
    low_hit = Edge(
        "LOW", "E1", "BTC", "yes", .10, .56, .45, 1,
        ((.44, .44, ()), (.56, .56, ("LOW",))),
    )
    high_hit = Edge(
        "HIGH", "E2", "BTC", "yes", .50, .80, .29, 2,
        ((.20, .25, ()), (.80, .75, ("HIGH",))),
    )
    ranked = sorted(
        (low_hit, high_hit), key=lambda edge: edge_priority(edge, {}, 25), reverse=True
    )
    assert ranked == [high_hit, low_hit]


def test_disarmed_gate_blocks_before_anything():
    import pathlib
    import tempfile
    config.ARMED_FILE = pathlib.Path(tempfile.mkdtemp(prefix="dd-arm-")) / "ARMED"
    from engine.executor import execute

    class Keyed:
        can_trade = True
    # DISARMED: must return before touching rails/db/orders (con=None would crash)
    assert execute(Keyed(), None, [], 0.0)[0].startswith("DISARMED")


def test_v2_order_mapping():
    from kalshi.client import v2_order_body
    b = v2_order_body("T", "no", 3, 0.16, client_order_id="x")   # buy no = ask yes
    assert (b["side"], b["price"], b["count"]) == ("ask", "0.8400", "3.00")
    assert b["time_in_force"] == "immediate_or_cancel"
    b = v2_order_body("T", "yes", 2, 0.55, post_only=True)
    assert (b["side"], b["price"]) == ("bid", "0.5500")
    assert b["time_in_force"] == "good_till_canceled" and b["post_only"]


if __name__ == "__main__":
    for fn in [test_ask_depth_parses_live_shape, test_kelly_respects_caps,
               test_open_exposure_aggregate_not_per_contract]:
        fn()
        print(f"PASS {fn.__name__}")
