"""
Lead-Lag Latency Arbitrage Bot Engine
======================================
In-process asynchronous bot that ingests market data (real WebSocket in live mode,
simulated in preview/paper mode), computes the Binance→CoinSwitch PRO price
divergence, and manages the full trade lifecycle for up to 10 coin slots.

Modules encapsulated here:
  - DataIngestion   : Binance / CoinSwitch top-of-book feed (simulator by default)
  - SignalEngine    : Net-spread hurdle gate + directional signal generation
  - PortfolioManager: Dynamic equal-weight capital allocation across 10 slots
  - OrderExecutor   : Paper + Live order lifecycle w/ partial-fill handling
  - TelegramReporter: Trade + hourly summary alerts

The same engine class is imported by:
  1. FastAPI dashboard (server.py) — Mongo-backed
  2. Standalone CLI bot (standalone_bot/run_bot.py) — SQLite-backed
via a pluggable `store` interface.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Dict, List, Optional

import aiohttp

from coinswitch_client import CoinSwitchError, CoinSwitchFuturesClient

logger = logging.getLogger("bot_engine")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DEFAULT_COINS = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "NEAR", "LINK"]

# Realistic spot anchors used by the simulator (USDT).
SEED_PRICES_USDT = {
    "BTC": 68000.0, "ETH": 3500.0, "SOL": 160.0, "BNB": 590.0, "XRP": 0.55,
    "DOGE": 0.14, "ADA": 0.42, "AVAX": 32.0, "NEAR": 5.2, "LINK": 14.5,
}


@dataclass
class BotConfig:
    paper_trading: bool = True
    starting_capital_inr: float = 4000.0
    num_slots: int = 10
    coins: List[str] = field(default_factory=lambda: list(DEFAULT_COINS))
    entry_taker_fee_pct: float = 0.05      # % per side
    exit_taker_fee_pct: float = 0.05
    slippage_buffer_pct: float = 0.03
    hurdle_extra_pct: float = 0.02         # additional edge required beyond fees+slip
    timeout_seconds: int = 45              # 30-60s guard
    stop_loss_pct: float = 0.35            # volatility hard stop
    fx_usdt_inr: float = 84.5              # falls back if REST fails
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    binance_ws_url: str = "wss://fstream.binance.com/stream"
    coinswitch_ws_url: str = ""            # user supplies from CoinSwitch docs
    coinswitch_base_url: str = "https://coinswitch.co"
    coinswitch_api_key: str = ""
    coinswitch_api_secret: str = ""
    live_order_timeout_s: float = 5.0      # max time to wait for fill status
    simulate: bool = True                  # force simulator (no real exchange creds)
    laggard_stale_after_s: float = 10.0    # WS silence before REST fallback / STALE gate

    @classmethod
    def from_env(cls) -> "BotConfig":
        def _f(k, d):
            try: return float(os.environ.get(k, d))
            except Exception: return d
        def _i(k, d):
            try: return int(float(os.environ.get(k, d)))
            except Exception: return d
        def _b(k, d):
            return os.environ.get(k, str(d)).strip().lower() in ("1","true","yes","on")
        return cls(
            paper_trading=_b("PAPER_TRADING", True),
            starting_capital_inr=_f("STARTING_CAPITAL_INR", 4000.0),
            entry_taker_fee_pct=_f("ENTRY_FEE_PCT", 0.05),
            exit_taker_fee_pct=_f("EXIT_FEE_PCT", 0.05),
            slippage_buffer_pct=_f("SLIPPAGE_BUFFER_PCT", 0.03),
            hurdle_extra_pct=_f("HURDLE_EXTRA_PCT", 0.02),
            timeout_seconds=_i("TIMEOUT_SECONDS", 45),
            stop_loss_pct=_f("STOP_LOSS_PCT", 0.35),
            fx_usdt_inr=_f("FX_USDT_INR", 84.5),
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
            coinswitch_ws_url=os.environ.get("COINSWITCH_WS_URL", ""),
            coinswitch_base_url=os.environ.get("COINSWITCH_BASE_URL", "https://coinswitch.co"),
            coinswitch_api_key=os.environ.get("COINSWITCH_API_KEY", ""),
            coinswitch_api_secret=os.environ.get("COINSWITCH_API_SECRET", ""),
            live_order_timeout_s=_f("LIVE_ORDER_TIMEOUT_S", 5.0),
            simulate=_b("SIMULATE_MARKET", True),
            laggard_stale_after_s=_f("LAGGARD_STALE_AFTER_S", 10.0),
        )


# ---------------------------------------------------------------------------
# Domain models
# ---------------------------------------------------------------------------
@dataclass
class TopOfBook:
    coin: str
    bid: float
    ask: float
    ts_ms: int

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0


@dataclass
class Position:
    id: str
    coin: str
    direction: str                 # "LONG" | "SHORT"
    size_inr: float
    contracts: float
    entry_price_inr: float         # CoinSwitch entry
    binance_ref_usdt: float        # leader price at entry
    target_price_inr: float        # catch-up target
    stop_price_inr: float
    opened_at: float               # epoch seconds
    timeout_at: float
    mode: str                      # "PAPER" | "LIVE"
    status: str = "OPEN"           # OPEN | CLOSED
    exit_price_inr: Optional[float] = None
    closed_at: Optional[float] = None
    gross_pnl_inr: float = 0.0
    fees_inr: float = 0.0
    net_pnl_inr: float = 0.0
    close_reason: Optional[str] = None
    entry_order_id: Optional[str] = None
    exit_order_id: Optional[str] = None
    exec_latency_ms: Optional[float] = None


# ---------------------------------------------------------------------------
# In-memory store (fine for dashboard preview; standalone bot swaps in SQLite)
# ---------------------------------------------------------------------------
class MemoryStore:
    def __init__(self) -> None:
        self.open: Dict[str, Position] = {}
        self.closed: List[Position] = []
        self.equity_curve: List[Dict[str, float]] = []
        self.event_log: List[Dict[str, Any]] = []

    # --- open positions ------------------------------------------------------
    async def add_open(self, p: Position) -> None:
        self.open[p.coin] = p

    async def get_open(self, coin: str) -> Optional[Position]:
        return self.open.get(coin)

    async def all_open(self) -> List[Position]:
        return list(self.open.values())

    async def close(self, p: Position) -> None:
        self.open.pop(p.coin, None)
        self.closed.append(p)
        if len(self.closed) > 2000:
            self.closed = self.closed[-2000:]

    async def all_closed(self, limit: int = 500) -> List[Position]:
        return list(reversed(self.closed[-limit:]))

    # --- misc telemetry ------------------------------------------------------
    async def push_equity(self, ts: float, equity: float) -> None:
        self.equity_curve.append({"ts": ts, "equity": equity})
        if len(self.equity_curve) > 5000:
            self.equity_curve = self.equity_curve[-5000:]

    async def get_equity_curve(self) -> List[Dict[str, float]]:
        return list(self.equity_curve)

    async def log(self, level: str, msg: str) -> None:
        self.event_log.append({"ts": time.time(), "level": level, "msg": msg})
        if len(self.event_log) > 500:
            self.event_log = self.event_log[-500:]

    async def recent_logs(self, limit: int = 100) -> List[Dict[str, Any]]:
        return list(reversed(self.event_log[-limit:]))


# ---------------------------------------------------------------------------
# Market data — simulator (dev) + real WebSocket adapter (live/production)
# ---------------------------------------------------------------------------
class MarketSimulator:
    """Generates a plausible Binance leader + CoinSwitch laggard tape.

    The laggard trails the leader by a variable lag (100–1500 ms) plus noise
    so the SignalEngine sees real hurdle-gate opportunities.
    """
    def __init__(self, coins: List[str], fx_provider=None, fx_ref: float = 84.5) -> None:
        self.coins = coins
        self._fx_provider = fx_provider     # callable returning current usdt/inr
        self.fx_ref = fx_ref                # fallback
        self.binance: Dict[str, TopOfBook] = {}
        self.coinswitch: Dict[str, TopOfBook] = {}
        self._leader_series: Dict[str, List[float]] = {c: [SEED_PRICES_USDT[c]] for c in coins}
        self._running = False

    def _fx(self) -> float:
        try:
            v = self._fx_provider() if self._fx_provider else self.fx_ref
            return float(v) if v and 40 < float(v) < 200 else self.fx_ref
        except Exception:
            return self.fx_ref

    async def start(self) -> None:
        self._running = True
        asyncio.create_task(self._tick_loop())

    def stop(self) -> None:
        self._running = False

    async def _tick_loop(self) -> None:
        while self._running:
            now_ms = int(time.time() * 1000)
            for c in self.coins:
                series = self._leader_series[c]
                last = series[-1]
                # small random walk with occasional jumps
                drift = random.gauss(0, last * 0.0004)
                if random.random() < 0.02:
                    drift += random.choice([-1, 1]) * last * random.uniform(0.001, 0.004)
                new_price = max(0.0001, last + drift)
                series.append(new_price)
                if len(series) > 200:
                    self._leader_series[c] = series[-200:]

                spread = new_price * 0.0004
                self.binance[c] = TopOfBook(c, new_price - spread/2, new_price + spread/2, now_ms)

                # laggard mirrors leader with a delay of 3–10 ticks + noise,
                # emitted in INR-scale (× fx_ref) to match real CoinSwitch PRO quotes.
                lag_ticks = random.randint(3, 10)
                lag_usdt = series[-min(len(series), lag_ticks)]
                lag_inr = lag_usdt * self._fx() * (1 + random.gauss(0, 0.0006))
                lag_spread = lag_inr * 0.0006
                self.coinswitch[c] = TopOfBook(c, lag_inr - lag_spread/2, lag_inr + lag_spread/2, now_ms)
            await asyncio.sleep(0.25)

    def leader(self, coin: str) -> Optional[TopOfBook]: return self.binance.get(coin)
    def laggard(self, coin: str) -> Optional[TopOfBook]: return self.coinswitch.get(coin)


class LiveBinanceFeed:
    """Real Binance Futures WebSocket book ticker adapter.
    Used only when SIMULATE_MARKET=false. Falls back silently on error.
    """
    def __init__(self, coins: List[str]) -> None:
        self.coins = coins
        self.book: Dict[str, TopOfBook] = {}
        self._running = False
        self.connected = False
        self.last_msg_ts = 0.0
        self.reconnects = 0
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        self._running = False
        self.connected = False
        if self._task: self._task.cancel()

    def last_msg_age_s(self) -> Optional[float]:
        return (time.time() - self.last_msg_ts) if self.last_msg_ts else None

    async def _run(self) -> None:
        streams = "/".join(f"{c.lower()}usdt@bookTicker" for c in self.coins)
        url = f"wss://fstream.binance.com/stream?streams={streams}"
        backoff = 1
        while self._running:
            try:
                async with aiohttp.ClientSession() as sess:
                    async with sess.ws_connect(url, heartbeat=20) as ws:
                        backoff = 1
                        self.connected = True
                        async for msg in ws:
                            if msg.type != aiohttp.WSMsgType.TEXT: continue
                            self.last_msg_ts = time.time()
                            data = msg.json().get("data", {})
                            sym = data.get("s", "")
                            if not sym.endswith("USDT"): continue
                            coin = sym[:-4]
                            try:
                                self.book[coin] = TopOfBook(
                                    coin=coin,
                                    bid=float(data["b"]),
                                    ask=float(data["a"]),
                                    ts_ms=int(data.get("E", time.time()*1000)),
                                )
                            except Exception:
                                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Binance WS error: {e}; reconnecting in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
            finally:
                self.connected = False
                if self._running: self.reconnects += 1


COINSWITCH_WS_DEFAULT_URL = (
    "wss://ws.coinswitch.co/pro/realtime-rates-socket/futures/exchange_2/"
    "?EIO=4&transport=websocket"
)
COINSWITCH_WS_NAMESPACE = "/exchange_2"
COINSWITCH_WS_BOOK_EVENT = "FETCH_ORDER_BOOK_CS_PRO"


class LiveCoinSwitchBookFeed:
    """CoinSwitch PRO Futures public order-book stream (Socket.IO v4 over raw WS).

    Speaks the Engine.IO/Socket.IO wire protocol directly with aiohttp so no
    extra dependency is needed:
      server "0{...}"        -> we send "40/exchange_2,"          (join namespace)
      server "40/exchange_2" -> we emit subscribe for each coin   ("42/exchange_2,[event,payload]")
      server "2"             -> we reply "3"                        (ping/pong)
      server "42/exchange_2,[FETCH_ORDER_BOOK_CS_PRO,{...}]" -> full book snapshot
    Every push is a FULL snapshot; top-of-book is taken from bids[0]/asks[0].
    Quotes are stored INR-scaled (× fx) to match the engine's laggard convention.
    """
    def __init__(self, coins: List[str], fx_provider, url: str = "") -> None:
        self.coins = coins
        self._fx = fx_provider
        self.url = url or COINSWITCH_WS_DEFAULT_URL
        self.book: Dict[str, TopOfBook] = {}
        self.tick_recv_ts: Dict[str, float] = {}      # coin -> local epoch s of last snapshot
        self.connected = False
        self.subscribed = False
        self.last_msg_ts = 0.0
        self.connected_since = 0.0
        self.reconnects = 0
        self.msg_count = 0
        self.wire_latency_ms: Optional[float] = None  # EMA of (recv - snapshot ts)
        self.last_error: str = ""
        self._rate_window: List[float] = []
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        if self._running: return
        self._running = True
        self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        self._running = False
        self.connected = False
        self.subscribed = False
        if self._task: self._task.cancel()

    # ---- health ------------------------------------------------------------
    def last_msg_age_s(self) -> Optional[float]:
        return (time.time() - self.last_msg_ts) if self.last_msg_ts else None

    def coin_age_ms(self, coin: str) -> Optional[float]:
        t = self.tick_recv_ts.get(coin)
        return (time.time() - t) * 1000.0 if t else None

    def fresh(self, coin: str, max_age_s: float) -> Optional[TopOfBook]:
        t = self.tick_recv_ts.get(coin)
        if not t or (time.time() - t) > max_age_s: return None
        return self.book.get(coin)

    def msgs_per_s(self) -> float:
        cutoff = time.time() - 5.0
        self._rate_window = [t for t in self._rate_window if t >= cutoff]
        return len(self._rate_window) / 5.0

    # ---- wire protocol -------------------------------------------------------
    def _sub_frame(self, coin: str) -> str:
        payload = json.dumps([COINSWITCH_WS_BOOK_EVENT, {"event": "subscribe", "pair": f"{coin}USDT"}])
        return f"42{COINSWITCH_WS_NAMESPACE},{payload}"

    def _on_snapshot(self, d: Dict[str, Any]) -> None:
        d = d.get("data", d) if isinstance(d.get("data"), dict) else d
        sym = str(d.get("symbol") or d.get("s") or "")
        if not sym.endswith("USDT"): return
        coin = sym[:-4]
        bids, asks = d.get("bids") or [], d.get("asks") or []
        if not bids or not asks: return
        try:
            bid, ask = float(bids[0][0]), float(asks[0][0])
        except (TypeError, ValueError, IndexError):
            return
        if bid <= 0 or ask <= 0: return
        now = time.time()
        fx = float(self._fx() or 0) or 1.0
        ts_ms = int(d.get("timestamp") or now * 1000)
        self.book[coin] = TopOfBook(coin=coin, bid=bid * fx, ask=ask * fx, ts_ms=ts_ms)
        self.tick_recv_ts[coin] = now
        lat = max(0.0, now * 1000.0 - ts_ms)
        if lat < 60_000:  # ignore absurd clock-skew values
            self.wire_latency_ms = lat if self.wire_latency_ms is None else 0.9 * self.wire_latency_ms + 0.1 * lat

    async def _run(self) -> None:
        backoff = 1
        while self._running:
            try:
                async with aiohttp.ClientSession() as sess:
                    async with sess.ws_connect(self.url, heartbeat=None) as ws:
                        self.connected = True
                        self.connected_since = time.time()
                        self.last_error = ""
                        while self._running:
                            msg = await ws.receive(timeout=60)
                            if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.ERROR):
                                raise ConnectionError(f"ws closed ({msg.type.name})")
                            if msg.type != aiohttp.WSMsgType.TEXT: continue
                            self.last_msg_ts = time.time()
                            data: str = msg.data
                            if data.startswith("0"):                       # engine.io OPEN
                                await ws.send_str(f"40{COINSWITCH_WS_NAMESPACE},")
                            elif data == "2":                              # ping
                                await ws.send_str("3")
                            elif data.startswith(f"40{COINSWITCH_WS_NAMESPACE}"):  # namespace CONNECT ack
                                for c in self.coins:
                                    await ws.send_str(self._sub_frame(c))
                                self.subscribed = True
                                backoff = 1
                                logger.info(f"CoinSwitch WS subscribed to {len(self.coins)} books")
                            elif data.startswith(f"42{COINSWITCH_WS_NAMESPACE},"):  # EVENT
                                try:
                                    evt = json.loads(data.split(",", 1)[1])
                                except Exception:
                                    continue
                                if len(evt) < 2 or evt[0] != COINSWITCH_WS_BOOK_EVENT or not isinstance(evt[1], dict):
                                    continue
                                if "success" in evt[1] and "bids" not in evt[1]:
                                    continue  # subscription ack
                                self.msg_count += 1
                                self._rate_window.append(self.last_msg_ts)
                                self._on_snapshot(evt[1])
                            elif data.startswith("44"):                    # namespace CONNECT_ERROR
                                raise ConnectionError(f"namespace error: {data[:200]}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.last_error = str(e) or e.__class__.__name__
                logger.warning(f"CoinSwitch WS error: {self.last_error}; reconnecting in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
            finally:
                self.connected = False
                self.subscribed = False
                if self._running: self.reconnects += 1


class FXRateService:
    """Maintains a real-time USDT/INR quote (Binance P2P median as source).
    Falls back to config value if REST fails.
    """
    def __init__(self, fallback: float) -> None:
        self.rate = fallback
        self._fallback = fallback
        self._running = False

    async def start(self) -> None:
        self._running = True
        asyncio.create_task(self._loop())

    def stop(self) -> None: self._running = False

    async def _loop(self) -> None:
        while self._running:
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as s:
                    async with s.get("https://api.coingecko.com/api/v3/simple/price"
                                     "?ids=tether&vs_currencies=inr") as r:
                        if r.status == 200:
                            d = await r.json()
                            rate = float(d.get("tether", {}).get("inr", 0) or 0)
                            if 60 < rate < 130:
                                self.rate = rate
            except Exception:
                pass  # keep last known
            await asyncio.sleep(30)


# ---------------------------------------------------------------------------
# Telegram reporter
# ---------------------------------------------------------------------------
class LiveCoinSwitchTickerPoller:
    """Fallback CoinSwitch PRO laggard feed via authenticated REST ticker polling.

    Real HFT should use a WebSocket book stream — but until that is wired,
    this gives the engine a functional laggard TopOfBook in live mode.

    Cadence: ~0.5s between coin polls (~10 coins => ~5s per coin refresh).
    Well below the 20 req/60s limit on any private endpoint.
    """
    def __init__(self, client, coins: List[str], fx_provider) -> None:
        self.client = client
        self.coins = coins
        self._fx = fx_provider
        self.book: Dict[str, TopOfBook] = {}
        self._running = False
        self._task: Optional[asyncio.Task] = None

    @property
    def running(self) -> bool:
        return self._running

    async def start(self) -> None:
        if self._running: return
        self._running = True
        self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        self._running = False
        if self._task: self._task.cancel()
        self.book.clear()

    async def _run(self) -> None:
        try:
            while self._running:
                for c in self.coins:
                    if not self._running: break
                    try:
                        r = await self.client.ticker(f"{c}USDT")
                        d = (r or {}).get("data", {}) or r or {}
                        bid = float(d.get("bid") or d.get("best_bid") or d.get("bid_price") or 0)
                        ask = float(d.get("ask") or d.get("best_ask") or d.get("ask_price") or 0)
                        if bid <= 0 or ask <= 0: continue
                        fx = float(self._fx() or 0) or 1.0
                        self.book[c] = TopOfBook(
                            coin=c, bid=bid*fx, ask=ask*fx,
                            ts_ms=int(time.time()*1000),
                        )
                    except Exception as e:
                        logger.debug(f"CoinSwitch ticker poll {c} failed: {e}")
                    await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            pass


class TelegramReporter:
    """Outbound-only Telegram Bot API client (sendMessage, HTML parse mode).
    Serialises sends through a lock with a small gap to respect the ~1 msg/s per-chat limit.
    """
    def __init__(self, token: str, chat_id: str) -> None:
        self.token = token
        self.chat_id = chat_id
        self.enabled = bool(token and chat_id)
        self.sent_count = 0
        self.fail_count = 0
        self.last_sent_ts: Optional[float] = None
        self.last_error: str = ""
        self.bot_username: str = ""
        self._lock = asyncio.Lock()

    def status(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "chat_id_masked": (self.chat_id[:2] + "…" + self.chat_id[-3:]) if self.chat_id else "",
            "bot_username": self.bot_username,
            "sent_count": self.sent_count,
            "fail_count": self.fail_count,
            "last_sent_ts": self.last_sent_ts,
            "last_error": self.last_error,
        }

    async def get_me(self) -> Dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not configured")
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as s:
            async with s.get(f"https://api.telegram.org/bot{self.token}/getMe") as r:
                d = await r.json(content_type=None)
        if not d.get("ok"):
            raise RuntimeError(f"getMe failed ({r.status}): {d.get('description', d)}")
        self.bot_username = d["result"].get("username", "")
        return d["result"]

    async def send(self, text: str, raise_on_error: bool = False) -> bool:
        if not self.enabled:
            logger.info(f"[TELEGRAM-MUTED] {text}")
            if raise_on_error:
                raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not configured in backend/.env")
            return False
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": text,
                   "parse_mode": "HTML", "disable_web_page_preview": True}
        async with self._lock:
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as s:
                    async with s.post(url, json=payload) as r:
                        d = await r.json(content_type=None)
                if r.status == 429:
                    retry = float((d.get("parameters") or {}).get("retry_after", 1))
                    await asyncio.sleep(min(retry, 5))
                    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as s:
                        async with s.post(url, json=payload) as r:
                            d = await r.json(content_type=None)
                if not d.get("ok"):
                    raise RuntimeError(f"HTTP {r.status}: {d.get('description', d)}")
                self.sent_count += 1
                self.last_sent_ts = time.time()
                self.last_error = ""
                await asyncio.sleep(0.05)
                return True
            except Exception as e:
                self.fail_count += 1
                self.last_error = str(e)
                logger.warning(f"telegram send failed: {e}")
                if raise_on_error: raise
                return False


# ---------------------------------------------------------------------------
# Core engine
# ---------------------------------------------------------------------------
class BotEngine:
    def __init__(self, cfg: BotConfig, store: Optional[MemoryStore] = None) -> None:
        self.cfg = cfg
        self.store = store or MemoryStore()
        self.fx = FXRateService(cfg.fx_usdt_inr)
        self.simulator = MarketSimulator(cfg.coins, fx_provider=lambda: self.fx.rate, fx_ref=cfg.fx_usdt_inr) if cfg.simulate else None
        self.live_feed = None if cfg.simulate else LiveBinanceFeed(cfg.coins)
        # Public CoinSwitch book WS — no keys needed, powers PAPER + LIVE when not simulating
        self.coinswitch_feed: Optional[LiveCoinSwitchBookFeed] = None if cfg.simulate else \
            LiveCoinSwitchBookFeed(cfg.coins, lambda: self.fx.rate, url=cfg.coinswitch_ws_url)
        self.telegram = TelegramReporter(cfg.telegram_bot_token, cfg.telegram_chat_id)
        # CoinSwitch REST client — only built when we have both keys AND we're
        # about to trade for real (paper mode still uses simulated fills).
        self.coinswitch: Optional[CoinSwitchFuturesClient] = None
        self.coinswitch_poller: Optional[LiveCoinSwitchTickerPoller] = None  # REST fallback when WS stale
        if cfg.coinswitch_api_key and cfg.coinswitch_api_secret:
            try:
                self.coinswitch = CoinSwitchFuturesClient(
                    api_key=cfg.coinswitch_api_key,
                    secret_key=cfg.coinswitch_api_secret,
                    base_url=cfg.coinswitch_base_url,
                )
                self.coinswitch_poller = LiveCoinSwitchTickerPoller(
                    self.coinswitch, cfg.coins, lambda: self.fx.rate)
            except Exception as e:
                logger.warning(f"CoinSwitch client init failed (keys malformed?): {e}")
        self.running = False
        self.started_at: Optional[float] = None
        self.equity_inr = cfg.starting_capital_inr
        self.realized_pnl_inr = 0.0
        self.trade_count = 0
        self.win_count = 0
        self.loss_count = 0
        self.total_hold_sec = 0.0
        self.total_exec_latency_ms = 0.0
        self._tasks: List[asyncio.Task] = []
        self._last_hourly_ts = time.time()
        self._last_signal_state: Dict[str, str] = {c: "WAITING" for c in cfg.coins}
        self._last_spread_pct: Dict[str, float] = {c: 0.0 for c in cfg.coins}
        # sparklines: last 60 spread% points per coin
        self._sparkline: Dict[str, List[float]] = {c: [] for c in cfg.coins}
        # heatmap: coin -> {epoch_sec: signed max |spread|/hurdle ratio in that second}
        self._heat: Dict[str, Dict[int, float]] = {c: {} for c in cfg.coins}
        self._live_missing_keys_warned = False
        self._stale_no_keys_warned = False

    # --- lifecycle -----------------------------------------------------------
    async def start(self) -> None:
        if self.running: return
        self.running = True
        self.started_at = time.time()
        if self.simulator: await self.simulator.start()
        if self.live_feed: await self.live_feed.start()
        if self.coinswitch_feed: await self.coinswitch_feed.start()
        await self.fx.start()
        self._tasks = [
            asyncio.create_task(self._scan_loop()),
            asyncio.create_task(self._position_manager_loop()),
            asyncio.create_task(self._equity_ticker_loop()),
            asyncio.create_task(self._hourly_summary_loop()),
        ]
        if self.coinswitch_feed:
            self._tasks.append(asyncio.create_task(self._feed_watchdog_loop()))
        feed = "SIMULATOR" if self.simulator else "Binance WS + CoinSwitch book WS"
        await self.store.log("INFO", f"Bot started in {'PAPER' if self.cfg.paper_trading else 'LIVE'} mode · feed: {feed}")

    async def stop(self) -> None:
        if not self.running: return
        self.running = False
        for t in self._tasks: t.cancel()
        self._tasks = []
        if self.simulator: self.simulator.stop()
        if self.live_feed: self.live_feed.stop()
        if self.coinswitch_feed: self.coinswitch_feed.stop()
        if self.coinswitch_poller: self.coinswitch_poller.stop()
        self.fx.stop()
        await self.store.log("INFO", "Bot stopped")

    # --- accessors -----------------------------------------------------------
    def _leader(self, coin: str) -> Optional[TopOfBook]:
        return self.simulator.leader(coin) if self.simulator else self.live_feed.book.get(coin)

    def _laggard(self, coin: str) -> Optional[TopOfBook]:
        if self.simulator: return self.simulator.laggard(coin)
        if self.coinswitch_feed:
            tob = self.coinswitch_feed.fresh(coin, self.cfg.laggard_stale_after_s)
            if tob: return tob
        if self.coinswitch_poller and self.coinswitch_poller.running:
            return self.coinswitch_poller.book.get(coin)
        return None

    def _feed_stale(self) -> bool:
        age = self.coinswitch_feed.last_msg_age_s() if self.coinswitch_feed else None
        if age is None:
            # never received anything: stale once we've waited past the threshold since start
            return bool(self.started_at) and (time.time() - self.started_at) > self.cfg.laggard_stale_after_s
        return age > self.cfg.laggard_stale_after_s

    async def _feed_watchdog_loop(self) -> None:
        """Flip the REST ticker poller on while the CoinSwitch WS is stale, off when it recovers."""
        try:
            while self.running:
                stale = self._feed_stale()
                if stale and self.coinswitch and not self.coinswitch_poller.running:
                    await self.coinswitch_poller.start()
                    await self.store.log("WARN", "CoinSwitch WS stale >%.0fs — falling back to REST ticker poller" % self.cfg.laggard_stale_after_s)
                elif stale and not self.coinswitch and not self._stale_no_keys_warned:
                    await self.store.log("WARN", "CoinSwitch WS stale and no API keys for REST fallback — laggard quotes marked STALE, entries paused")
                    self._stale_no_keys_warned = True
                elif not stale:
                    if self.coinswitch_poller and self.coinswitch_poller.running:
                        self.coinswitch_poller.stop()
                        await self.store.log("INFO", "CoinSwitch WS recovered — REST fallback poller stopped")
                    self._stale_no_keys_warned = False
                await asyncio.sleep(2)
        except asyncio.CancelledError: pass

    def laggard_source(self) -> str:
        if self.simulator: return "SIMULATOR"
        if self.coinswitch_feed and not self._feed_stale(): return "COINSWITCH_WS"
        if self.coinswitch_poller and self.coinswitch_poller.running: return "REST_POLLER"
        return "STALE"

    def feed_health(self) -> Dict[str, Any]:
        f = self.coinswitch_feed
        lf = self.live_feed
        def _r(v, n=1): return None if v is None else round(v, n)
        return {
            "source": self.laggard_source(),
            "simulate": self.cfg.simulate,
            "stale_after_s": self.cfg.laggard_stale_after_s,
            "laggard": None if not f else {
                "connected": f.connected, "subscribed": f.subscribed,
                "last_msg_age_s": _r(f.last_msg_age_s(), 2),
                "wire_latency_ms": _r(f.wire_latency_ms, 0),
                "msgs_per_s": _r(f.msgs_per_s(), 1),
                "msg_count": f.msg_count, "reconnects": f.reconnects,
                "last_error": f.last_error,
                "coins": [{"coin": c, "age_ms": _r(f.coin_age_ms(c), 0),
                           "stale": f.fresh(c, self.cfg.laggard_stale_after_s) is None} for c in self.cfg.coins],
            },
            "leader": None if not lf else {
                "connected": lf.connected, "last_msg_age_s": _r(lf.last_msg_age_s(), 2),
                "reconnects": lf.reconnects, "coins_with_quotes": len(lf.book),
            },
            "rest_fallback": {
                "available": self.coinswitch is not None,
                "active": bool(self.coinswitch_poller and self.coinswitch_poller.running),
            },
        }

    def hurdle_pct(self) -> float:
        c = self.cfg
        return c.entry_taker_fee_pct + c.exit_taker_fee_pct + c.slippage_buffer_pct + c.hurdle_extra_pct

    def allocated_per_slot_inr(self) -> float:
        n = max(1, self.cfg.num_slots)
        return self.equity_inr / n

    # --- heatmap ---------------------------------------------------------------
    HEAT_WINDOW_S = 60
    NEAR_HURDLE_RATIO = 0.8

    def _record_heat(self, coin: str, gross_pct: float) -> None:
        ratio = gross_pct / max(self.hurdle_pct(), 1e-9)
        sec = int(time.time())
        buckets = self._heat[coin]
        prev = buckets.get(sec, 0.0)
        if abs(ratio) >= abs(prev): buckets[sec] = ratio
        if len(buckets) > self.HEAT_WINDOW_S + 5:
            cutoff = sec - self.HEAT_WINDOW_S
            for k in [k for k in buckets if k < cutoff]: del buckets[k]

    def _heat_series(self, coin: str) -> List[float]:
        now = int(time.time())
        b = self._heat.get(coin, {})
        return [round(b.get(s, 0.0), 3) for s in range(now - self.HEAT_WINDOW_S + 1, now + 1)]

    # --- signal / scan -------------------------------------------------------
    async def _scan_loop(self) -> None:
        try:
            while self.running:
                for coin in self.cfg.coins:
                    await self._scan_coin(coin)
                await asyncio.sleep(0.4)
        except asyncio.CancelledError: pass

    async def _scan_coin(self, coin: str) -> None:
        leader = self._leader(coin); lagg = self._laggard(coin)
        if not leader or not lagg: return
        fx = max(self.fx.rate, 1e-6)
        # convert leader USDT mid → INR reference for like-with-like comparison
        leader_inr_mid = leader.mid * fx
        # gross divergence % vs laggard mid
        gross_pct = (leader_inr_mid - lagg.mid) / lagg.mid * 100.0
        self._last_spread_pct[coin] = gross_pct
        sp = self._sparkline[coin]; sp.append(gross_pct)
        if len(sp) > 60: self._sparkline[coin] = sp[-60:]
        self._record_heat(coin, gross_pct)

        # skip if slot already occupied
        if await self.store.get_open(coin):
            self._last_signal_state[coin] = "IN_TRADE"
            return

        hurdle = self.hurdle_pct()
        if gross_pct > hurdle:
            self._last_signal_state[coin] = "HURDLE_MET_LONG"
            await self._open_position(coin, "LONG", leader, lagg, leader_inr_mid)
        elif -gross_pct > hurdle:
            self._last_signal_state[coin] = "HURDLE_MET_SHORT"
            await self._open_position(coin, "SHORT", leader, lagg, leader_inr_mid)
        else:
            self._last_signal_state[coin] = "WAITING"

    # --- order lifecycle -----------------------------------------------------
    async def _open_position(self, coin: str, direction: str,
                             leader: TopOfBook, lagg: TopOfBook,
                             leader_inr_mid: float) -> None:
        signal_ts = time.time()
        size_inr = min(self.allocated_per_slot_inr(), self.equity_inr * 0.15)
        if size_inr < 50: return  # min lot guard
        # Entry price (paper) = worst top-of-book (taker)
        entry_price = lagg.ask if direction == "LONG" else lagg.bid
        contracts = size_inr / entry_price
        target = leader_inr_mid
        stop = entry_price * (1 - self.cfg.stop_loss_pct/100.0) if direction == "LONG" \
               else entry_price * (1 + self.cfg.stop_loss_pct/100.0)
        latency_ms = 0.0
        entry_order_id: Optional[str] = None
        entry_fee_inr = 0.0

        if self.cfg.paper_trading:
            latency_ms = random.uniform(80, 220)
            await asyncio.sleep(latency_ms/1000.0)
        else:
            # -------- LIVE: place MARKET order on CoinSwitch PRO Futures --------
            if not self.coinswitch:
                if not self._live_missing_keys_warned:
                    await self.store.log("ERROR", "[LIVE] CoinSwitch keys missing — no orders will be placed until COINSWITCH_API_KEY/SECRET are set. Suppressing further per-coin warnings.")
                    self._live_missing_keys_warned = True
                return
            fx = max(self.fx.rate, 1e-6)
            # base-asset quantity (e.g. BTC units). Use Binance USDT price as ref
            # since it's the freshest; CoinSwitch fill will get its own avg price.
            usdt_size = size_inr / fx
            qty = usdt_size / max(leader.mid, 1e-9)
            symbol = f"{coin}USDT"
            side = "BUY" if direction == "LONG" else "SELL"
            t0 = time.perf_counter()
            try:
                placed = await self.coinswitch.place_order(
                    symbol=symbol, side=side, order_type="MARKET",
                    quantity=qty, reduce_only=False,
                )
                entry_order_id = (placed.get("data") or {}).get("order_id")
                if not entry_order_id:
                    await self.store.log("ERROR", f"[LIVE] {coin} place_order missing order_id: {placed}")
                    return
                filled = await self.coinswitch.wait_for_fill(
                    entry_order_id, timeout_s=self.cfg.live_order_timeout_s)
            except CoinSwitchError as e:
                await self.store.log("ERROR", f"[LIVE] {coin} order rejected {e.status}: {e.body}")
                return
            except Exception as e:
                await self.store.log("ERROR", f"[LIVE] {coin} order exception: {e}")
                return
            latency_ms = (time.perf_counter() - t0) * 1000.0
            d = (filled.get("data") or {})
            exec_qty = float(d.get("exec_quantity") or 0)
            avg_px = float(d.get("avg_execution_price") or 0)
            fee_usdt = float(d.get("execution_fee") or 0)
            status = d.get("status", "")
            if exec_qty <= 0 or avg_px <= 0:
                await self.store.log(
                    "WARN", f"[LIVE] {coin} order {status} exec_qty=0 order_id={entry_order_id}")
                return
            # translate to engine's INR bookkeeping using current FX
            contracts = exec_qty                          # base-asset units
            entry_price = avg_px * fx                     # INR-equivalent entry
            size_inr = exec_qty * avg_px * fx             # actual filled INR notional
            entry_fee_inr = fee_usdt * fx
            target = leader_inr_mid
            stop = entry_price * (1 - self.cfg.stop_loss_pct/100.0) if direction == "LONG" \
                   else entry_price * (1 + self.cfg.stop_loss_pct/100.0)
            await self.store.log(
                "INFO",
                f"[LIVE] {coin} filled {exec_qty} @ {avg_px} USDT ({status}) fee={fee_usdt} USDT")

        pos = Position(
            id=str(uuid.uuid4()),
            coin=coin, direction=direction,
            size_inr=size_inr, contracts=contracts,
            entry_price_inr=entry_price,
            binance_ref_usdt=leader.mid,
            target_price_inr=target,
            stop_price_inr=stop,
            opened_at=signal_ts,
            timeout_at=signal_ts + self.cfg.timeout_seconds,
            mode="PAPER" if self.cfg.paper_trading else "LIVE",
            exec_latency_ms=latency_ms,
            entry_order_id=entry_order_id,
            fees_inr=entry_fee_inr,
        )
        await self.store.add_open(pos)
        await self.store.log("TRADE", f"OPEN {direction} {coin} ₹{size_inr:.2f} @ {entry_price:.6f}")
        await self.telegram.send(
            f"🟢 <b>OPEN {direction}</b> {coin}\n"
            f"Size: ₹{size_inr:.2f} ({contracts:.6f} contracts)\n"
            f"Entry: {entry_price:.6f} | Target: {target:.6f}\n"
            f"Mode: {pos.mode}"
        )

    async def _position_manager_loop(self) -> None:
        try:
            while self.running:
                for pos in await self.store.all_open():
                    await self._manage_position(pos)
                await asyncio.sleep(0.3)
        except asyncio.CancelledError: pass

    async def _manage_position(self, pos: Position) -> None:
        lagg = self._laggard(pos.coin)
        if not lagg: return
        now = time.time()
        # mark-to-market
        cur = lagg.bid if pos.direction == "LONG" else lagg.ask
        reason = None
        if pos.direction == "LONG":
            if cur >= pos.target_price_inr: reason = "TARGET_HIT"
            elif cur <= pos.stop_price_inr: reason = "STOP_LOSS"
        else:
            if cur <= pos.target_price_inr: reason = "TARGET_HIT"
            elif cur >= pos.stop_price_inr: reason = "STOP_LOSS"
        if not reason and now >= pos.timeout_at: reason = "TIMEOUT"
        if reason: await self._close_position(pos, cur, reason)

    async def _close_position(self, pos: Position, price: float, reason: str) -> None:
        exit_fee_inr: Optional[float] = None
        exit_order_id: Optional[str] = None
        if pos.mode == "LIVE" and self.coinswitch:
            # place reduce_only MARKET on opposite side
            symbol = f"{pos.coin}USDT"
            side = "SELL" if pos.direction == "LONG" else "BUY"
            try:
                placed = await self.coinswitch.place_order(
                    symbol=symbol, side=side, order_type="MARKET",
                    quantity=pos.contracts, reduce_only=True,
                )
                exit_order_id = (placed.get("data") or {}).get("order_id")
                if exit_order_id:
                    filled = await self.coinswitch.wait_for_fill(
                        exit_order_id, timeout_s=self.cfg.live_order_timeout_s)
                    d = (filled.get("data") or {})
                    exec_qty = float(d.get("exec_quantity") or 0)
                    avg_px = float(d.get("avg_execution_price") or 0)
                    fee_usdt = float(d.get("execution_fee") or 0)
                    if exec_qty > 0 and avg_px > 0:
                        fx = max(self.fx.rate, 1e-6)
                        price = avg_px * fx
                        exit_fee_inr = fee_usdt * fx
                        # partial-fill: shrink contracts to what actually closed
                        if exec_qty < pos.contracts:
                            pos.contracts = exec_qty
                    await self.store.log(
                        "INFO",
                        f"[LIVE] {pos.coin} CLOSE fill exec={exec_qty} avg={avg_px} fee={fee_usdt} USDT")
            except CoinSwitchError as e:
                await self.store.log("ERROR", f"[LIVE] {pos.coin} close rejected {e.status}: {e.body}")
            except Exception as e:
                await self.store.log("ERROR", f"[LIVE] {pos.coin} close exception: {e}")

        pos.exit_price_inr = price
        pos.closed_at = time.time()
        pos.close_reason = reason
        pos.exit_order_id = exit_order_id
        if pos.direction == "LONG":
            gross = (price - pos.entry_price_inr) * pos.contracts
        else:
            gross = (pos.entry_price_inr - price) * pos.contracts
        if pos.mode == "LIVE":
            entry_fee = pos.fees_inr or 0.0  # captured at open
            fees = entry_fee + (exit_fee_inr if exit_fee_inr is not None
                                else pos.size_inr * self.cfg.exit_taker_fee_pct/100.0)
        else:
            fees = pos.size_inr * (self.cfg.entry_taker_fee_pct + self.cfg.exit_taker_fee_pct) / 100.0
        pos.gross_pnl_inr = gross
        pos.fees_inr = fees
        pos.net_pnl_inr = gross - fees
        pos.status = "CLOSED"
        self.equity_inr += pos.net_pnl_inr
        self.realized_pnl_inr += pos.net_pnl_inr
        self.trade_count += 1
        if pos.net_pnl_inr >= 0: self.win_count += 1
        else: self.loss_count += 1
        self.total_hold_sec += (pos.closed_at - pos.opened_at)
        if pos.exec_latency_ms: self.total_exec_latency_ms += pos.exec_latency_ms
        await self.store.close(pos)
        await self.store.log(
            "TRADE",
            f"CLOSE {pos.direction} {pos.coin} @ {price:.6f} ({reason}) net ₹{pos.net_pnl_inr:+.2f}"
        )
        emoji = "🟩" if pos.net_pnl_inr >= 0 else "🟥"
        await self.telegram.send(
            f"{emoji} <b>CLOSE {pos.direction}</b> {pos.coin} ({reason})\n"
            f"Exit: {price:.6f} | Net PnL: ₹{pos.net_pnl_inr:+.2f}\n"
            f"Gross: ₹{gross:+.2f} | Fees: ₹{fees:.2f}\n"
            f"Mode: {pos.mode}"
        )

    # --- equity + hourly reporting ------------------------------------------
    async def _equity_ticker_loop(self) -> None:
        try:
            while self.running:
                await self.store.push_equity(time.time(), self.equity_inr)
                await asyncio.sleep(5)
        except asyncio.CancelledError: pass

    async def _hourly_summary_loop(self) -> None:
        try:
            while self.running:
                await asyncio.sleep(3600)
                await self._send_hourly_summary()
        except asyncio.CancelledError: pass

    async def _send_hourly_summary(self, raise_on_error: bool = False) -> None:
        wr = (self.win_count / self.trade_count * 100.0) if self.trade_count else 0.0
        avg_hold = self.total_hold_sec / self.trade_count if self.trade_count else 0.0
        avg_lat = self.total_exec_latency_ms / self.trade_count if self.trade_count else 0.0
        msg = (
            f"📊 <b>Hourly Summary</b>\n"
            f"Trades: {self.trade_count}  |  Win rate: {wr:.1f}%\n"
            f"Realized PnL: ₹{self.realized_pnl_inr:+.2f}\n"
            f"Equity: ₹{self.equity_inr:.2f}\n"
            f"Avg hold: {avg_hold:.1f}s  |  Avg exec latency: {avg_lat:.0f}ms\n"
            f"Mode: {'PAPER' if self.cfg.paper_trading else 'LIVE'}"
        )
        await self.telegram.send(msg, raise_on_error=raise_on_error)

    # --- snapshot for API ----------------------------------------------------
    def snapshot(self) -> Dict[str, Any]:
        wr = (self.win_count / self.trade_count * 100.0) if self.trade_count else 0.0
        avg_hold = self.total_hold_sec / self.trade_count if self.trade_count else 0.0
        avg_lat = self.total_exec_latency_ms / self.trade_count if self.trade_count else 0.0
        return {
            "running": self.running,
            "mode": "PAPER" if self.cfg.paper_trading else "LIVE",
            "started_at": self.started_at,
            "equity_inr": round(self.equity_inr, 2),
            "starting_capital_inr": self.cfg.starting_capital_inr,
            "realized_pnl_inr": round(self.realized_pnl_inr, 2),
            "free_margin_inr": round(self.equity_inr - sum(p.size_inr for p in self.store.open.values()), 2),
            "allocated_per_slot_inr": round(self.allocated_per_slot_inr(), 2),
            "num_slots": self.cfg.num_slots,
            "hurdle_pct": round(self.hurdle_pct(), 3),
            "trades": self.trade_count,
            "wins": self.win_count,
            "losses": self.loss_count,
            "win_rate": round(wr, 2),
            "avg_hold_sec": round(avg_hold, 2),
            "avg_exec_latency_ms": round(avg_lat, 1),
            "fx_usdt_inr": round(self.fx.rate, 4),
        }

    def spreads_snapshot(self) -> List[Dict[str, Any]]:
        out = []
        for c in self.cfg.coins:
            leader = self._leader(c); lagg = self._laggard(c)
            heat = self._heat_series(c)
            out.append({
                "coin": c,
                "binance_bid": round(leader.bid, 6) if leader else None,
                "binance_ask": round(leader.ask, 6) if leader else None,
                "coinswitch_bid": round(lagg.bid, 6) if lagg else None,
                "coinswitch_ask": round(lagg.ask, 6) if lagg else None,
                "gross_spread_pct": round(self._last_spread_pct.get(c, 0.0), 4),
                "signal_state": self._last_signal_state.get(c, "WAITING"),
                "in_trade": c in self.store.open,
                "sparkline": list(self._sparkline.get(c, [])),
                "heat": heat,
                "near_hurdle_hits_60s": sum(1 for r in heat if abs(r) >= self.NEAR_HURDLE_RATIO),
                "peak_ratio_60s": round(max((abs(r) for r in heat), default=0.0), 3),
            })
        return out
