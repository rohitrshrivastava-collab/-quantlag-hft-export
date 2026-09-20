"""Async CoinSwitch PRO Futures REST client (Ed25519 signed).

Wraps the endpoints needed by the arbitrage engine:
  - wallet_balance / positions / instrument_info / ticker / order_book
  - place_order / get_order_status / cancel_order / cancel_all_open_orders

Reference: https://api-trading.coinswitch.co/futures/reference/
Auth:      https://api-trading.coinswitch.co/get-started/authentication

Signing scheme (Ed25519 over `METHOD + path_with_query + epoch_ms`):
    headers = {
        "X-AUTH-APIKEY":    <hex public key>,
        "X-AUTH-SIGNATURE": <hex Ed25519 sig>,
        "X-AUTH-EPOCH":     <str Unix ms>,
    }
Body is NOT included in the signed message.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import random
import time
import urllib.parse
import uuid
from typing import Any, Dict, Optional

import aiohttp
from cryptography.hazmat.primitives.asymmetric import ed25519

from rate_limiter import RateLimiterGroup


DEFAULT_BASE_URL = "https://coinswitch.co"
EXCHANGE_ID = "EXCHANGE_2"
ORDER_PATHS = ("/trade/api/v2/futures/order", "/trade/api/v2/futures/cancel-all")


class CoinSwitchError(Exception):
    def __init__(self, status: int, body: Any) -> None:
        super().__init__(f"CoinSwitch API {status}: {body}")
        self.status = status
        self.body = body


class CoinSwitchFuturesClient:
    """Thin async client around the CoinSwitch PRO Futures v2 REST surface."""

    def __init__(self,
                 api_key: str,
                 secret_key: str,
                 base_url: str = DEFAULT_BASE_URL,
                 timeout_s: float = 8.0) -> None:
        if not api_key or not secret_key:
            raise ValueError("api_key and secret_key are required")
        self.api_key = api_key
        self._signer = ed25519.Ed25519PrivateKey.from_private_bytes(bytes.fromhex(secret_key))
        self.base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        orders_pm = int(os.environ.get("CS_RATE_LIMIT_ORDERS_PER_MIN", "20"))
        default_pm = int(os.environ.get("CS_RATE_LIMIT_DEFAULT_PER_MIN", "60"))
        self.limiter = RateLimiterGroup(default_pm, {p: orders_pm for p in ORDER_PATHS})
        self.http_429_count = 0
        self._sess: Optional[aiohttp.ClientSession] = None
        self._specs: Dict[str, Dict[str, Any]] = {}
        self.last_create_ms: Optional[int] = None
        self.last_fill_latency_ms: Optional[int] = None
        self.last_warm_ms: Optional[int] = None
        self.last_warm_error: str = ""
        self.fills_via_rest = 0
        self.fills_via_create = 0   # market order already terminal in the create response

    async def _session(self) -> aiohttp.ClientSession:
        """One warm keep-alive session so order placement skips DNS+TCP+TLS."""
        if self._sess is None or self._sess.closed:
            conn = aiohttp.TCPConnector(limit=8, ttl_dns_cache=300, keepalive_timeout=60, enable_cleanup_closed=True)
            self._sess = aiohttp.ClientSession(timeout=self._timeout, connector=conn)
        return self._sess

    async def keep_warm(self) -> None:
        try:
            t0 = time.perf_counter()
            await self.ticker("BTCUSDT")
            self.last_warm_ms = round((time.perf_counter() - t0) * 1000)
            self.last_warm_error = ""
        except Exception as e:
            self.last_warm_error = str(e)[:80]

    async def close(self) -> None:
        if self._sess and not self._sess.closed: await self._sess.close()

    # ------------------------- signing / transport --------------------------
    def _sign(self, method: str, path: str,
              params: Optional[Dict[str, Any]] = None) -> tuple:
        """Return (headers, signed_path) for a signed request.

        The signed_path is `path` plus urlencoded params (if any). Callers use
        `base_url + signed_path` as the request URL and pass `headers` as-is.
        """
        method = method.upper()
        if params:
            sep = "&" if "?" in path else "?"
            path = path + sep + urllib.parse.urlencode(params)
        decoded = urllib.parse.unquote_plus(path)
        epoch = str(int(time.time() * 1000))
        sig = self._signer.sign((method + decoded + epoch).encode("utf-8")).hex()
        headers = {
            "Content-Type": "application/json",
            "X-AUTH-APIKEY": self.api_key,
            "X-AUTH-SIGNATURE": sig,
            "X-AUTH-EPOCH": epoch,
        }
        return headers, path

    async def _request(self, method: str, path: str,
                       params: Optional[Dict[str, Any]] = None,
                       body: Optional[Dict[str, Any]] = None,
                       _attempt: int = 0) -> Dict[str, Any]:
        await self.limiter.acquire(path)
        headers, signed_path = self._sign(method, path, params)
        url = self.base_url + signed_path
        s = await self._session()
        t0 = time.perf_counter()
        async with s.request(method, url, headers=headers,
                             data=json.dumps(body) if body is not None else None) as r:
            text = await r.text()
            try:
                payload = json.loads(text) if text else {}
            except Exception:
                payload = {"raw": text}
            if method.upper() == "POST" and path.endswith("/futures/order"):
                self.last_create_ms = round((time.perf_counter() - t0) * 1000)
            if r.status == 429 and _attempt < 2:
                self.http_429_count += 1
                retry_after = float(r.headers.get("Retry-After") or 0) or (1.5 * (2 ** _attempt))
                await asyncio.sleep(retry_after + random.uniform(0.05, 0.4))
                return await self._request(method, path, params, body, _attempt + 1)
            if r.status >= 400:
                raise CoinSwitchError(r.status, payload)
            return payload

    def limiter_stats(self) -> Dict[str, Any]:
        d = self.limiter.stats()
        d["http_429_count"] = self.http_429_count
        return d

    # ------------------------- account ---------------------------------------
    async def wallet_balance(self) -> Dict[str, Any]:
        return await self._request("GET", "/trade/api/v2/futures/wallet_balance")

    async def positions(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {"exchange": EXCHANGE_ID}
        if symbol: params["symbol"] = symbol.upper()
        return await self._request("GET", "/trade/api/v2/futures/positions", params=params)

    # ------------------------- reference data --------------------------------
    async def instrument_info(self, symbol: str) -> Dict[str, Any]:
        return await self._request("GET", "/trade/api/v2/futures/instrument_info",
                                   params={"exchange": EXCHANGE_ID, "symbol": symbol.upper()})

    async def ticker(self, symbol: str) -> Dict[str, Any]:
        return await self._request("GET", "/trade/api/v2/futures/ticker",
                                   params={"exchange": EXCHANGE_ID, "symbol": symbol.upper()})

    async def order_book(self, symbol: str) -> Dict[str, Any]:
        return await self._request("GET", "/trade/api/v2/futures/order_book",
                                   params={"exchange": EXCHANGE_ID, "symbol": symbol.upper()})

    # ------------------------- orders ----------------------------------------
    async def place_order(self, *, symbol: str, side: str, order_type: str,
                          quantity: float, price: Optional[float] = None,
                          trigger_price: Optional[float] = None,
                          reduce_only: Optional[bool] = None,
                          time_in_force: Optional[str] = None,
                          client_order_id: Optional[str] = None) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "exchange": EXCHANGE_ID,
            "symbol": symbol.upper(),
            "side": side.upper(),
            "order_type": order_type.upper(),
            "quantity": quantity,
            "client_order_id": client_order_id or str(uuid.uuid4()),
        }
        if price is not None:         body["price"] = price
        if trigger_price is not None: body["trigger_price"] = trigger_price
        if reduce_only is not None:   body["reduce_only"] = reduce_only
        if time_in_force is not None: body["time_in_force"] = time_in_force.upper()
        return await self._request("POST", "/trade/api/v2/futures/order", body=body)

    async def get_order_status(self, order_id: str) -> Dict[str, Any]:
        return await self._request("GET", "/trade/api/v2/futures/order",
                                   params={"exchange": EXCHANGE_ID, "order_id": order_id})

    async def cancel_order(self, order_id: str) -> Dict[str, Any]:
        return await self._request("DELETE", "/trade/api/v2/futures/order",
                                   params={"exchange": EXCHANGE_ID, "order_id": order_id})

    async def cancel_all_open_orders(self, symbol: str) -> Dict[str, Any]:
        return await self._request("POST", "/trade/api/v2/futures/cancel-all",
                                   body={"exchange": EXCHANGE_ID, "symbol": symbol.upper()})

    async def update_leverage(self, symbol: str, leverage: int) -> Dict[str, Any]:
        return await self._request("POST", "/trade/api/v2/futures/leverage",
                                   body={"exchange": EXCHANGE_ID, "symbol": symbol.upper(), "leverage": int(leverage)})

    # ------------------------- helpers ---------------------------------------
    async def spec(self, symbol: str) -> Dict[str, Any]:
        symbol = symbol.upper()
        if symbol not in self._specs:
            info = (await self.instrument_info(symbol)).get("data") or {}
            self._specs[symbol] = info.get(symbol) or next(iter(info.values()), {}) if info else {}
        return self._specs[symbol]

    async def size_qty(self, coin: str, qty: float, price_usdt: float, reduce_only: bool) -> float:
        """Snap to base_quantity_step_size; entries floored but lifted to min_base_quantity."""
        sp = await self.spec(f"{coin}USDT")
        step = float(sp.get("base_quantity_step_size") or sp.get("lot_size") or 1e-6)
        min_q = float(sp.get("min_base_quantity") or step)
        q = round(qty / step) * step if reduce_only else math.floor(qty / step) * step
        q = max(q, min_q)
        prec = int(sp.get("quantity_precision", 8) or 0)
        return float(f"{q:.{prec}f}")

    @staticmethod
    def order_data(resp: Dict[str, Any]) -> Dict[str, Any]:
        """Order payload lives at data.order (GET status) or data (POST create)."""
        d = resp.get("data") if isinstance(resp, dict) else None
        d = d or {}
        return d.get("order") if isinstance(d.get("order"), dict) else d

    async def wait_for_fill(self, order_id: str, timeout_s: float = 5.0,
                            poll_s: float = 0.5, placed: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Return the terminal order dict. Create returns status RAISED; the exchange executes market orders
        ~400 ms later, so poll at 0.5 s (order endpoints share a 20/min budget → ≤ ~4 GETs per order).
        Terminal: EXECUTED, PARTIALLY_EXECUTED, CANCELLED, REJECTED, EXPIRED.
        """
        terminal = {"EXECUTED", "PARTIALLY_EXECUTED", "CANCELLED", "REJECTED", "EXPIRED"}
        t0 = time.time()
        last: Dict[str, Any] = self.order_data(placed) if placed else {}
        if last.get("status") in terminal:
            self.last_fill_latency_ms = 0
            self.fills_via_create += 1
            return last
        deadline = t0 + timeout_s
        first = True
        while time.time() < deadline:
            await asyncio.sleep(0.45 if first else poll_s); first = False
            try:
                last = self.order_data(await self.get_order_status(order_id)) or last
            except CoinSwitchError:
                continue
            if last.get("status") in terminal:
                self.fills_via_rest += 1
                self.last_fill_latency_ms = round((time.time() - t0) * 1000)
                return last
        return last
