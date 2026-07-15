"""Client transport behavior: 429 backoff retry, 4xx surfaces the body."""
import httpx

import config
from kalshi.client import KalshiAPIError, KalshiClient


def make_client(handler):
    k = KalshiClient.__new__(KalshiClient)
    k._key = k._key_id = None
    k.http = httpx.Client(base_url=config.KALSHI_BASE,
                          transport=httpx.MockTransport(handler))
    return k


def test_429_retries_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 2:
            return httpx.Response(429, text="rate limited")
        return httpx.Response(200, json={"ok": True})

    assert make_client(handler)._req("GET", "/markets") == {"ok": True}
    assert calls["n"] == 2


def test_post_500_is_never_retried():
    """A 500 on order placement may have executed server-side — retrying could
    double-fill. This pins the single most safety-critical line in the client."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(500, text="internal")

    try:
        make_client(handler)._req("POST", "/portfolio/events/orders")
        raise AssertionError("500 not raised")
    except KalshiAPIError:
        pass
    assert calls["n"] == 1, f"POST retried on 500 ({calls['n']} calls) — double-fill risk"
    # GETs may retry 500 freely (idempotent)
    calls["n"] = 0

    def handler2(request):
        calls["n"] += 1
        return httpx.Response(500 if calls["n"] < 2 else 200, json={"ok": 1})

    assert make_client(handler2)._req("GET", "/markets") == {"ok": 1}


def test_4xx_raises_with_body():
    def handler(request):
        return httpx.Response(400, json={"error": {"code": "market_paused"}})

    try:
        make_client(handler)._req("POST", "/portfolio/events/orders")
        raise AssertionError("400 not raised")
    except KalshiAPIError as e:
        assert e.status == 400 and "market_paused" in e.body


def test_positions_and_orders_are_fully_paginated():
    seen = []

    def handler(request):
        seen.append((request.url.path, request.url.params.get("cursor"),
                     request.url.params.get("status")))
        cursor = request.url.params.get("cursor")
        key = "market_positions" if request.url.path.endswith("positions") else "orders"
        return httpx.Response(200, json={
            key: [{"ticker": "A" if cursor is None else "B"}],
            "cursor": "next" if cursor is None else "",
        })

    client = make_client(handler)
    try:
        assert [row["ticker"] for row in client.positions()] == ["A", "B"]
        assert [row["ticker"] for row in client.orders(status=None)] == ["A", "B"]
    finally:
        client.http.close()
    assert seen == [
        ("/trade-api/v2/portfolio/positions", None, None),
        ("/trade-api/v2/portfolio/positions", "next", None),
        ("/trade-api/v2/portfolio/orders", None, None),
        ("/trade-api/v2/portfolio/orders", "next", None),
    ]


def test_pagination_limit_fails_closed_instead_of_returning_partial_state():
    def handler(_request):
        return httpx.Response(200, json={
            "market_positions": [{"ticker": "A"}], "cursor": "still-more",
        })

    client = make_client(handler)
    try:
        with __import__("pytest").raises(KalshiAPIError, match="pagination truncated"):
            client._paged(
                "/portfolio/positions", "market_positions", {"limit": 1}, max_pages=2
            )
    finally:
        client.http.close()


def test_cancel_uses_v2_events_path():
    # legacy DELETE /portfolio/orders/{id} returns 410 deprecated (live 2026-07-15)
    seen = {}

    def handler(request):
        seen["method"], seen["path"] = request.method, request.url.path
        return httpx.Response(200, json={"order_id": "x", "reduced_by": "2.00"})

    client = make_client(handler)
    try:
        client.cancel_order("abc-123")
    finally:
        client.http.close()
    assert seen["method"] == "DELETE"
    assert seen["path"].endswith("/portfolio/events/orders/abc-123")


if __name__ == "__main__":
    for fn in [test_429_retries_then_succeeds, test_4xx_raises_with_body]:
        fn()
        print(f"PASS {fn.__name__}")
