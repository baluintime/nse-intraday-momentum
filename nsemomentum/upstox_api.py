"""Thin REST client for the Upstox API v2.

Only the endpoints the strategy needs: candles, quotes, option chain/contracts,
orders, positions and funds. All calls raise UpstoxError on API-level failure.
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import time as _time
import urllib.parse
from typing import Any

import requests

log = logging.getLogger(__name__)

BASE_URL = "https://api.upstox.com"
ASSETS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"


class UpstoxError(RuntimeError):
    pass


class UpstoxAPI:
    def __init__(self, access_token: str | None, max_retries: int = 3):
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})
        self.max_retries = max_retries
        self.set_token(access_token)

    def set_token(self, access_token: str | None) -> None:
        """Swap the bearer token (e.g. after a fresh web login)."""
        self.access_token = access_token
        if access_token:
            self._session.headers["Authorization"] = f"Bearer {access_token}"
        else:
            self._session.headers.pop("Authorization", None)

    @property
    def has_token(self) -> bool:
        return bool(self.access_token)

    # ------------------------------------------------------------------ core

    def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        if not self.access_token:
            raise UpstoxError("not connected to Upstox — no access token")
        url = f"{BASE_URL}{path}"
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = self._session.request(method, url, timeout=30, **kwargs)
                if resp.status_code == 429:
                    _time.sleep(1 + attempt)
                    continue
                body = resp.json()
                if resp.status_code >= 400 or body.get("status") == "error":
                    raise UpstoxError(f"{method} {path} -> {resp.status_code}: {resp.text[:500]}")
                return body
            except (requests.ConnectionError, requests.Timeout, json.JSONDecodeError) as exc:
                last_exc = exc
                _time.sleep(2**attempt)
        raise UpstoxError(f"{method} {path} failed after retries: {last_exc}")

    @staticmethod
    def _enc(instrument_key: str) -> str:
        return urllib.parse.quote(instrument_key, safe="")

    # ----------------------------------------------------------------- data

    def intraday_candles(self, instrument_key: str, unit: str = "1minute") -> list[list]:
        """Today's completed candles, newest first: [ts, o, h, l, c, vol, oi]."""
        body = self._request(
            "GET", f"/v2/historical-candle/intraday/{self._enc(instrument_key)}/{unit}"
        )
        return body.get("data", {}).get("candles", []) or []

    def historical_candles(
        self, instrument_key: str, to_date: str, from_date: str, unit: str = "1minute"
    ) -> list[list]:
        """Historical candles between dates (YYYY-MM-DD), newest first."""
        body = self._request(
            "GET",
            f"/v2/historical-candle/{self._enc(instrument_key)}/{unit}/{to_date}/{from_date}",
        )
        return body.get("data", {}).get("candles", []) or []

    def ltp(self, instrument_keys: list[str]) -> dict[str, float]:
        """Last traded price for one or more instrument keys."""
        body = self._request(
            "GET",
            "/v2/market-quote/ltp",
            params={"instrument_key": ",".join(instrument_keys)},
        )
        out: dict[str, float] = {}
        for item in (body.get("data") or {}).values():
            key = item.get("instrument_token") or item.get("instrument_key")
            if key is not None and item.get("last_price") is not None:
                out[key] = float(item["last_price"])
        return out

    def ltp_single(self, instrument_key: str) -> float | None:
        return self.ltp([instrument_key]).get(instrument_key)

    def full_quote(self, instrument_keys: list[str]) -> dict[str, dict]:
        """Full market quote for one or more instruments: OHLC, last price,
        five-level bid/ask depth, volume and (for F&O) open interest.

        Returns ``{instrument_key: quote_dict}``. The momentum screener uses
        ``depth`` (order-book imbalance) and ``oi`` (futures OI build-up)."""
        body = self._request(
            "GET",
            "/v2/market-quote/quotes",
            params={"instrument_key": ",".join(instrument_keys)},
        )
        out: dict[str, dict] = {}
        for item in (body.get("data") or {}).values():
            key = item.get("instrument_token") or item.get("instrument_key")
            if key is not None:
                out[key] = item
        return out

    @staticmethod
    def depth_totals(quote: dict) -> tuple[float | None, float | None]:
        """Total buy vs sell quantity across the five order-book levels of a
        ``full_quote`` entry. Returns (buy_qty, sell_qty), each None if absent."""
        depth = (quote or {}).get("depth") or {}
        buy = depth.get("buy") or []
        sell = depth.get("sell") or []
        buy_qty = sum(float(lvl.get("quantity", 0) or 0) for lvl in buy) if buy else None
        sell_qty = sum(float(lvl.get("quantity", 0) or 0) for lvl in sell) if sell else None
        return buy_qty, sell_qty

    # -------------------------------------------------------------- options

    def option_contracts(self, underlying_key: str) -> list[dict]:
        body = self._request(
            "GET", "/v2/option/contract", params={"instrument_key": underlying_key}
        )
        return body.get("data", []) or []

    def option_chain(self, underlying_key: str, expiry_date: str) -> list[dict]:
        body = self._request(
            "GET",
            "/v2/option/chain",
            params={"instrument_key": underlying_key, "expiry_date": expiry_date},
        )
        return body.get("data", []) or []

    # --------------------------------------------------------------- orders

    def place_order(
        self,
        instrument_key: str,
        quantity: int,
        transaction_type: str,
        order_type: str = "LIMIT",
        price: float = 0.0,
        product: str = "I",
        tag: str = "nsemomentum",
    ) -> str:
        payload = {
            "instrument_token": instrument_key,
            "quantity": quantity,
            "transaction_type": transaction_type,
            "order_type": order_type,
            "price": round(price, 2) if order_type == "LIMIT" else 0,
            "product": product,
            "validity": "DAY",
            "disclosed_quantity": 0,
            "trigger_price": 0,
            "is_amo": False,
            "tag": tag,
        }
        body = self._request("POST", "/v2/order/place", json=payload)
        return body["data"]["order_id"]

    def cancel_order(self, order_id: str) -> None:
        self._request("DELETE", "/v2/order/cancel", params={"order_id": order_id})

    def order_details(self, order_id: str) -> dict:
        body = self._request("GET", "/v2/order/details", params={"order_id": order_id})
        return body.get("data", {}) or {}

    def positions(self) -> list[dict]:
        body = self._request("GET", "/v2/portfolio/short-term-positions")
        return body.get("data", []) or []

    def funds(self) -> dict:
        body = self._request("GET", "/v2/user/get-funds-and-margin")
        return body.get("data", {}) or {}

    # ---------------------------------------------------- instrument master

    @staticmethod
    def download_nse_instruments() -> list[dict]:
        """Download and decode Upstox's NSE instrument master (no auth needed)."""
        resp = requests.get(ASSETS_URL, timeout=60)
        resp.raise_for_status()
        with gzip.GzipFile(fileobj=io.BytesIO(resp.content)) as fh:
            return json.load(fh)
