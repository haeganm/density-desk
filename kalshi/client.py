"""Minimal Kalshi REST client. RSA-PSS signing per Kalshi API docs; public
market-data endpoints work without keys (client stays usable for Phases 0-2)."""
from __future__ import annotations
import base64
import datetime as dt
import time
from email.utils import parsedate_to_datetime

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

import config


class KalshiAPIError(RuntimeError):
    def __init__(self, msg: str, status: int = 0, body: str = ""):
        super().__init__(msg)
        self.status, self.body = status, body


def fp(obj: dict, dollars_key: str, cents_key: str | None = None, default: float = 0.0) -> float:
    """Fixed-point dollars-string field, with legacy integer-cents fallback.
    Kalshi migrated to `*_dollars`/`*_fp` strings mid-2026; cents fields are deprecated."""
    v = obj.get(dollars_key)
    if v is not None and v != "":
        return float(v)
    if cents_key and obj.get(cents_key) is not None:
        return float(obj[cents_key]) / 100
    return default


def fpc(obj: dict, fp_key: str, legacy_key: str | None = None, default: float = 0.0) -> float:
    """Fixed-point count field (`*_fp` string) with legacy int fallback."""
    v = obj.get(fp_key)
    if v is not None and v != "":
        return float(v)
    if legacy_key and obj.get(legacy_key) is not None:
        return float(obj[legacy_key])
    return default


def v2_order_body(ticker: str, buy_side: str, count: float, price: float,
                  post_only: bool = False, client_order_id: str | None = None) -> dict:
    """V2 orders speak yes-leg only: buying YES @ p = bid @ p; buying NO @ p =
    ask (sell yes) @ (1 - p). Prices/counts are fixed-point strings."""
    yes_price = price if buy_side == "yes" else 1.0 - price
    body = {"ticker": ticker,
            "side": "bid" if buy_side == "yes" else "ask",
            "count": f"{count:.2f}",
            "price": f"{yes_price:.4f}",
            "time_in_force": "good_till_canceled" if post_only else "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross",
            "cancel_order_on_pause": True,
            "post_only": post_only}
    if client_order_id:
        body["client_order_id"] = client_order_id
    return body


class KalshiClient:
    def __init__(self):
        self.http = httpx.Client(base_url=config.KALSHI_BASE, timeout=30)
        self._key = None
        self._key_id = None
        self.clock_skew_s: float | None = None
        if config.KALSHI_PEM.exists() and config.KALSHI_KEY_ID_FILE.exists():
            self._key = serialization.load_pem_private_key(
                config.KALSHI_PEM.read_bytes(), password=None)
            self._key_id = config.KALSHI_KEY_ID_FILE.read_text().strip()

    @property
    def can_trade(self) -> bool:
        return self._key is not None

    def _headers(self, method: str, path: str) -> dict:
        if not self._key:
            return {}
        ts = str(int(time.time() * 1000))
        msg = f"{ts}{method}/trade-api/v2{path}".encode()
        sig = self._key.sign(
            msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=hashes.SHA256().digest_size),
            hashes.SHA256())
        return {"KALSHI-ACCESS-KEY": self._key_id,
                "KALSHI-ACCESS-TIMESTAMP": ts,
                "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()}

    def _req(self, method: str, path: str, **kw):
        # POSTs (order placement) only retry 429 — the request definitely wasn't
        # processed. A 500/503 on a POST may have executed server-side; retrying
        # could double-fill (client_order_id dedup is a backstop, not a license).
        retryable = (429,) if method == "POST" else (429, 500, 503)
        for attempt in range(3):
            r = self.http.request(method, path, headers=self._headers(method, path), **kw)
            self.clock_skew_s = None
            try:
                server_time = parsedate_to_datetime(r.headers["Date"])
                if server_time.tzinfo is None:
                    server_time = server_time.replace(tzinfo=dt.UTC)
                self.clock_skew_s = (server_time - dt.datetime.now(dt.UTC)).total_seconds()
            except (KeyError, TypeError, ValueError, OverflowError):
                pass
            if r.status_code in retryable and attempt < 2:
                time.sleep(1 if attempt == 0 else 4)     # backoff; budget is shared
                continue
            if r.status_code >= 400:
                # body is essential forensics (CPI-night 400s carried the reason)
                raise KalshiAPIError(f"{r.status_code} {method} {path}: {r.text[:300]}",
                                     status=r.status_code, body=r.text or "")
            return r.json()

    # --- public market data ---
    def markets(self, series_ticker: str, status: str = "open", limit: int = 200) -> list[dict]:
        out, cursor = [], None
        while True:
            params = {"series_ticker": series_ticker, "status": status, "limit": limit}
            if cursor:
                params["cursor"] = cursor
            d = self._req("GET", "/markets", params=params)
            out += d.get("markets", [])
            cursor = d.get("cursor")
            if not cursor or not d.get("markets"):
                return out

    def orderbook(self, ticker: str, depth: int = 10) -> dict:
        return self._req("GET", f"/markets/{ticker}/orderbook", params={"depth": depth})

    def markets_by_tickers(self, tickers: list[str], max_batches: int = 3) -> list[dict]:
        """Batch lookup (settled-status checks). Capped + spaced per call to stay
        inside the shared rate budget (live-fire 429s on 2026-07-14); overflow
        tickers simply roll to the next cycle."""
        out = []
        for n, i in enumerate(range(0, len(tickers), 40)):
            if n >= max_batches:
                break
            if n:
                time.sleep(0.35)
            d = self._req("GET", "/markets", params={"tickers": ",".join(tickers[i:i + 40])})
            out += d.get("markets", [])
        return out

    # --- authenticated ---
    def balance(self) -> dict:
        return self._req("GET", "/portfolio/balance")

    def positions(self) -> list[dict]:
        return self._paged(
            "/portfolio/positions", "market_positions", {"limit": 200}
        )

    def _paged(self, path: str, key: str, params: dict, max_pages: int = 20) -> list[dict]:
        out, cursor = [], None
        for _ in range(max_pages):
            p = dict(params)
            if cursor:
                p["cursor"] = cursor
            d = self._req("GET", path, params=p)
            out += d.get(key, [])
            cursor = d.get("cursor")
            if not cursor:
                return out
        raise KalshiAPIError(
            f"pagination truncated for {path} after {max_pages} pages",
            body=str(cursor or ""),
        )

    def fills(self, ticker: str | None = None, limit: int = 200,
              min_ts: float | None = None) -> list[dict]:
        # min_ts keeps the page walk bounded; lifetime history would eventually
        # exceed the 20-page pagination ceiling and brick sync (fail-closed)
        params = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if min_ts is not None:
            params["min_ts"] = int(min_ts)
        return self._paged("/portfolio/fills", "fills", params)

    def settlements(self, limit: int = 200, min_ts: float | None = None) -> list[dict]:
        params = {"limit": limit}
        if min_ts is not None:
            params["min_ts"] = int(min_ts)
        return self._paged("/portfolio/settlements", "settlements", params)

    def orders(self, status: str | None = "resting") -> list[dict]:
        params = {"limit": 200}
        if status is not None:
            params["status"] = status
        return self._paged("/portfolio/orders", "orders", params)

    def create_order(self, ticker: str, buy_side: str, count: float, price: float,
                     post_only: bool = False,
                     client_order_id: str | None = None) -> dict:
        """Buy `count` contracts of buy_side ('yes'|'no') at `price` dollars via
        the V2 endpoint (legacy /portfolio/orders is deprecated)."""
        return self._req("POST", "/portfolio/events/orders",
                         json=v2_order_body(ticker, buy_side, count, price,
                                            post_only, client_order_id))

    def cancel_order(self, order_id: str) -> dict:
        # V2 path — legacy /portfolio/orders/{id} returns 410 deprecated (2026-07-15 live)
        return self._req("DELETE", f"/portfolio/events/orders/{order_id}")

    def amend_order(self, order_id: str, ticker: str, buy_side: str, count: float,
                    price: float, client_order_id: str | None = None) -> dict:
        """Reprice a resting order. Kalshi keeps queue position ONLY when size
        decreases; a price change goes to the back of the queue (still cheaper
        than cancel+repost: one round trip, no window where we're off the book).
        Body mirrors v2_order_body's yes-leg mapping (buy no = ask at 1-price)."""
        body = v2_order_body(ticker, buy_side, count, price, post_only=True,
                             client_order_id=client_order_id)
        payload = {"ticker": body["ticker"], "side": body["side"],
                   "price": body["price"], "count": body["count"]}
        if client_order_id:
            payload["client_order_id"] = client_order_id
        return self._req("POST", f"/portfolio/events/orders/{order_id}/amend",
                         json=payload)
