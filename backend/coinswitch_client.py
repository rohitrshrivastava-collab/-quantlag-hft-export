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
import time
import urllib.parse
import uuid
from typing import Any, Dict, Optional

import aiohttp
from cryptography.hazmat.primitives.asymmetric import ed25519


DEFAULT_BASE_URL = "https://coinswitch.co"
EXCHANGE_ID = "EXCHANGE_2"


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
                       body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        headers, signed_path = self._sign(method, path, params)
        url = self.base_url + signed_path
        async with aiohttp.ClientSession(timeout=self._timeout) as s:
            async with s.request(method, url, headers=headers,
                                 data=json.dumps(body) if body is not None else None) as r:
                text = await r.text()
                try:
                    payload = json.loads(text) if text else {}
                except Exception:
                    payload = {"raw": text}
                if r.status >= 400:
                    raise CoinSwitchError(r.status, payload)
                return payload

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

    # ------------------------- helpers ---------------------------------------
    async def wait_for_fill(self, order_id: str, timeout_s: float = 5.0,
                            poll_s: float = 0.25) -> Dict[str, Any]:
        """Poll get_order_status until terminal or timeout.
        Terminal (per docs): EXECUTED, PARTIALLY_EXECUTED, CANCELLED, REJECTED, EXPIRED.
        (For Futures, PARTIALLY_EXECUTED is terminal — the unfilled remainder is dead.)
        """
        terminal = {"EXECUTED", "PARTIALLY_EXECUTED", "CANCELLED", "REJECTED", "EXPIRED"}
        deadline = time.time() + timeout_s
        last: Dict[str, Any] = {}
        while time.time() < deadline:
            try:
                last = await self.get_order_status(order_id)
            except CoinSwitchError:
                await asyncio.sleep(poll_s); continue
            status = (last.get("data", {}) or {}).get("status", "")
            if status in terminal:
                return last
            await asyncio.sleep(poll_s)
        return last
