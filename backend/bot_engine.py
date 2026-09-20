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
from pathlib import Path
import random
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

import aiohttp

from coinswitch_client import CoinSwitchError, CoinSwitchFuturesClient
from coindcx_client import CoinDCXError, CoinDCXFuturesClient, CoinDCXUserStream
from liquidity import assess
from replay import TickRecorder
from settings_store import apply_settings, load_settings, save_settings

logger = logging.getLogger("bot_engine")


class SlotTooSmall(Exception):
    """Exchange minimum order size is far above the slot's notional."""


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
    leverage: int = 1                      # notional = slot margin × leverage
    laggard_exchange: str = "coinswitch"   # coinswitch | coindcx
    coindcx_api_key: str = ""
    coindcx_api_secret: str = ""
    basis_filter: bool = True              # subtract rolling median basis from spread
    basis_window_s: int = 300
    digest_time_ist: str = "23:30"
    data_dir: str = ""                     # settings.json + ticks/ (default: <repo>/data)
    record_ticks: bool = True
    tick_retention_days: int = 7
    # phantom-liquidity guard
    max_slippage_pct: float = 0.05         # max round-trip slippage (entry+exit) we accept
    phantom_frozen_s: float = 3.0          # laggard book unchanged this long while Binance ticks → PHANTOM
    depth_bps: float = 10.0                # resting-depth window reported per coin
    min_order_inr: float = 100.0           # shrink floor: skip if the book can't absorb at least this
    max_entry_frozen_s: float = 1.0        # fresh-book filter: laggard top-5 must have changed within this many s
    # shadow comparison assumptions (per laggard exchange)
    coinswitch_taker_fee_pct: float = 0.05
    coindcx_taker_fee_pct: float = 0.059
    coinswitch_exec_latency_s: float = 1.0
    coindcx_exec_latency_s: float = 1.9    # measured: create ≈1.2 s + fill confirm ≈0.7 s

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
            leverage=max(1, _i("LEVERAGE", 1)),
            laggard_exchange=os.environ.get("LAGGARD_EXCHANGE", "coinswitch").strip().lower() or "coinswitch",
            coindcx_api_key=os.environ.get("COINDCX_API_KEY", ""),
            coindcx_api_secret=os.environ.get("COINDCX_API_SECRET", ""),
            basis_filter=_b("BASIS_FILTER", True),
            basis_window_s=_i("BASIS_WINDOW_S", 300),
            digest_time_ist=os.environ.get("DIGEST_TIME_IST", "23:30"),
            data_dir=os.environ.get("DATA_DIR", ""),
            record_ticks=_b("RECORD_TICKS", True),
            tick_retention_days=_i("TICK_RETENTION_DAYS", 7),
            max_slippage_pct=_f("MAX_SLIPPAGE_PCT", 0.05),
            phantom_frozen_s=_f("PHANTOM_FROZEN_S", 3.0),
            depth_bps=_f("DEPTH_BPS", 10.0),
            min_order_inr=_f("MIN_ORDER_INR", 100.0),
            max_entry_frozen_s=_f("MAX_ENTRY_FROZEN_S", 1.0),
            coinswitch_taker_fee_pct=_f("COINSWITCH_TAKER_FEE_PCT", 0.05),
            coindcx_taker_fee_pct=_f("COINDCX_TAKER_FEE_PCT", 0.059),
            coinswitch_exec_latency_s=_f("COINSWITCH_EXEC_LATENCY_S", 1.0),
            coindcx_exec_latency_s=_f("COINDCX_EXEC_LATENCY_S", 1.9),
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
    leverage: int = 1
    notional_inr: float = 0.0
    basis_pct: float = 0.0
    liq_state: str = ""
    liq_slippage_pct: float = 0.0      # expected one-side entry slippage at the traded notional
    requested_notional_inr: float = 0.0  # pre-shrink slot notional
    entry_fx: float = 0.0              # USDT/INR used to book the entry; exits are marked at the same rate


# ---------------------------------------------------------------------------
# In-memory store (fine for dashboard preview; standalone bot swaps in SQLite)
# ---------------------------------------------------------------------------
class MemoryStore:
    def __init__(self, persist_path: Optional[Path] = None) -> None:
        self.open: Dict[str, Position] = {}
        self.closed: List[Position] = []
        self.equity_curve: List[Dict[str, float]] = []
        self.event_log: List[Dict[str, Any]] = []
        self._path = persist_path
        self._eq_pushes = 0
        if persist_path: self._load()

    # --- persistence (web dashboard; CLI uses SQLiteStore) --------------------
    def _load(self) -> None:
        try:
            d = json.loads(self._path.read_text())
            self.closed = [Position(**p) for p in d.get("closed", [])]
            self.open = {p["coin"]: Position(**p) for p in d.get("open", [])}
            self.equity_curve = d.get("equity_curve", [])
            logger.info(f"Restored {len(self.closed)} closed / {len(self.open)} open trades from {self._path.name}")
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(f"trade history load failed: {e}")

    def _save(self, with_equity: bool = True) -> None:
        if not self._path: return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            prev = {}
            if not with_equity and self._path.exists():
                try: prev = json.loads(self._path.read_text())
                except Exception: prev = {}
            data = {"open": [asdict(p) for p in self.open.values()], "closed": [asdict(p) for p in self.closed],
                    "equity_curve": self.equity_curve if with_equity else prev.get("equity_curve", self.equity_curve)}
            tmp = self._path.with_suffix(".tmp"); tmp.write_text(json.dumps(data)); tmp.replace(self._path)
        except Exception as e:
            logger.warning(f"trade history save failed: {e}")

    # --- open positions ------------------------------------------------------
    async def add_open(self, p: Position) -> None:
        self.open[p.coin] = p
        self._save()

    async def get_open(self, coin: str) -> Optional[Position]:
        return self.open.get(coin)

    async def all_open(self) -> List[Position]:
        return list(self.open.values())

    async def close(self, p: Position) -> None:
        self.open.pop(p.coin, None)
        self.closed.append(p)
        if len(self.closed) > 2000:
            self.closed = self.closed[-2000:]
        self._save()

    async def all_closed(self, limit: int = 500) -> List[Position]:
        return list(reversed(self.closed[-limit:]))

    # --- misc telemetry ------------------------------------------------------
    async def push_equity(self, ts: float, equity: float) -> None:
        self.equity_curve.append({"ts": ts, "equity": equity})
        if len(self.equity_curve) > 5000:
            self.equity_curve = self.equity_curve[-5000:]
        self._eq_pushes += 1
        if self._eq_pushes % 12 == 0: self._save()

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
        self.depth: Dict[str, Tuple[List[Tuple[float, float]], List[Tuple[float, float]]]] = {}  # coin -> (bids, asks) top-20 in USDT
        self.book_change_ts: Dict[str, float] = {}   # coin -> last time the top-5 levels changed
        self._book_sig: Dict[str, tuple] = {}
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
        self._store_depth(coin, bids, asks, now)
        lat = max(0.0, now * 1000.0 - ts_ms)
        if lat < 60_000:  # ignore absurd clock-skew values
            self.wire_latency_ms = lat if self.wire_latency_ms is None else 0.9 * self.wire_latency_ms + 0.1 * lat

    def _store_depth(self, coin: str, bids: list, asks: list, now: float) -> None:
        def _lv(rows):
            out = []
            for r in rows[:20]:
                try:
                    p, q = float(r[0]), float(r[1])
                except (TypeError, ValueError, IndexError):
                    continue
                if p > 0 and q > 0: out.append((p, q))
            return out
        b, a = _lv(bids), _lv(asks)
        self.depth[coin] = (b, a)
        sig = (tuple(b[:5]), tuple(a[:5]))
        if sig != self._book_sig.get(coin):
            self._book_sig[coin] = sig
            self.book_change_ts[coin] = now
        self.book_change_ts.setdefault(coin, now)

    def frozen_s(self, coin: str) -> float:
        t = self.book_change_ts.get(coin)
        return (time.time() - t) if t else 0.0

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


class LiveCoinDCXBookFeed(LiveCoinSwitchBookFeed):
    """CoinDCX Futures public book stream (Socket.IO v2 / EIO=3): depth-snapshot + depth-update deltas."""
    DEFAULT_URL = "wss://stream.coindcx.com/socket.io/?EIO=3&transport=websocket"

    def __init__(self, coins: List[str], fx_provider, url: str = "") -> None:
        super().__init__(coins, fx_provider, url or self.DEFAULT_URL)
        self._books: Dict[str, Dict[str, Dict[float, float]]] = {}

    def _apply(self, coin: str, side: str, levels: Dict[str, str], snapshot: bool) -> None:
        book = self._books.setdefault(coin, {"bids": {}, "asks": {}})
        if snapshot: book[side] = {}
        for p, q in levels.items():
            fp, fq = float(p), float(q)
            if fq <= 0: book[side].pop(fp, None)
            else: book[side][fp] = fq

    async def _run(self) -> None:
        backoff = 1
        while self._running:
            try:
                async with aiohttp.ClientSession() as sess:
                    async with sess.ws_connect(self.url, heartbeat=None) as ws:
                        self.connected = True; self.connected_since = time.time(); self.last_error = ""
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
                                for c in self.coins:
                                    await ws.send_str("42" + json.dumps(["join", {"channelName": f"B-{c}_USDT@orderbook@20-futures"}]))
                                self.subscribed = True; backoff = 1
                                logger.info(f"CoinDCX WS joined {len(self.coins)} books")
                            elif data.startswith("42"):
                                evt = json.loads(data[2:]); name = evt[0]
                                if name not in ("depth-update", "depth-snapshot"): continue
                                payload = evt[1].get("data") if isinstance(evt[1], dict) else None
                                d = json.loads(payload) if isinstance(payload, str) else (payload or {})
                                coin = str(d.get("s", "")).replace("B-", "").replace("_USDT", "").replace("USDT", "")
                                if coin not in self.coins: continue
                                snap = name == "depth-snapshot"
                                self._apply(coin, "bids", d.get("bids", {}), snap)
                                self._apply(coin, "asks", d.get("asks", {}), snap)
                                b = self._books[coin]
                                if not b["bids"] or not b["asks"]: continue
                                self.msg_count += 1; self._rate_window.append(self.last_msg_ts)
                                bids = sorted(b["bids"].items(), key=lambda x: -x[0])[:20]
                                asks = sorted(b["asks"].items(), key=lambda x: x[0])[:20]
                                self._on_snapshot({"s": f"{coin}USDT", "timestamp": d.get("ts"),
                                                   "bids": bids, "asks": asks})
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.last_error = str(e) or e.__class__.__name__
                logger.warning(f"CoinDCX WS error: {self.last_error}; reconnecting in {backoff}s")
                await asyncio.sleep(backoff); backoff = min(backoff * 2, 30)
            finally:
                self.connected = False; self.subscribed = False
                if self._running: self.reconnects += 1


def make_laggard_feed(exchange: str, coins: List[str], fx_provider, url: str = "") -> LiveCoinSwitchBookFeed:
    if exchange == "coindcx": return LiveCoinDCXBookFeed(coins, fx_provider)
    return LiveCoinSwitchBookFeed(coins, fx_provider, url=url)


class FXRateService:
    """USDT/INR quote = median of fresh sources (CoinGecko, CoinDCX spot USDTINR, Binance P2P top-10 median).
    Falls back to the last good blend, then to the config value.
    """
    FRESH_S = 180

    def __init__(self, fallback: float) -> None:
        self.rate = fallback
        self._fallback = fallback
        self._running = False
        self.sources: Dict[str, Dict[str, Any]] = {}
        self.last_update_ts = 0.0
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        self._running = False
        if self._task: self._task.cancel()

    def status(self) -> Dict[str, Any]:
        now = time.time()
        return {"rate": round(self.rate, 4), "last_update_age_s": round(now - self.last_update_ts, 1) if self.last_update_ts else None,
                "sources": {k: {"rate": v.get("rate"), "age_s": round(now - v["ts"], 1) if v.get("ts") else None,
                                "error": v.get("error", "")} for k, v in self.sources.items()},
                "used": [k for k, v in self.sources.items() if v.get("rate") and now - v.get("ts", 0) < self.FRESH_S]}

    async def _coingecko(self, s: aiohttp.ClientSession) -> float:
        async with s.get("https://api.coingecko.com/api/v3/simple/price?ids=tether&vs_currencies=inr") as r:
            d = await r.json(content_type=None)
            return float(d.get("tether", {}).get("inr", 0) or 0)

    async def _coindcx(self, s: aiohttp.ClientSession) -> float:
        async with s.get("https://api.coindcx.com/exchange/ticker") as r:
            for x in await r.json(content_type=None):
                if x.get("market") == "USDTINR":
                    bid, ask = float(x.get("bid") or 0), float(x.get("ask") or 0)
                    return (bid + ask) / 2 if bid and ask else float(x.get("last_price") or 0)
        return 0.0

    async def _binance_p2p(self, s: aiohttp.ClientSession) -> float:
        body = {"asset": "USDT", "fiat": "INR", "tradeType": "BUY", "rows": 10, "page": 1, "payTypes": [], "publisherType": None}
        async with s.post("https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search", json=body) as r:
            d = (await r.json(content_type=None)).get("data") or []
            px = sorted(float(a["adv"]["price"]) for a in d if a.get("adv", {}).get("price"))
            return px[len(px) // 2] if px else 0.0

    async def _loop(self) -> None:
        fetchers = {"coingecko": self._coingecko, "coindcx": self._coindcx, "binance_p2p": self._binance_p2p}
        try:
            while self._running:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as s:
                    results = await asyncio.gather(*(f(s) for f in fetchers.values()), return_exceptions=True)
                now = time.time()
                for name, res in zip(fetchers, results):
                    if isinstance(res, Exception) or not (60 < float(res or 0) < 130):
                        self.sources.setdefault(name, {})["error"] = str(res)[:80] if isinstance(res, Exception) else "out of range/empty"
                    else:
                        self.sources[name] = {"rate": round(float(res), 4), "ts": now, "error": ""}
                fresh = {k: v["rate"] for k, v in self.sources.items() if v.get("rate") and now - v.get("ts", 0) < self.FRESH_S}
                if fresh:
                    # CoinDCX's own USDT/INR is the rate the exchange actually margins at; others are sanity fallbacks.
                    # Fixed preference avoids the ±1.5% jumps a plain median makes when one source drops out.
                    vals = sorted(fresh.values())
                    self.rate = fresh["coindcx"] if "coindcx" in fresh else (vals[len(vals) // 2] if len(vals) % 2 else (vals[len(vals)//2 - 1] + vals[len(vals)//2]) / 2)
                    self.last_update_ts = now
                await asyncio.sleep(30)
        except asyncio.CancelledError:
            pass


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
        self.data_dir = Path(cfg.data_dir) if cfg.data_dir else Path(__file__).resolve().parent.parent / "data"
        self.store = store or MemoryStore(persist_path=self.data_dir / "trades.json")
        self.fx = FXRateService(cfg.fx_usdt_inr)
        self._build_feeds()
        self.telegram = TelegramReporter(cfg.telegram_bot_token, cfg.telegram_chat_id)
        self.coindcx: Optional[CoinDCXFuturesClient] = None
        self.coindcx_user_stream: Optional[CoinDCXUserStream] = None
        if cfg.coindcx_api_key and cfg.coindcx_api_secret:
            try:
                self.coindcx = CoinDCXFuturesClient(cfg.coindcx_api_key, cfg.coindcx_api_secret,
                                                    margin_currency=os.environ.get("COINDCX_MARGIN_CURRENCY", "USDT"))
                self.coindcx_user_stream = CoinDCXUserStream(cfg.coindcx_api_key, cfg.coindcx_api_secret)
                self.coindcx.user_stream = self.coindcx_user_stream
            except Exception as e:
                logger.warning(f"CoinDCX client init failed: {e}")
        self._order_cooldown: Dict[str, float] = {}
        self._basis_samples: Dict[str, List[tuple]] = {c: [] for c in cfg.coins}
        self.settings_path = self.data_dir / "settings.json"
        self.recorder = TickRecorder(self.data_dir / "ticks", cfg.tick_retention_days, enabled=cfg.record_ticks and not cfg.simulate)
        self._restored_settings: Dict[str, Any] = {}
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
        self._last_gross_pct: Dict[str, float] = {c: 0.0 for c in cfg.coins}
        self._last_basis_pct: Dict[str, float] = {c: 0.0 for c in cfg.coins}
        # sparklines: last 60 spread% points per coin
        self._sparkline: Dict[str, List[float]] = {c: [] for c in cfg.coins}
        # heatmap: coin -> {epoch_sec: signed max-|spread|% in that second} (kept HEAT_RETENTION_S)
        self._heat: Dict[str, Dict[int, float]] = {c: {} for c in cfg.coins}
        self.paused: set = set()
        self._last_liq: Dict[str, Dict[str, Any]] = {}
        self.liq_stats: Dict[str, int] = {"phantom_skips": 0, "thin_skips": 0, "edge_skips": 0, "shrunk": 0, "stale_skips": 0}
        self._live_missing_keys_warned = False
        self._stale_no_keys_warned = False
        self._restored_settings = apply_settings(self, load_settings(self.settings_path))
        if self._restored_settings:
            logger.info(f"Restored persisted settings: {self._restored_settings}")
            restored_coins = self._restored_settings.get("coins")
            if restored_coins and list(restored_coins) != list(self._sparkline.keys()):
                self._reset_universe(list(restored_coins))
            elif "laggard_exchange" in self._restored_settings:
                self._build_feeds()

    def persist(self) -> None:
        try:
            save_settings(self, self.settings_path)
        except Exception as e:
            logger.warning(f"settings persist failed: {e}")

    def _build_feeds(self) -> None:
        cfg = self.cfg
        self.simulator = MarketSimulator(cfg.coins, fx_provider=lambda: self.fx.rate, fx_ref=cfg.fx_usdt_inr) if cfg.simulate else None
        self.live_feed = None if cfg.simulate else LiveBinanceFeed(cfg.coins)
        # Public laggard book WS — no keys needed, powers PAPER + LIVE when not simulating
        self.coinswitch_feed: Optional[LiveCoinSwitchBookFeed] = None if cfg.simulate else \
            make_laggard_feed(cfg.laggard_exchange, cfg.coins, lambda: self.fx.rate, url=cfg.coinswitch_ws_url)

    def _reset_universe(self, coins: List[str]) -> None:
        self.cfg.coins = coins
        for name in ("_last_spread_pct", "_last_gross_pct", "_last_basis_pct"):
            setattr(self, name, {c: 0.0 for c in coins})
        self._last_signal_state = {c: "WAITING" for c in coins}
        self._sparkline = {c: [] for c in coins}
        self._heat = {c: {} for c in coins}
        self._basis_samples = {c: [] for c in coins}
        self._last_liq = {}
        self.paused = {c for c in self.paused if c in coins}
        self._build_feeds()
        if self.coinswitch_poller: self.coinswitch_poller.coins = coins

    async def set_laggard_exchange(self, exchange: str) -> None:
        """Switch the traded pair (Binance → coinswitch | coindcx): restarts feeds; refuses while positions are open."""
        exchange = exchange.strip().lower()
        if exchange not in ("coinswitch", "coindcx"): raise ValueError("exchange must be coinswitch or coindcx")
        if exchange == self.cfg.laggard_exchange: return
        if await self.store.all_open(): raise RuntimeError("close open positions before switching the pair")
        was_running = self.running
        if was_running: await self.stop()
        self.cfg.laggard_exchange = exchange
        self._build_feeds()
        self._last_liq = {}
        self._order_cooldown = {}
        self._live_missing_keys_warned = False
        self.persist()
        await self.store.log("INFO", f"Pair switched to Binance → {exchange.upper()}")
        if was_running: await self.start()
        if not self.cfg.paper_trading and self._live_executor_ready():
            await self.apply_leverage(self.cfg.leverage)

    async def wallet(self) -> Dict[str, Any]:
        """Futures wallet of the active laggard exchange (normalised: [{currency, balance, locked}])."""
        ex = self.cfg.laggard_exchange
        out: Dict[str, Any] = {"exchange": ex, "balances": [], "keys": False, "error": None, "fetched_at": time.time()}
        try:
            if ex == "coindcx":
                if not self.coindcx: return out
                out["keys"] = True
                for w in (await self.coindcx.wallet_balance()) or []:
                    out["balances"].append({"currency": str(w.get("currency_short_name", "")).upper(),
                                            "balance": float(w.get("balance") or 0), "locked": float(w.get("locked_balance") or 0)})
                out["margin_currency"] = self.coindcx.margin_currency
            else:
                if not self.coinswitch: return out
                out["keys"] = True
                wb = await self.coinswitch.wallet_balance()
                data = wb.get("data", wb) if isinstance(wb, dict) else {}
                for w in (data.get("base_asset_balances") or []) if isinstance(data, dict) else []:
                    b = w.get("balances") or {}
                    out["balances"].append({"currency": str(w.get("base_asset") or "USDT").upper(),
                                            "balance": float(b.get("total_balance") or 0),
                                            "locked": float(b.get("total_blocked_balance") or 0) + float(b.get("total_position_margin") or 0)})
                out["margin_currency"] = "USDT"
        except Exception as e:
            out["error"] = str(e)
        return out

    async def set_coins(self, coins: List[str]) -> None:
        """Swap the traded universe: restarts feeds; refuses while positions are open."""
        coins = [c.upper() for c in dict.fromkeys(coins) if c]
        if not coins: raise ValueError("empty universe")
        if await self.store.all_open(): raise RuntimeError("close open positions before changing the universe")
        was_running = self.running
        if was_running: await self.stop()
        self._reset_universe(coins)
        self.persist()
        await self.store.log("INFO", f"Universe set to {len(coins)} coins: {', '.join(coins)}")
        if was_running: await self.start()

    # --- lifecycle -----------------------------------------------------------
    async def start(self) -> None:
        if self.running: return
        self.running = True
        self.started_at = time.time()
        if self.simulator: await self.simulator.start()
        if self.live_feed: await self.live_feed.start()
        if self.coinswitch_feed: await self.coinswitch_feed.start()
        if self.coindcx_user_stream and self.cfg.laggard_exchange == "coindcx": await self.coindcx_user_stream.start()
        if self.recorder.enabled: await self.recorder.start()
        await self.fx.start()
        self._tasks = [
            asyncio.create_task(self._scan_loop()),
            asyncio.create_task(self._position_manager_loop()),
            asyncio.create_task(self._equity_ticker_loop()),
            asyncio.create_task(self._hourly_summary_loop()),
        ]
        if self.coinswitch_feed:
            self._tasks.append(asyncio.create_task(self._feed_watchdog_loop()))
        if self.coindcx and self.cfg.laggard_exchange == "coindcx":
            self._tasks.append(asyncio.create_task(self._keep_warm_loop()))
        if self.telegram.enabled:
            self._tasks.append(asyncio.create_task(self._telegram_resolve_bot()))
            self._tasks.append(asyncio.create_task(self._daily_digest_loop()))
        feed = "SIMULATOR" if self.simulator else f"Binance WS + {self.cfg.laggard_exchange.upper()} book WS"
        await self.store.log("INFO", f"Bot started in {'PAPER' if self.cfg.paper_trading else 'LIVE'} mode · feed: {feed}")

    async def stop(self) -> None:
        if not self.running: return
        self.running = False
        for t in self._tasks: t.cancel()
        self._tasks = []
        if self.simulator: self.simulator.stop()
        if self.live_feed: self.live_feed.stop()
        if self.coinswitch_feed: self.coinswitch_feed.stop()
        if self.coindcx_user_stream: self.coindcx_user_stream.stop()
        if self.coinswitch_poller: self.coinswitch_poller.stop()
        self.recorder.stop()
        self.persist()
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

    async def _keep_warm_loop(self) -> None:
        """Keep the CoinDCX HTTPS connection hot so order create doesn't pay DNS+TCP+TLS."""
        try:
            while self.running:
                await self.coindcx.keep_warm()
                await asyncio.sleep(15)
        except asyncio.CancelledError: pass

    def laggard_source(self) -> str:
        if self.simulator: return "SIMULATOR"
        if self.coinswitch_feed and not self._feed_stale(): return f"{self.cfg.laggard_exchange.upper()}_WS"
        if self.coinswitch_poller and self.coinswitch_poller.running: return "REST_POLLER"
        return "STALE"

    def feed_health(self) -> Dict[str, Any]:
        f = self.coinswitch_feed
        lf = self.live_feed
        def _r(v, n=1): return None if v is None else round(v, n)
        return {
            "source": self.laggard_source(),
            "laggard_exchange": self.cfg.laggard_exchange,
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
                "rate_limiter": self.coinswitch.limiter_stats() if self.coinswitch else None,
            },
            "fx": self.fx.status(),
            "liquidity_guard": {**self.liq_stats, "max_slippage_pct": self.cfg.max_slippage_pct,
                                "phantom_frozen_s": self.cfg.phantom_frozen_s, "min_order_inr": self.cfg.min_order_inr,
                                "max_entry_frozen_s": self.cfg.max_entry_frozen_s},
            "user_ws": None if not self.coindcx_user_stream else {
                **self.coindcx_user_stream.status(),
                "active": self.cfg.laggard_exchange == "coindcx",
                "fills_via_ws": self.coindcx.fills_via_ws, "fills_via_rest": self.coindcx.fills_via_rest,
                "last_fill_latency_ms": self.coindcx.last_fill_latency_ms, "last_fill_source": self.coindcx.last_fill_source,
                "last_create_ms": self.coindcx.last_create_ms, "last_warm_ms": self.coindcx.last_warm_ms,
                "last_warm_error": self.coindcx.last_warm_error,
            },
            "recorder": self.recorder.status(),
            "restored_settings": self._restored_settings,
        }

    def hurdle_pct(self) -> float:
        c = self.cfg
        return c.entry_taker_fee_pct + c.exit_taker_fee_pct + c.slippage_buffer_pct + c.hurdle_extra_pct

    def dynamic_hurdle_pct(self, one_side_slippage_pct: float) -> float:
        """Hurdle with the flat slippage buffer replaced by measured round-trip slippage (floored at the buffer)."""
        c = self.cfg
        return c.entry_taker_fee_pct + c.exit_taker_fee_pct + max(c.slippage_buffer_pct, 2 * one_side_slippage_pct) + c.hurdle_extra_pct

    # --- phantom-liquidity guard ---------------------------------------------------
    def liquidity(self, coin: str, direction: str, notional_inr: float) -> Optional[Dict[str, Any]]:
        """Walk the laggard book for `notional_inr` on the side we'd hit (LONG→asks, SHORT→bids)."""
        feed = self.coinswitch_feed
        if not feed or coin not in feed.depth: return None
        bids, asks = feed.depth[coin]
        levels = asks if direction == "LONG" else bids
        fx = max(self.fx.rate, 1e-6)
        leader = self._leader(coin)
        leader_fresh = bool(leader) and (time.time() * 1000 - leader.ts_ms) < 2000
        liq = assess(levels, notional_inr / fx, self.cfg.max_slippage_pct / 2, self.cfg.depth_bps,
                     feed.frozen_s(coin), self.cfg.phantom_frozen_s if leader_fresh else float("inf"))
        return {"state": liq.state, "vwap_inr": liq.vwap * fx, "slippage_pct": liq.slippage_pct,
                "fillable_inr": round(liq.fillable * fx, 2), "depth_inr": round(liq.depth_notional * fx, 2),
                "levels": liq.levels, "frozen_s": liq.frozen_s, "notional_inr": round(notional_inr, 2),
                "dyn_hurdle_pct": round(self.dynamic_hurdle_pct(liq.slippage_pct), 4)}

    def allocated_per_slot_inr(self) -> float:
        n = max(1, self.cfg.num_slots)
        return self.equity_inr / n

    # --- heatmap ---------------------------------------------------------------
    HEAT_WINDOW_S = 60
    HEAT_RETENTION_S = 600
    NEAR_HURDLE_RATIO = 0.8

    def _record_heat(self, coin: str, gross_pct: float) -> None:
        sec = int(time.time())
        buckets = self._heat[coin]
        prev = buckets.get(sec, 0.0)
        if abs(gross_pct) >= abs(prev): buckets[sec] = gross_pct
        if len(buckets) > self.HEAT_RETENTION_S + 5:
            cutoff = sec - self.HEAT_RETENTION_S
            for k in [k for k in buckets if k < cutoff]: del buckets[k]

    def _heat_series(self, coin: str) -> List[float]:
        now = int(time.time())
        h = max(self.hurdle_pct(), 1e-9)
        b = self._heat.get(coin, {})
        return [round(b.get(s, 0.0) / h, 3) for s in range(now - self.HEAT_WINDOW_S + 1, now + 1)]

    def hurdle_simulate(self, hurdle_pct: float, window_s: int = 600) -> Dict[str, Any]:
        """Count would-be signals (contiguous runs of seconds with |spread| ≥ hurdle) per coin."""
        now = int(time.time())
        window_s = max(10, min(int(window_s), self.HEAT_RETENTION_S))
        coins = []
        total_ep = total_sec = 0
        for c in self.cfg.coins:
            b = self._heat.get(c, {})
            episodes = secs = 0
            prev_above = False
            for s in range(now - window_s + 1, now + 1):
                above = abs(b.get(s, 0.0)) >= hurdle_pct
                if above:
                    secs += 1
                    if not prev_above: episodes += 1
                prev_above = above
            coins.append({"coin": c, "episodes": episodes, "seconds_above": secs})
            total_ep += episodes; total_sec += secs
        covered = min(window_s, int(now - (self.started_at or now)))
        return {"hurdle_pct": hurdle_pct, "window_s": window_s, "covered_s": max(0, covered),
                "total_episodes": total_ep, "total_seconds_above": total_sec, "coins": coins}

    # --- kill switch -----------------------------------------------------------
    def set_paused(self, coin: str, paused: bool) -> bool:
        if coin not in self.cfg.coins: return False
        if paused: self.paused.add(coin)
        else: self.paused.discard(coin)
        self.persist()
        return True

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
        self.recorder.record(coin, leader.bid, leader.ask, lagg.bid, lagg.ask, fx)
        basis = self._update_basis(coin, gross_pct)
        adj_pct = gross_pct - basis if self.cfg.basis_filter else gross_pct
        self._last_gross_pct[coin] = gross_pct
        self._last_basis_pct[coin] = basis
        self._last_spread_pct[coin] = adj_pct
        sp = self._sparkline[coin]; sp.append(adj_pct)
        if len(sp) > 60: self._sparkline[coin] = sp[-60:]
        self._record_heat(coin, adj_pct)
        lev = max(1, int(self.cfg.leverage))
        liq = self.liquidity(coin, "LONG" if adj_pct >= 0 else "SHORT", self.allocated_per_slot_inr() * lev)
        if liq: self._last_liq[coin] = liq

        # skip if slot already occupied
        if await self.store.get_open(coin):
            self._last_signal_state[coin] = "IN_TRADE"
            return
        if coin in self.paused:
            self._last_signal_state[coin] = "PAUSED"
            return

        hurdle = self.hurdle_pct()
        # basis-adjusted catch-up target: leader price net of the persistent offset
        target_ref = leader_inr_mid / (1 + basis / 100.0) if self.cfg.basis_filter else leader_inr_mid
        if adj_pct > hurdle:
            self._last_signal_state[coin] = "HURDLE_MET_LONG"
            await self._open_position(coin, "LONG", leader, lagg, target_ref, basis, adj_pct)
        elif -adj_pct > hurdle:
            self._last_signal_state[coin] = "HURDLE_MET_SHORT"
            await self._open_position(coin, "SHORT", leader, lagg, target_ref, basis, adj_pct)
        else:
            self._last_signal_state[coin] = "WAITING"

    # --- basis filter ----------------------------------------------------------
    def _update_basis(self, coin: str, gross_pct: float) -> float:
        now = time.time()
        s = self._basis_samples[coin]
        s.append((now, gross_pct))
        cutoff = now - self.cfg.basis_window_s
        if s and s[0][0] < cutoff:
            self._basis_samples[coin] = s = [x for x in s if x[0] >= cutoff]
        if len(s) < 20: return 0.0        # not enough history → no adjustment
        vals = sorted(v for _, v in s)
        return vals[len(vals) // 2]

    # --- account controls ------------------------------------------------------
    async def reset_account(self, equity_inr: float) -> None:
        self.cfg.starting_capital_inr = equity_inr
        self.equity_inr = equity_inr
        self.realized_pnl_inr = 0.0
        self.trade_count = self.win_count = self.loss_count = 0
        self.total_hold_sec = self.total_exec_latency_ms = 0.0
        if hasattr(self.store, "equity_curve"): self.store.equity_curve = []
        await self.store.push_equity(time.time(), equity_inr)
        self.persist()
        await self.store.log("INFO", f"Account reset: equity ₹{equity_inr:.2f}, stats zeroed (closed history kept)")

    async def apply_leverage(self, leverage: int) -> List[str]:
        """Set leverage; in LIVE mode push to the laggard exchange for coins without open positions."""
        self.cfg.leverage = leverage
        errors: List[str] = []
        if self.cfg.paper_trading: return errors
        open_coins = {p.coin for p in await self.store.all_open()}
        for c in self.cfg.coins:
            if c in open_coins: continue
            try:
                if self.cfg.laggard_exchange == "coindcx" and self.coindcx:
                    await self.coindcx.update_leverage(c, leverage)
                elif self.coinswitch:
                    await self.coinswitch.update_leverage(f"{c}USDT", leverage)
            except Exception as e:
                errors.append(f"{c}: {e}")
        if errors: await self.store.log("WARN", f"Leverage update failed for {len(errors)} symbols: {errors[:3]}")
        else: await self.store.log("INFO", f"Leverage set to {leverage}x")
        self.persist()
        return errors

    def coin_analytics(self) -> List[Dict[str, Any]]:
        out = []
        closed = getattr(self.store, "closed", [])
        for c in self.cfg.coins:
            ps = [p for p in closed if p.coin == c]
            n = len(ps); wins = sum(1 for p in ps if p.net_pnl_inr >= 0)
            out.append({
                "coin": c, "trades": n, "wins": wins, "losses": n - wins,
                "win_rate": round(wins / n * 100, 1) if n else 0.0,
                "net_pnl_inr": round(sum(p.net_pnl_inr for p in ps), 2),
                "gross_pnl_inr": round(sum(p.gross_pnl_inr for p in ps), 2),
                "fees_inr": round(sum(p.fees_inr for p in ps), 2),
                "avg_hold_sec": round(sum((p.closed_at or 0) - p.opened_at for p in ps) / n, 1) if n else 0.0,
                "timeouts": sum(1 for p in ps if p.close_reason == "TIMEOUT"),
                "target_hits": sum(1 for p in ps if p.close_reason == "TARGET_HIT"),
                "stops": sum(1 for p in ps if p.close_reason == "STOP_LOSS"),
                "last_trade_ts": max((p.closed_at or 0) for p in ps) if ps else None,
                "paused": c in self.paused,
                "basis_pct": round(self._last_basis_pct.get(c, 0.0), 4),
            })
        return out

    # --- order lifecycle -----------------------------------------------------
    async def _open_position(self, coin: str, direction: str,
                             leader: TopOfBook, lagg: TopOfBook,
                             leader_inr_mid: float, basis: float = 0.0, adj_pct: Optional[float] = None) -> None:
        signal_ts = time.time()
        size_inr = min(self.allocated_per_slot_inr(), max(0.0, self.equity_inr - sum(p.size_inr for p in await self.store.all_open())))
        if size_inr < 50: return  # min lot guard
        lev = max(1, int(self.cfg.leverage))
        notional_inr = size_inr * lev
        requested_notional = notional_inr
        # Entry price (paper) = worst top-of-book (taker)
        entry_price = lagg.ask if direction == "LONG" else lagg.bid
        # -------- phantom-liquidity guard: walk the book before committing --------
        liq = self.liquidity(coin, direction, notional_inr)
        liq_state, liq_slip = "", 0.0
        if liq:
            if liq["state"] in ("PHANTOM", "NO_DEPTH"):
                self.liq_stats["phantom_skips"] += 1
                self._last_signal_state[coin] = "PHANTOM"
                return
            if liq["frozen_s"] > self.cfg.max_entry_frozen_s:
                # fresh-book filter: a quote that hasn't moved is a stale copy, not a dislocation
                self.liq_stats["stale_skips"] = self.liq_stats.get("stale_skips", 0) + 1
                self._last_signal_state[coin] = "STALE_BOOK"
                return
            if liq["fillable_inr"] < notional_inr:
                if liq["fillable_inr"] < self.cfg.min_order_inr:
                    self.liq_stats["thin_skips"] += 1
                    self._last_signal_state[coin] = "THIN"
                    return
                notional_inr = liq["fillable_inr"]
                size_inr = notional_inr / lev
                self.liq_stats["shrunk"] += 1
                liq = self.liquidity(coin, direction, notional_inr) or liq
            liq_state, liq_slip = liq["state"], liq["slippage_pct"]
            if adj_pct is not None and abs(adj_pct) < self.dynamic_hurdle_pct(liq_slip):
                self.liq_stats["edge_skips"] += 1
                self._last_signal_state[coin] = "THIN"
                return
            if liq["vwap_inr"] > 0: entry_price = liq["vwap_inr"]
        contracts = notional_inr / entry_price
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
            # -------- LIVE: place MARKET order on the laggard exchange --------
            if not self._live_executor_ready():
                if not self._live_missing_keys_warned:
                    await self.store.log("ERROR", f"[LIVE] {self.cfg.laggard_exchange.upper()} keys missing — no orders will be placed until API key/secret are set. Suppressing further per-coin warnings.")
                    self._live_missing_keys_warned = True
                return
            fx = max(self.fx.rate, 1e-6)
            if self._order_cooldown.get(coin, 0) > time.time(): return
            usdt_notional = notional_inr / fx
            qty = usdt_notional / max(leader.mid, 1e-9)
            side = "BUY" if direction == "LONG" else "SELL"
            t0 = time.perf_counter()
            try:
                fill = await self._live_market(coin, side, qty, reduce_only=False, price_usdt=leader.mid)
            except SlotTooSmall as e:
                self._order_cooldown[coin] = time.time() + 600
                await self.store.log("WARN", f"[LIVE] {coin} skipped: {e} — raise equity/leverage or bench this coin; retry in 10 min")
                return
            except (CoinSwitchError, CoinDCXError) as e:
                self._order_cooldown[coin] = time.time() + 30
                await self.store.log("ERROR", f"[LIVE] {coin} order rejected {e.status}: {e.body} — cooling down 30s")
                return
            except Exception as e:
                self._order_cooldown[coin] = time.time() + 30
                await self.store.log("ERROR", f"[LIVE] {coin} order exception: {e} — cooling down 30s")
                return
            latency_ms = (time.perf_counter() - t0) * 1000.0
            entry_order_id = fill["order_id"]
            exec_qty, avg_px, fee_usdt, status = fill["exec_qty"], fill["avg_px"], fill["fee"], fill["status"]
            if exec_qty <= 0 or avg_px <= 0:
                await self.store.log(
                    "WARN", f"[LIVE] {coin} order {status} exec_qty=0 order_id={entry_order_id}")
                return
            # translate to engine's INR bookkeeping using current FX
            contracts = exec_qty                          # base-asset units
            entry_price = avg_px * fx                     # INR-equivalent entry
            notional_inr = exec_qty * avg_px * fx         # actual filled INR notional
            size_inr = notional_inr / lev                 # margin actually committed
            entry_fee_inr = fee_usdt * fx
            target = leader_inr_mid
            stop = entry_price * (1 - self.cfg.stop_loss_pct/100.0) if direction == "LONG" \
                   else entry_price * (1 + self.cfg.stop_loss_pct/100.0)
            await self.store.log(
                "INFO",
                f"[LIVE] {coin} filled {exec_qty} @ {avg_px} USDT ({status}) fee={fee_usdt} USDT · create {self.coindcx.last_create_ms if self.coindcx else '?'} ms · fill via {fill.get('fill_source') or 'REST'} {fill.get('fill_latency_ms') or '?'} ms")

        # never take profit below break-even: the target must at least cover both taker fees + edge
        be = (self.cfg.entry_taker_fee_pct + self.cfg.exit_taker_fee_pct + self.cfg.hurdle_extra_pct) / 100.0
        target = max(target, entry_price * (1 + be)) if direction == "LONG" else min(target, entry_price * (1 - be))

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
            leverage=lev,
            notional_inr=notional_inr,
            basis_pct=basis,
            liq_state=liq_state,
            liq_slippage_pct=liq_slip,
            requested_notional_inr=requested_notional,
            entry_fx=max(self.fx.rate, 1e-6),
        )
        await self.store.add_open(pos)
        shrink = f" (shrunk from ₹{requested_notional:.0f})" if notional_inr < requested_notional - 0.5 else ""
        await self.store.log("TRADE", f"OPEN {direction} {coin} ₹{size_inr:.2f}×{lev} @ {entry_price:.6f}{shrink} · book {liq_state or 'n/a'} slip {liq_slip:.3f}%")
        await self.telegram.send(
            f"🟢 <b>OPEN {direction}</b> {coin}\n"
            f"Margin: ₹{size_inr:.2f} × {lev}x = ₹{notional_inr:.2f} ({contracts:.6f} contracts)\n"
            f"Entry: {entry_price:.6f} | Target: {target:.6f}\n"
            f"Mode: {pos.mode}"
        )

    # --- live execution (exchange-agnostic) --------------------------------------
    def _live_executor_ready(self) -> bool:
        return bool(self.coindcx if self.cfg.laggard_exchange == "coindcx" else self.coinswitch)

    async def _live_market(self, coin: str, side: str, qty: float, reduce_only: bool, price_usdt: float = 0.0) -> Dict[str, Any]:
        """Place a MARKET order on the configured laggard and wait for the fill.
        Returns {order_id, exec_qty, avg_px (USDT), fee (USDT), status}."""
        if self.cfg.laggard_exchange == "coindcx":
            sized = await self.coindcx.size_qty(coin, qty, price_usdt, reduce_only)
            if not reduce_only and price_usdt > 0 and sized * price_usdt > qty * price_usdt * 1.5 + 1e-9:
                raise SlotTooSmall(f"{coin} exchange minimum ≈ {sized * price_usdt:.2f} USDT exceeds slot notional {qty * price_usdt:.2f} USDT")
            qty = sized
            placed = await self.coindcx.market_order(coin, side, qty, self.cfg.leverage)
            oid = str(placed.get("id") or "")
            if not oid: raise RuntimeError(f"CoinDCX create missing id: {placed}")
            o = await self.coindcx.wait_for_fill(oid, side, timeout_s=self.cfg.live_order_timeout_s) or placed
            total = float(o.get("total_quantity") or 0); rem = float(o.get("remaining_quantity") or 0)
            return {"order_id": oid, "exec_qty": max(0.0, total - rem), "avg_px": float(o.get("avg_price") or 0),
                    "fee": float(o.get("fee_amount") or 0), "status": str(o.get("status", "")),
                    "fill_source": self.coindcx.last_fill_source, "fill_latency_ms": self.coindcx.last_fill_latency_ms}
        placed = await self.coinswitch.place_order(symbol=f"{coin}USDT", side=side, order_type="MARKET",
                                                   quantity=qty, reduce_only=reduce_only)
        oid = (placed.get("data") or {}).get("order_id")
        if not oid: raise RuntimeError(f"CoinSwitch place_order missing order_id: {placed}")
        d = (await self.coinswitch.wait_for_fill(oid, timeout_s=self.cfg.live_order_timeout_s)).get("data") or {}
        return {"order_id": oid, "exec_qty": float(d.get("exec_quantity") or 0),
                "avg_px": float(d.get("avg_execution_price") or 0), "fee": float(d.get("execution_fee") or 0),
                "status": d.get("status", "")}

    async def _position_manager_loop(self) -> None:
        try:
            while self.running:
                for pos in await self.store.all_open():
                    await self._manage_position(pos)
                await asyncio.sleep(0.3)
        except asyncio.CancelledError: pass

    def _mark_fx(self, pos: Position) -> float:
        """Rescale a current INR quote into the position's entry-FX frame so FX drift can't fake a target/stop."""
        return (pos.entry_fx / max(self.fx.rate, 1e-6)) if pos.entry_fx else 1.0

    async def _manage_position(self, pos: Position) -> None:
        lagg = self._laggard(pos.coin)
        if not lagg: return
        now = time.time()
        # mark-to-market (in the entry-FX frame)
        cur = (lagg.bid if pos.direction == "LONG" else lagg.ask) * self._mark_fx(pos)
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
        if pos.mode == "PAPER":
            # realistic paper exit: walk the opposite side of the book for our contracts
            liq = self.liquidity(pos.coin, "SHORT" if pos.direction == "LONG" else "LONG", pos.contracts * price)
            if liq and liq["vwap_inr"] > 0: price = liq["vwap_inr"] * self._mark_fx(pos)
        if pos.mode == "LIVE" and self._live_executor_ready():
            if self._order_cooldown.get(pos.coin, 0) > time.time(): return
            # place reduce_only MARKET on opposite side
            side = "SELL" if pos.direction == "LONG" else "BUY"
            try:
                fill = await self._live_market(pos.coin, side, pos.contracts, reduce_only=True)
                exit_order_id = fill["order_id"]
                exec_qty, avg_px, fee_usdt = fill["exec_qty"], fill["avg_px"], fill["fee"]
                if exec_qty > 0 and avg_px > 0:
                    fx = pos.entry_fx or max(self.fx.rate, 1e-6)
                    price = avg_px * fx
                    exit_fee_inr = fee_usdt * fx
                    # partial-fill: shrink contracts to what actually closed
                    if exec_qty < pos.contracts:
                        pos.contracts = exec_qty
                await self.store.log(
                    "INFO",
                    f"[LIVE] {pos.coin} CLOSE fill exec={exec_qty} avg={avg_px} fee={fee_usdt} USDT · fill via {fill.get('fill_source') or 'REST'} {fill.get('fill_latency_ms') or '?'} ms")
            except (CoinSwitchError, CoinDCXError) as e:
                self._order_cooldown[pos.coin] = time.time() + 10
                await self.store.log("ERROR", f"[LIVE] {pos.coin} close rejected {e.status}: {e.body} — position kept open, retry in 10s")
                return
            except Exception as e:
                self._order_cooldown[pos.coin] = time.time() + 10
                await self.store.log("ERROR", f"[LIVE] {pos.coin} close exception: {e} — position kept open, retry in 10s")
                return

        notional = pos.notional_inr or pos.size_inr * max(1, pos.leverage)
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
                                else notional * self.cfg.exit_taker_fee_pct/100.0)
        else:
            fees = notional * (self.cfg.entry_taker_fee_pct + self.cfg.exit_taker_fee_pct) / 100.0
        pos.gross_pnl_inr = gross
        pos.fees_inr = fees
        pos.net_pnl_inr = gross - fees
        pos.status = "CLOSED"
        self.equity_inr += pos.net_pnl_inr
        self.realized_pnl_inr += pos.net_pnl_inr
        self.trade_count += 1
        if pos.net_pnl_inr >= 0: self.win_count += 1
        else: self.loss_count += 1
        self.persist()
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

    async def _telegram_resolve_bot(self) -> None:
        try:
            me = await self.telegram.get_me()
            await self.store.log("INFO", f"Telegram alerts enabled via @{me.get('username', '?')}")
        except Exception as e:
            self.telegram.last_error = str(e)
            await self.store.log("WARN", f"Telegram getMe failed: {e}")

    async def _daily_digest_loop(self) -> None:
        try:
            while self.running:
                hh, mm = (int(x) for x in (self.cfg.digest_time_ist.split(":") + ["0"])[:2])
                ist = timezone(timedelta(hours=5, minutes=30))
                now = datetime.now(ist)
                nxt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if nxt <= now: nxt += timedelta(days=1)
                await asyncio.sleep((nxt - now).total_seconds())
                await self.send_daily_digest()
        except asyncio.CancelledError: pass

    async def send_daily_digest(self, raise_on_error: bool = False, bench: Optional[Dict[str, Any]] = None) -> None:
        rows = sorted(self.coin_analytics(), key=lambda r: r["net_pnl_inr"], reverse=True)
        traded = [r for r in rows if r["trades"]]
        wr = (self.win_count / self.trade_count * 100.0) if self.trade_count else 0.0
        lines = [f"📒 <b>Daily Digest</b> · {datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime('%d %b %Y %H:%M IST')}",
                 f"Equity ₹{self.equity_inr:.2f} (start ₹{self.cfg.starting_capital_inr:.0f}) · PnL ₹{self.realized_pnl_inr:+.2f}",
                 f"Trades {self.trade_count} · Win {wr:.1f}% · Lev {self.cfg.leverage}x · Slots {self.cfg.num_slots} · {self.cfg.laggard_exchange.upper()}"]
        if traded:
            lines.append(f"🏆 Best: {traded[0]['coin']} ₹{traded[0]['net_pnl_inr']:+.2f} ({traded[0]['trades']} tr)")
            lines.append(f"🩸 Worst: {traded[-1]['coin']} ₹{traded[-1]['net_pnl_inr']:+.2f} ({traded[-1]['trades']} tr)")
            lines.append("<pre>coin  tr  win%   net₹   T/O</pre>")
            lines.append("<pre>" + "\n".join(f"{r['coin']:<5}{r['trades']:>3}{r['win_rate']:>6.0f}{r['net_pnl_inr']:>8.2f}{r['timeouts']:>5}" for r in traded) + "</pre>")
        fh = self.feed_health()
        lg = fh.get("laggard") or {}
        lines.append(f"📡 Feed {fh['source']} · wire {lg.get('wire_latency_ms') or '—'} ms · rc {lg.get('reconnects', 0)}")
        if bench and bench.get("exchanges"):
            ex = bench["exchanges"].get(self.cfg.laggard_exchange) or {}
            t = ex.get("ws_tick_to_receipt_ms") or {}
            lines.append(f"⏱ Last benchmark {self.cfg.laggard_exchange}: tick p50 {t.get('p50')} / p99 {t.get('p99')} ms · TCP p50 {(ex.get('tcp_rtt_ms') or {}).get('p50')} ms")
        paused = sorted(self.paused)
        if paused: lines.append(f"⏸ Paused: {', '.join(paused)}")
        await self.telegram.send("\n".join(lines), raise_on_error=raise_on_error)

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
            "leverage": self.cfg.leverage,
            "laggard_exchange": self.cfg.laggard_exchange,
            "basis_filter": self.cfg.basis_filter,
            "open_positions": len(self.store.open),
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
                "raw_spread_pct": round(self._last_gross_pct.get(c, 0.0), 4),
                "basis_pct": round(self._last_basis_pct.get(c, 0.0), 4),
                "signal_state": self._last_signal_state.get(c, "WAITING"),
                "in_trade": c in self.store.open,
                "paused": c in self.paused,
                "sparkline": list(self._sparkline.get(c, [])),
                "heat": heat,
                "near_hurdle_hits_60s": sum(1 for r in heat if abs(r) >= self.NEAR_HURDLE_RATIO),
                "peak_ratio_60s": round(max((abs(r) for r in heat), default=0.0), 3),
                "liquidity": self._last_liq.get(c),
            })
        return out
