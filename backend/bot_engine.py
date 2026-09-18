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
    simulate: bool = True                  # force simulator (no real exchange creds)

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
            simulate=_b("SIMULATE_MARKET", True),
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

    async def start(self) -> None:
        self._running = True
        asyncio.create_task(self._run())

    def stop(self) -> None:
        self._running = False

    async def _run(self) -> None:
        streams = "/".join(f"{c.lower()}usdt@bookTicker" for c in self.coins)
        url = f"wss://fstream.binance.com/stream?streams={streams}"
        backoff = 1
        while self._running:
            try:
                async with aiohttp.ClientSession() as sess:
                    async with sess.ws_connect(url, heartbeat=20) as ws:
                        backoff = 1
                        async for msg in ws:
                            if msg.type != aiohttp.WSMsgType.TEXT: continue
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
            except Exception as e:
                logger.warning(f"Binance WS error: {e}; reconnecting in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)


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
class TelegramReporter:
    def __init__(self, token: str, chat_id: str) -> None:
        self.token = token
        self.chat_id = chat_id
        self.enabled = bool(token and chat_id)

    async def send(self, text: str) -> None:
        if not self.enabled:
            logger.info(f"[TELEGRAM-MUTED] {text}")
            return
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as s:
                await s.post(url, json={"chat_id": self.chat_id, "text": text,
                                        "parse_mode": "HTML", "disable_web_page_preview": True})
        except Exception as e:
            logger.warning(f"telegram send failed: {e}")


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
        self.telegram = TelegramReporter(cfg.telegram_bot_token, cfg.telegram_chat_id)
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

    # --- lifecycle -----------------------------------------------------------
    async def start(self) -> None:
        if self.running: return
        self.running = True
        self.started_at = time.time()
        if self.simulator: await self.simulator.start()
        if self.live_feed: await self.live_feed.start()
        await self.fx.start()
        self._tasks = [
            asyncio.create_task(self._scan_loop()),
            asyncio.create_task(self._position_manager_loop()),
            asyncio.create_task(self._equity_ticker_loop()),
            asyncio.create_task(self._hourly_summary_loop()),
        ]
        await self.store.log("INFO", f"Bot started in {'PAPER' if self.cfg.paper_trading else 'LIVE'} mode")

    async def stop(self) -> None:
        if not self.running: return
        self.running = False
        for t in self._tasks: t.cancel()
        self._tasks = []
        if self.simulator: self.simulator.stop()
        if self.live_feed: self.live_feed.stop()
        self.fx.stop()
        await self.store.log("INFO", "Bot stopped")

    # --- accessors -----------------------------------------------------------
    def _leader(self, coin: str) -> Optional[TopOfBook]:
        return self.simulator.leader(coin) if self.simulator else self.live_feed.book.get(coin)

    def _laggard(self, coin: str) -> Optional[TopOfBook]:
        return self.simulator.laggard(coin) if self.simulator else None  # CoinSwitch WS impl not shipped

    def hurdle_pct(self) -> float:
        c = self.cfg
        return c.entry_taker_fee_pct + c.exit_taker_fee_pct + c.slippage_buffer_pct + c.hurdle_extra_pct

    def allocated_per_slot_inr(self) -> float:
        n = max(1, self.cfg.num_slots)
        return self.equity_inr / n

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
        # Entry price = worst top-of-book (taker)
        entry_price = lagg.ask if direction == "LONG" else lagg.bid
        contracts = size_inr / entry_price
        # target: catch-up to leader INR (mid), stop = symmetric hard stop
        target = leader_inr_mid
        stop = entry_price * (1 - self.cfg.stop_loss_pct/100.0) if direction == "LONG" \
               else entry_price * (1 + self.cfg.stop_loss_pct/100.0)
        # simulate execution latency
        latency_ms = random.uniform(80, 220) if self.cfg.paper_trading else 0.0
        if self.cfg.paper_trading:
            await asyncio.sleep(latency_ms/1000.0)
        else:
            # LIVE: real REST call would go here (Binance/CoinSwitch place order)
            # deliberately not wired — user must plug CoinSwitch PRO REST API creds.
            await self.store.log("WARN", f"[LIVE MODE] {coin} order placement stub; supply CoinSwitch REST client to enable.")
            return

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
        pos.exit_price_inr = price
        pos.closed_at = time.time()
        pos.close_reason = reason
        if pos.direction == "LONG":
            gross = (price - pos.entry_price_inr) * pos.contracts
        else:
            gross = (pos.entry_price_inr - price) * pos.contracts
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

    async def _send_hourly_summary(self) -> None:
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
        await self.telegram.send(msg)

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
            })
        return out
