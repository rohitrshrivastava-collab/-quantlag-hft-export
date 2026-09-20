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
import logging
import math
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


class CoinDCXUserStream:
    """Authenticated CoinDCX Socket.IO (EIO=3) stream on channel `coindcx`.
    Pushes `df-order-update` / `df-position-update` so fills resolve without REST polling."""
    URL = "wss://stream.coindcx.com/socket.io/?EIO=3&transport=websocket"
    logger = logging.getLogger("coindcx.user_ws")

    def __init__(self, api_key: str, api_secret: str) -> None:
        self.api_key = api_key
        self._secret = api_secret.encode()
        self.orders: Dict[str, Dict[str, Any]] = {}          # order_id -> latest update
        self.order_recv_ts: Dict[str, float] = {}
        self._events: Dict[str, asyncio.Event] = {}
        self.last_position: Optional[Dict[str, Any]] = None
        self.connected = False
        self.joined = False
        self.last_msg_ts = 0.0
        self.order_updates = 0
        self.position_updates = 0
        self.reconnects = 0
        self.last_error = ""
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        if self._running: return
        self._running = True
        self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        self._running = False
        self.connected = self.joined = False
        if self._task: self._task.cancel()

    def watch(self, order_id: str) -> asyncio.Event:
        return self._events.setdefault(order_id, asyncio.Event())

    def unwatch(self, order_id: str) -> None:
        self._events.pop(order_id, None)

    def _join_frame(self) -> str:
        sig = hmac.new(self._secret, b'{"channel":"coindcx"}', hashlib.sha256).hexdigest()
        return "42" + json.dumps(["join", {"channelName": "coindcx", "authSignature": sig, "apiKey": self.api_key}])

    @staticmethod
    def _items(payload: Any) -> List[Dict[str, Any]]:
        d = payload.get("data") if isinstance(payload, dict) and "data" in payload else payload
        if isinstance(d, str):
            try: d = json.loads(d)
            except Exception: return []
        if isinstance(d, dict): return [d]
        return [x for x in d if isinstance(x, dict)] if isinstance(d, list) else []

    def _on_event(self, name: str, payload: Any) -> None:
        if name == "df-order-update":
            for o in self._items(payload):
                oid = str(o.get("id") or "")
                if not oid: continue
                self.orders[oid] = o
                self.order_recv_ts[oid] = time.time()
                self.order_updates += 1
                ev = self._events.get(oid)
                if ev: ev.set()
            if len(self.orders) > 500:
                for k in sorted(self.order_recv_ts, key=self.order_recv_ts.get)[:250]:
                    self.orders.pop(k, None); self.order_recv_ts.pop(k, None)
        elif name == "df-position-update":
            items = self._items(payload)
            if items:
                self.last_position = items[-1]
                self.position_updates += 1

    async def _run(self) -> None:
        backoff = 1
        while self._running:
            try:
                async with aiohttp.ClientSession() as sess:
                    async with sess.ws_connect(self.URL, heartbeat=None) as ws:
                        self.connected = True; self.last_error = ""
                        last_ping = time.time()
                        while self._running:
                            if time.time() - last_ping > 20:
                                await ws.send_str("2"); last_ping = time.time()
                            msg = await ws.receive(timeout=30)
                            if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.ERROR):
                                raise ConnectionError(f"ws closed ({msg.type.name})")
                            if msg.type != aiohttp.WSMsgType.TEXT: continue
                            self.last_msg_ts = time.time()
                            data: str = msg.data
                            if data == "40":
                                await ws.send_str(self._join_frame())
                                self.joined = True; backoff = 1
                                self.logger.info("CoinDCX user stream joined channel coindcx")
                            elif data.startswith("42"):
                                try: evt = json.loads(data[2:])
                                except Exception: continue
                                if isinstance(evt, list) and len(evt) >= 2:
                                    self._on_event(str(evt[0]), evt[1])
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.last_error = str(e) or e.__class__.__name__
                self.logger.warning(f"CoinDCX user WS error: {self.last_error}; reconnecting in {backoff}s")
                await asyncio.sleep(backoff); backoff = min(backoff * 2, 30)
            finally:
                self.connected = self.joined = False
                if self._running: self.reconnects += 1

    def status(self) -> Dict[str, Any]:
        return {"connected": self.connected, "joined": self.joined,
                "last_msg_age_s": round(time.time() - self.last_msg_ts, 1) if self.last_msg_ts else None,
                "order_updates": self.order_updates, "position_updates": self.position_updates,
                "reconnects": self.reconnects, "last_error": self.last_error}


class CoinDCXFuturesClient:
    def __init__(self, api_key: str, api_secret: str, base_url: str = BASE_URL, timeout_s: float = 10.0,
                 margin_currency: str = "USDT") -> None:
        if not api_key or not api_secret:
            raise ValueError("api_key and api_secret are required")
        self.api_key = api_key
        self._secret = api_secret.encode()
        self.base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        self.margin_currency = (margin_currency or "USDT").upper()   # INR or USDT margined futures
        self.limiter = RateLimiterGroup(600)   # 16 req/s general; stay well below
        self.http_429_count = 0
        self.user_stream: Optional[CoinDCXUserStream] = None
        self.fills_via_ws = 0
        self.fills_via_rest = 0
        self.last_fill_latency_ms: Optional[float] = None
        self.last_fill_source: str = ""
        self._specs: Dict[str, Dict[str, Any]] = {}

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

    async def autodetect_margin_currency(self) -> str:
        """Pick INR or USDT — whichever futures wallet holds more free balance."""
        wallets = await self.wallet_balance()
        best, best_bal = self.margin_currency, -1.0
        for w in wallets if isinstance(wallets, list) else []:
            cur = str(w.get("currency_short_name", "")).upper()
            if cur in ("INR", "USDT"):
                bal = float(w.get("balance") or 0)
                if bal > best_bal: best, best_bal = cur, bal
        self.margin_currency = best
        return best

    async def positions(self, pairs: Optional[List[str]] = None) -> Any:
        body: Dict[str, Any] = {"page": "1", "size": "50", "margin_currency_short_name": [self.margin_currency]}
        if pairs: body["pairs"] = ",".join(pairs)
        return await self._request("POST", "/exchange/v1/derivatives/futures/positions", body)

    async def update_leverage(self, coin: str, leverage: int) -> Any:
        return await self._request("POST", "/exchange/v1/derivatives/futures/positions/update_leverage",
                                   {"pair": self.pair(coin), "leverage": str(int(leverage)),
                                    "margin_currency_short_name": self.margin_currency})

    async def market_order(self, coin: str, side: str, quantity: float, leverage: int) -> Dict[str, Any]:
        res = await self._request("POST", "/exchange/v1/derivatives/futures/orders/create", {
            "order": {"side": side.lower(), "pair": self.pair(coin), "order_type": "market_order",
                      "total_quantity": quantity, "leverage": int(leverage),
                      "notification": "no_notification", "margin_currency_short_name": self.margin_currency}})
        return res[0] if isinstance(res, list) and res else res

    async def cancel_order(self, order_id: str) -> Any:
        return await self._request("POST", "/exchange/v1/derivatives/futures/orders/cancel", {"id": order_id})

    async def list_orders(self, status: str, side: str) -> List[Dict[str, Any]]:
        res = await self._request("POST", "/exchange/v1/derivatives/futures/orders",
                                  {"status": status, "side": side.lower(), "page": "1", "size": "50",
                                   "margin_currency_short_name": [self.margin_currency]})
        return res if isinstance(res, list) else (res.get("data") or [])

    async def get_order(self, order_id: str) -> Optional[Dict[str, Any]]:
        res = await self._request("POST", "/exchange/v1/derivatives/futures/orders",
                                  {"id": order_id, "margin_currency_short_name": [self.margin_currency]})
        items = res if isinstance(res, list) else (res.get("data") or [])
        return next((o for o in items if o.get("id") == order_id), items[0] if items else None)

    async def wait_for_fill(self, order_id: str, side: str, timeout_s: float = 5.0, poll_s: float = 0.25) -> Dict[str, Any]:
        """Resolve via the authenticated WS when joined (REST poll only as a 1 s safety net), else poll REST."""
        t0 = time.time()
        deadline = t0 + timeout_s
        last: Dict[str, Any] = {}
        ws = self.user_stream if (self.user_stream and self.user_stream.joined) else None
        ev = ws.watch(order_id) if ws else None
        rest_gap = 1.0 if ws else poll_s
        next_poll = t0 + rest_gap if ws else t0
        try:
            while True:
                now = time.time()
                if ws:
                    o = ws.orders.get(order_id)
                    if o:
                        last = o
                        if str(o.get("status", "")).lower() in TERMINAL:
                            self.fills_via_ws += 1; self.last_fill_source = "WS"
                            self.last_fill_latency_ms = round((ws.order_recv_ts.get(order_id, now) - t0) * 1000)
                            return o
                if now >= deadline: break
                if now >= next_poll:
                    try:
                        o = await self.get_order(order_id)
                        if o:
                            last = o
                            if str(o.get("status", "")).lower() in TERMINAL:
                                self.fills_via_rest += 1; self.last_fill_source = "REST"
                                self.last_fill_latency_ms = round((time.time() - t0) * 1000)
                                return o
                    except CoinDCXError:
                        pass
                    next_poll = time.time() + rest_gap
                if ev:
                    try:
                        await asyncio.wait_for(ev.wait(), timeout=min(0.25, max(0.01, deadline - time.time())))
                    except asyncio.TimeoutError:
                        pass
                    ev.clear()
                else:
                    await asyncio.sleep(poll_s)
        finally:
            if ws: ws.unwatch(order_id)
        return last

    async def size_qty(self, coin: str, qty: float, price_usdt: float, reduce_only: bool) -> float:
        """Snap qty to the instrument's quantity_increment; entries are floored but lifted to min_notional×1.05."""
        spec = self._specs.get(coin)
        if not spec:
            spec = self._specs[coin] = await self.instrument(coin)
        step = float(spec.get("quantity_increment") or 1e-6)
        min_qty = float(spec.get("min_quantity") or step)
        if reduce_only:
            q = round(qty / step) * step
        else:
            q = math.floor(qty / step) * step
            min_notional = float(spec.get("min_notional") or 6) * 1.05
            if price_usdt > 0 and q * price_usdt < min_notional:
                q = math.ceil((min_notional / price_usdt) / step) * step
        q = max(q, min_qty)
        return float(f"{q:.10g}")

    async def instrument(self, coin: str) -> Dict[str, Any]:
        """Public instrument details: min_notional, quantity_increment, taker_fee, max leverage…"""
        url = f"{self.base_url}/exchange/v1/derivatives/futures/data/instrument?pair={self.pair(coin)}&margin_currency_short_name={self.margin_currency}"
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
