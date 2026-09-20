"""Async CoinDCX Futures REST client (HMAC-SHA256 signed).

Signing: body = {"timestamp": epoch_ms, ...}; sig = HMAC_SHA256(secret, compact_json(body)).
Headers: X-AUTH-APIKEY, X-AUTH-SIGNATURE. Futures pair format: B-BTC_USDT.
Reference: https://docs.coindcx.com/ (Futures End Points).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from typing import Any, Dict, List, Optional

import aiohttp

from rate_limiter import RateLimiterGroup

BASE_URL = "https://api.coindcx.com"
TERMINAL = {"filled", "cancelled", "partially_cancelled", "rejected"}


class CoinDCXError(Exception):
    def __init__(self, status: int, body: Any) -> None:
        super().__init__(f"CoinDCX API {status}: {body}")
        self.status = status
        self.body = body


class CoinDCXFuturesClient:
    def __init__(self, api_key: str, api_secret: str, base_url: str = BASE_URL, timeout_s: float = 10.0) -> None:
        if not api_key or not api_secret:
            raise ValueError("api_key and api_secret are required")
        self.api_key = api_key
        self._secret = api_secret.encode()
        self.base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        self.limiter = RateLimiterGroup(600)   # 16 req/s general; stay well below
        self.http_429_count = 0

    @staticmethod
    def pair(coin: str) -> str:
        return f"B-{coin.upper()}_USDT"

    async def _request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None, _attempt: int = 0) -> Any:
        await self.limiter.acquire(path)
        payload = {"timestamp": int(time.time() * 1000), **(body or {})}
        raw = json.dumps(payload, separators=(",", ":")).encode()
        sig = hmac.new(self._secret, raw, hashlib.sha256).hexdigest()
        headers = {"Content-Type": "application/json", "X-AUTH-APIKEY": self.api_key, "X-AUTH-SIGNATURE": sig}
        async with aiohttp.ClientSession(timeout=self._timeout) as s:
            async with s.request(method, self.base_url + path, data=raw, headers=headers) as r:
                text = await r.text()
                try: data = json.loads(text) if text else {}
                except Exception: data = {"raw": text}
                if r.status == 429 and _attempt < 2:
                    self.http_429_count += 1
                    await asyncio.sleep(1.5 * (2 ** _attempt))
                    return await self._request(method, path, body, _attempt + 1)
                if r.status >= 400:
                    raise CoinDCXError(r.status, data)
                return data

    async def wallet_balance(self) -> Any:
        return await self._request("GET", "/exchange/v1/derivatives/futures/wallets")

    async def positions(self, pairs: Optional[List[str]] = None) -> Any:
        body: Dict[str, Any] = {"page": "1", "size": "50", "margin_currency_short_name": ["USDT"]}
        if pairs: body["pairs"] = ",".join(pairs)
        return await self._request("POST", "/exchange/v1/derivatives/futures/positions", body)

    async def update_leverage(self, coin: str, leverage: int) -> Any:
        return await self._request("POST", "/exchange/v1/derivatives/futures/positions/update_leverage",
                                   {"pair": self.pair(coin), "leverage": str(int(leverage)),
                                    "margin_currency_short_name": "USDT"})

    async def market_order(self, coin: str, side: str, quantity: float, leverage: int) -> Dict[str, Any]:
        res = await self._request("POST", "/exchange/v1/derivatives/futures/orders/create", {
            "order": {"side": side.lower(), "pair": self.pair(coin), "order_type": "market_order",
                      "total_quantity": quantity, "leverage": int(leverage),
                      "notification": "no_notification", "margin_currency_short_name": "USDT"}})
        return res[0] if isinstance(res, list) and res else res

    async def cancel_order(self, order_id: str) -> Any:
        return await self._request("POST", "/exchange/v1/derivatives/futures/orders/cancel", {"id": order_id})

    async def list_orders(self, status: str, side: str) -> List[Dict[str, Any]]:
        res = await self._request("POST", "/exchange/v1/derivatives/futures/orders",
                                  {"status": status, "side": side.lower(), "page": "1", "size": "50",
                                   "margin_currency_short_name": ["USDT"]})
        return res if isinstance(res, list) else (res.get("data") or [])

    async def wait_for_fill(self, order_id: str, side: str, timeout_s: float = 5.0, poll_s: float = 0.4) -> Dict[str, Any]:
        deadline = time.time() + timeout_s
        last: Dict[str, Any] = {}
        while time.time() < deadline:
            for status in ("filled", "open", "cancelled", "rejected", "partially_filled"):
                try:
                    for o in await self.list_orders(status, side):
                        if o.get("id") == order_id:
                            last = o
                            if str(o.get("status", "")).lower() in TERMINAL:
                                return o
                except CoinDCXError:
                    pass
            await asyncio.sleep(poll_s)
        return last

    async def instrument(self, coin: str) -> Dict[str, Any]:
        """Public instrument details: min_notional, quantity_increment, taker_fee, max leverage…"""
        url = f"{self.base_url}/exchange/v1/derivatives/futures/data/instrument?pair={self.pair(coin)}&margin_currency_short_name=USDT"
        async with aiohttp.ClientSession(timeout=self._timeout) as s:
            async with s.get(url) as r:
                d = await r.json(content_type=None)
                if r.status >= 400: raise CoinDCXError(r.status, d)
                return d.get("instrument") or d

    async def last_price(self, coin: str) -> float:
        async with aiohttp.ClientSession(timeout=self._timeout) as s:
            async with s.get("https://public.coindcx.com/market_data/v3/current_prices/futures/rt") as r:
                d = await r.json(content_type=None)
        return float(((d.get("prices") or {}).get(self.pair(coin)) or {}).get("ls") or 0)

    def limiter_stats(self) -> Dict[str, Any]:
        d = self.limiter.stats(); d["http_429_count"] = self.http_429_count
        return d
