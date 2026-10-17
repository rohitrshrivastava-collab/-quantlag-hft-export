"""FastAPI server for QuantLag HFT dashboard.
Hosts the async bot engine (paper mode with simulator by default) and
exposes REST endpoints under /api/* consumed by the React dashboard.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import math
import os
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from dataclasses import asdict
from dotenv import load_dotenv
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, Field
from starlette.middleware.cors import CORSMiddleware

from bot_engine import BotConfig, BotEngine
from coindcx_client import CoinDCXError
from replay import replay_session, replay_sweep
from screener import screen, build_universe
from shadow import ShadowRunner
from verdict import evaluate as evaluate_verdict

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")
sys.path.insert(0, str(ROOT_DIR.parent))
from benchmark.latency_bench import LatencyBenchmark  # noqa: E402

BENCH_RESULT_PATH = ROOT_DIR.parent / "benchmark" / "last_result.json"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("server")

# --- Mongo (required by env) — used only for closed-trade history persistence
mongo_url = os.environ["MONGO_URL"]
mongo_client = AsyncIOMotorClient(mongo_url)
db = mongo_client[os.environ["DB_NAME"]]

# --- Bot singleton -----------------------------------------------------------
CFG = BotConfig.from_env()
ENGINE = BotEngine(CFG)
SHADOW = ShadowRunner(ENGINE)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Auto-start bot in paper mode on server boot (dashboard shows live data)
    await ENGINE.start()
    await SHADOW.start()
    trial_task = asyncio.create_task(coinswitch_trial_watcher())
    universe_task = asyncio.create_task(universe_watcher())
    try:
        yield
    finally:
        trial_task.cancel()
        universe_task.cancel()
        await ENGINE.stop()
        mongo_client.close()


app = FastAPI(lifespan=lifespan)
api = APIRouter(prefix="/api")


# ------------------------- Schemas ------------------------------------------
class ModeUpdate(BaseModel):
    paper_trading: bool


class ConfigUpdate(BaseModel):
    entry_taker_fee_pct: Optional[float] = None
    exit_taker_fee_pct: Optional[float] = None
    slippage_buffer_pct: Optional[float] = None
    hurdle_extra_pct: Optional[float] = None
    timeout_seconds: Optional[int] = None
    stop_loss_pct: Optional[float] = None
    basis_filter: Optional[bool] = None
    basis_window_s: Optional[int] = Field(default=None, ge=30, le=3600)
    max_slippage_pct: Optional[float] = Field(default=None, ge=0, le=2)
    phantom_frozen_s: Optional[float] = Field(default=None, ge=0.5, le=60)
    min_order_inr: Optional[float] = Field(default=None, ge=1, le=1e6)
    max_entry_frozen_s: Optional[float] = Field(default=None, ge=0.1, le=60)
    spike_persist_ms: Optional[int] = Field(default=None, ge=0, le=10000)
    max_book_share_pct: Optional[float] = Field(default=None, ge=1, le=100)
    stop_mode: Optional[str] = Field(default=None, pattern="^(dynamic|fixed)$")
    vol_window_s: Optional[int] = Field(default=None, ge=60, le=3600)
    vol_k: Optional[float] = Field(default=None, ge=0.5, le=6)
    stop_min_pct: Optional[float] = Field(default=None, ge=0.05, le=5)
    stop_max_pct: Optional[float] = Field(default=None, ge=0.1, le=10)
    breakeven_floor: Optional[bool] = None
    auto_universe: Optional[bool] = None
    max_coin_price_usd: Optional[float] = Field(default=None, ge=0, le=1e9)


class AccountUpdate(BaseModel):
    equity_inr: Optional[float] = Field(default=None, gt=0, le=1e9)
    num_slots: Optional[int] = Field(default=None, ge=1, le=50)
    leverage: Optional[int] = Field(default=None, ge=1, le=100)


# ------------------------- Routes -------------------------------------------
@api.get("/")
async def root():
    return {"service": "QuantLag HFT Console", "version": "1.0.0"}


@api.get("/status")
async def status():
    return ENGINE.snapshot()


@api.post("/bot/start")
async def bot_start():
    await ENGINE.start()
    return {"ok": True, "running": ENGINE.running}


@api.post("/bot/stop")
async def bot_stop():
    await ENGINE.stop()
    return {"ok": True, "running": ENGINE.running}


@api.post("/bot/mode")
async def bot_mode(body: ModeUpdate):
    ENGINE.cfg.paper_trading = body.paper_trading
    ENGINE.persist()
    mode = "PAPER" if body.paper_trading else "LIVE"
    warning = None
    if not body.paper_trading and not ENGINE._live_executor_ready():
        warning = f"{ENGINE.cfg.laggard_exchange.upper()} keys not configured — LIVE mode will not place orders. Set the API key/secret in backend/.env and restart the service."
    # reset the once-per-flip warning latch so the next transition can log again
    ENGINE._live_missing_keys_warned = False
    await ENGINE.store.log("INFO", f"Mode switched to {mode}")
    return {"ok": True, "mode": mode, "warning": warning}


class LaggardUpdate(BaseModel):
    exchange: str


@api.post("/laggard")
async def set_laggard(body: LaggardUpdate):
    """Switch the traded pair: Binance → coinswitch | coindcx. Refuses with open positions."""
    try:
        await ENGINE.set_laggard_exchange(body.exchange)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    WALLET_CACHE["ts"] = 0.0
    warning = None
    if not ENGINE.cfg.paper_trading and not ENGINE._live_executor_ready():
        warning = f"{ENGINE.cfg.laggard_exchange.upper()} keys not configured — LIVE mode will not place orders on this pair."
    return {"ok": True, "laggard_exchange": ENGINE.cfg.laggard_exchange, "warning": warning, "status": ENGINE.snapshot()}


WALLET_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None}
TRIAL: Dict[str, Any] = {"armed": False, "waiting": False, "last_usdt": None, "message": "", "armed_at": None}


async def coinswitch_trial_watcher() -> None:
    """Arms the 1-slot CoinSwitch trial the moment USDT lands in the futures wallet."""
    while True:
        try:
            if (ENGINE.coinswitch and ENGINE.cfg.laggard_exchange == "coinswitch"
                    and not ENGINE.cfg.paper_trading and not TRIAL["armed"]):
                w = await ENGINE.wallet()
                usdt = next((b["balance"] for b in w.get("balances", []) if b["currency"] == "USDT"), 0.0)
                TRIAL["last_usdt"] = usdt
                TRIAL["waiting"] = usdt < 4.0
                if usdt >= 4.0 and not await ENGINE.store.all_open():
                    equity = round(usdt * max(ENGINE.fx.rate, 1e-6) * 0.98, 2)
                    if ENGINE.cfg.num_slots == 1 and int(ENGINE.cfg.leverage) == 10 and abs(ENGINE.equity_inr - equity) / max(equity, 1) < 0.1:
                        TRIAL.update(armed=True, waiting=False, message=f"CoinSwitch trial active: {usdt:.2f} USDT, equity ₹{ENGINE.equity_inr:.2f}, 1 slot, 10x")
                        continue   # already armed before a reload — don't reset stats again
                    await ENGINE.reset_account(equity)
                    ENGINE.cfg.num_slots = 1
                    errors = await ENGINE.apply_leverage(10)
                    ENGINE.persist()
                    TRIAL.update(armed=True, waiting=False, armed_at=time.time(),
                                 message=f"CoinSwitch trial armed: {usdt:.2f} USDT → equity ₹{equity}, 1 slot, 10x" + (f" (leverage errors: {errors[:2]})" if errors else ""))
                    await ENGINE.store.log("INFO", TRIAL["message"])
                    try: await ENGINE.telegram.send(TRIAL["message"])
                    except Exception: pass
                else:
                    TRIAL["message"] = f"Waiting for USDT in CoinSwitch futures wallet (now {usdt:.4f} USDT, need ≥ 4)"
            else:
                TRIAL["waiting"] = False
        except Exception as e:
            TRIAL["message"] = f"trial watcher error: {e}"
        await asyncio.sleep(20)


@api.get("/coinswitch/trial")
async def coinswitch_trial():
    return {**TRIAL, "laggard_exchange": ENGINE.cfg.laggard_exchange, "mode": "PAPER" if ENGINE.cfg.paper_trading else "LIVE"}


UNIVERSE: Dict[str, Any] = {"last": None, "selected": None, "applied_at": None, "error": None, "runs": 0, "ticks": 0, "skipped": None}


async def universe_watcher() -> None:
    """Continuously rebuild the traded shortlist (liquidity × volatility × max-price, both exchanges).

    Applies a swap via set_coins only when the bot is live, auto-universe is on, and no
    positions are open (a swap restarts feeds). The hard max-price cap is ALSO enforced
    instantly at scan time in the engine, so it takes effect even between swaps."""
    await asyncio.sleep(5)  # let feeds warm up before the first rebuild
    while True:
        delay = 60
        try:
            UNIVERSE["ticks"] += 1
            c = ENGINE.cfg
            delay = max(20, int(c.universe_refresh_s))
            if not (c.auto_universe and ENGINE.running):
                UNIVERSE["skipped"] = "auto-universe off or bot stopped"
            elif await ENGINE.store.all_open():
                UNIVERSE["skipped"] = "positions open (swap deferred; price cap still enforced live)"
            else:
                UNIVERSE["skipped"] = None
                res = await build_universe(
                    laggard=c.laggard_exchange, top_n=max(1, c.num_slots),
                    max_price_usd=c.max_coin_price_usd,
                    min_volume_usdt=c.universe_min_volume_usdt,
                    min_vol_pct=c.universe_min_vol_pct,
                    keep=list(c.coins))
                UNIVERSE.update(last=res, error=None, runs=UNIVERSE["runs"] + 1)
                sel = res.get("selected") or []
                if sel and set(sel) != set(c.coins):
                    try:
                        await ENGINE.set_coins(sel)
                        UNIVERSE.update(selected=sel, applied_at=time.time())
                        await ENGINE.store.log("INFO", f"Auto-universe → {len(sel)} coins: {', '.join(sel)}")
                    except RuntimeError:
                        pass  # a position opened between the check and the swap; try next cycle
                else:
                    UNIVERSE["selected"] = list(c.coins)
        except Exception as e:
            UNIVERSE["error"] = str(e)
        await asyncio.sleep(delay)


@api.get("/universe/auto")
async def universe_auto():
    c = ENGINE.cfg
    return {"auto_universe": c.auto_universe, "coins": c.coins, "num_slots": c.num_slots,
            "max_coin_price_usd": c.max_coin_price_usd,
            "min_volume_usdt": c.universe_min_volume_usdt, "min_vol_pct": c.universe_min_vol_pct,
            "refresh_s": c.universe_refresh_s, **UNIVERSE}


@api.get("/wallet")
async def wallet(force: bool = False):
    """Futures wallet of the active pair's laggard exchange, cached 10 s to respect rate limits."""
    if force or WALLET_CACHE["data"] is None or time.time() - WALLET_CACHE["ts"] > 10 or WALLET_CACHE["data"].get("exchange") != ENGINE.cfg.laggard_exchange:
        WALLET_CACHE["data"] = await ENGINE.wallet()
        WALLET_CACHE["ts"] = time.time()
    return WALLET_CACHE["data"]


@api.get("/config")
async def get_config():
    c = ENGINE.cfg
    return {
        "paper_trading": c.paper_trading,
        "entry_taker_fee_pct": c.entry_taker_fee_pct,
        "exit_taker_fee_pct": c.exit_taker_fee_pct,
        "slippage_buffer_pct": c.slippage_buffer_pct,
        "hurdle_extra_pct": c.hurdle_extra_pct,
        "hurdle_pct": ENGINE.hurdle_pct(),
        "timeout_seconds": c.timeout_seconds,
        "stop_loss_pct": c.stop_loss_pct,
        "starting_capital_inr": c.starting_capital_inr,
        "num_slots": c.num_slots,
        "coins": c.coins,
        "simulate": c.simulate,
        "basis_filter": c.basis_filter,
        "basis_window_s": c.basis_window_s,
        "leverage": c.leverage,
        "laggard_exchange": c.laggard_exchange,
        "coindcx_client_ready": ENGINE.coindcx is not None,
        "digest_time_ist": c.digest_time_ist,
        "max_slippage_pct": c.max_slippage_pct,
        "phantom_frozen_s": c.phantom_frozen_s,
        "min_order_inr": c.min_order_inr,
        "max_entry_frozen_s": c.max_entry_frozen_s,
        "spike_persist_ms": c.spike_persist_ms,
        "scan_interval_s": c.scan_interval_s,
        "max_book_share_pct": c.max_book_share_pct,
        "stop_mode": c.stop_mode,
        "vol_window_s": c.vol_window_s,
        "vol_k": c.vol_k,
        "stop_min_pct": c.stop_min_pct,
        "stop_max_pct": c.stop_max_pct,
        "breakeven_floor": c.breakeven_floor,
        "depth_bps": c.depth_bps,
        "auto_universe": c.auto_universe,
        "max_coin_price_usd": c.max_coin_price_usd,
        "universe_min_volume_usdt": c.universe_min_volume_usdt,
        "universe_min_vol_pct": c.universe_min_vol_pct,
    }


@api.post("/account")
async def update_account(body: AccountUpdate):
    """Manual Total Equity / slots / leverage. Equity reset requires no open positions."""
    changes: Dict[str, Any] = {}
    open_n = len(await ENGINE.store.all_open())
    if body.equity_inr is not None:
        if open_n:
            raise HTTPException(status_code=409, detail=f"close {open_n} open position(s) before resetting equity")
        await ENGINE.reset_account(body.equity_inr)
        changes["equity_inr"] = body.equity_inr
    if body.num_slots is not None:
        ENGINE.cfg.num_slots = body.num_slots
        changes["num_slots"] = body.num_slots
        await ENGINE.store.log("INFO", f"Slots set to {body.num_slots} (₹{ENGINE.allocated_per_slot_inr():.2f}/slot)")
    errors: List[str] = []
    if body.leverage is not None:
        errors = await ENGINE.apply_leverage(body.leverage)
        changes["leverage"] = body.leverage
    ENGINE.persist()
    return {"ok": True, "changes": changes, "leverage_errors": errors, "status": ENGINE.snapshot()}


@api.post("/coindcx/check")
async def coindcx_check():
    """Preflight: validates CoinDCX keys via futures wallet + positions."""
    if ENGINE.coindcx is None:
        raise HTTPException(status_code=400, detail="CoinDCX keys not configured — set COINDCX_API_KEY/SECRET in backend/.env and restart")
    try:
        margin_ccy = await ENGINE.coindcx.autodetect_margin_currency()
        wallets = await ENGINE.coindcx.wallet_balance()
        positions = await ENGINE.coindcx.positions()
    except CoinDCXError as e:
        hint = " (401 = key/secret rejected: check IP whitelist, Futures permission, or re-copy the secret)" if e.status == 401 else ""
        raise HTTPException(status_code=424, detail=f"CoinDCX API {e.status}: {e.body}{hint}")
    except Exception as e:
        raise HTTPException(status_code=424, detail=f"CoinDCX unreachable: {e}")
    await ENGINE.store.log("INFO", f"CoinDCX preflight OK — wallet reachable · margin currency {margin_ccy}")
    return {"ok": True, "margin_currency": margin_ccy, "wallets": wallets, "positions": positions if isinstance(positions, list) else positions,
            "rate_limiter": ENGINE.coindcx.limiter_stats()}


class CoinDCXTestOrder(BaseModel):
    coin: str = "DOGE"
    notional_usdt: float = Field(default=6.0, ge=1, le=50)
    leverage: int = Field(default=1, ge=1, le=10)
    confirm: bool = False


@api.post("/coindcx/test-order")
async def coindcx_test_order(body: CoinDCXTestOrder):
    """Tiny round-trip on Pair B: market BUY then immediate reduce-only market SELL. Real money — requires confirm."""
    if ENGINE.coindcx is None:
        raise HTTPException(status_code=400, detail="CoinDCX keys not configured")
    if not body.confirm:
        raise HTTPException(status_code=400, detail="set confirm=true — this places REAL orders on CoinDCX Futures")
    coin = body.coin.upper()
    c = ENGINE.coindcx
    steps: List[Dict[str, Any]] = []
    try:
        margin_ccy = await c.autodetect_margin_currency()
        wallets = await c.wallet_balance()
        free = {str(w.get("currency_short_name", "")).upper(): float(w.get("balance") or 0) for w in (wallets if isinstance(wallets, list) else [])}
        steps.append({"step": "wallet", "ok": True, "margin_currency": margin_ccy, "free": free})
        inst = await c.instrument(coin)
        px = await c.last_price(coin)
        if px <= 0: raise HTTPException(status_code=424, detail=f"no CoinDCX price for {coin}")
        min_notional = float(inst.get("min_notional") or 6)
        notional = max(body.notional_usdt, min_notional * 1.05)   # exchange converts min at its own FX — keep a buffer
        step = float(inst.get("quantity_increment") or 1e-6)
        qty = math.ceil((notional / px) / step) * step
        qty = max(qty, float(inst.get("min_quantity") or step))
        qty = float(f"{qty:.10g}")
        need = qty * px * (ENGINE.fx.rate if margin_ccy == "INR" else 1.0) * 1.02 / body.leverage
        if free.get(margin_ccy, 0) < need:
            raise HTTPException(status_code=424, detail={"message": f"insufficient free {margin_ccy} margin: have {free.get(margin_ccy, 0):.2f}, need ≈ {need:.2f}", "steps": steps})
        steps.append({"step": "sizing", "ok": True, "price": px, "qty": qty, "notional_usdt": round(qty * px, 4),
                      "margin_needed": round(need, 2), "margin_currency": margin_ccy,
                      "min_notional": min_notional, "taker_fee": inst.get("taker_fee"), "quantity_increment": step})
        try:
            await c.update_leverage(coin, body.leverage)
            steps.append({"step": "leverage", "ok": True, "leverage": body.leverage})
        except CoinDCXError as e:
            steps.append({"step": "leverage", "ok": False, "error": f"{e.status}: {e.body}"})
        t0 = time.perf_counter()
        opened = await c.market_order(coin, "buy", qty, body.leverage)
        oid = str(opened.get("id") or "")
        fill = await c.wait_for_fill(oid, "buy", timeout_s=6) or opened
        steps.append({"step": "buy", "ok": True, "order_id": oid, "status": fill.get("status"), "avg_price": fill.get("avg_price"),
                      "fee": fill.get("fee_amount"), "latency_ms": round((time.perf_counter() - t0) * 1000)})
        t1 = time.perf_counter()
        closed = await c.market_order(coin, "sell", qty, body.leverage)
        cid = str(closed.get("id") or "")
        cfill = await c.wait_for_fill(cid, "sell", timeout_s=6) or closed
        steps.append({"step": "sell_close", "ok": True, "order_id": cid, "status": cfill.get("status"), "avg_price": cfill.get("avg_price"),
                      "fee": cfill.get("fee_amount"), "latency_ms": round((time.perf_counter() - t1) * 1000)})
        positions = await c.positions([c.pair(coin)])
        flat = all(float(p.get("active_pos") or 0) == 0 for p in (positions if isinstance(positions, list) else []))
        steps.append({"step": "verify_flat", "ok": flat, "positions": positions})
    except CoinDCXError as e:
        steps.append({"step": "error", "ok": False, "error": f"CoinDCX API {e.status}: {e.body}"})
        await ENGINE.store.log("ERROR", f"CoinDCX test order failed at step {len(steps)}: {e.status} {e.body}")
        raise HTTPException(status_code=424, detail={"message": f"CoinDCX API {e.status}: {e.body}", "steps": steps})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=424, detail={"message": f"test order failed: {e}", "steps": steps})
    buy_px = float(steps[3].get("avg_price") or 0) if len(steps) > 3 else 0
    sell_px = float(steps[4].get("avg_price") or 0) if len(steps) > 4 else 0
    fees = sum(float(s.get("fee") or 0) for s in steps if s["step"] in ("buy", "sell_close"))
    cost = round((buy_px - sell_px) * qty + fees, 6) if buy_px and sell_px else None
    await ENGINE.store.log("TRADE", f"[COINDCX TEST] {coin} round-trip qty={qty} buy={buy_px} sell={sell_px} fees={fees} USDT cost≈{cost}")
    await ENGINE.telegram.send(f"🧪 <b>CoinDCX test order</b> {coin}\nqty {qty} · buy {buy_px} → sell {sell_px}\nfees {fees} USDT · round-trip cost ≈ {cost} USDT\nflat: {'yes' if steps[-1].get('ok') else 'CHECK POSITION'}")
    return {"ok": True, "coin": coin, "qty": qty, "round_trip_cost_usdt": cost, "flat": steps[-1].get("ok"), "steps": steps}


class ReplayRequest(BaseModel):
    date: str
    hurdle_pct: Optional[float] = Field(default=None, gt=0, le=10)
    basis_filter: Optional[bool] = None
    basis_window_s: Optional[int] = Field(default=None, ge=30, le=3600)
    leverage: Optional[int] = Field(default=None, ge=1, le=100)
    num_slots: Optional[int] = Field(default=None, ge=1, le=50)
    equity_inr: Optional[float] = Field(default=None, gt=0)
    timeout_seconds: Optional[int] = Field(default=None, ge=1, le=3600)
    stop_loss_pct: Optional[float] = Field(default=None, gt=0, le=50)


REPLAY: Dict[str, Any] = {"running": False, "result": None, "error": None}


@api.get("/replay/sessions")
async def replay_sessions():
    return {"items": ENGINE.recorder.sessions(), "recorder": ENGINE.recorder.status()}


@api.post("/replay/run")
async def replay_run(body: ReplayRequest):
    if REPLAY["running"]:
        raise HTTPException(status_code=409, detail="replay already running")
    path = ENGINE.recorder.root / f"{body.date}.jsonl"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"no recording for {body.date}")
    c = ENGINE.cfg
    kw = dict(coins=c.coins,
              hurdle_pct=body.hurdle_pct if body.hurdle_pct is not None else ENGINE.hurdle_pct(),
              basis_filter=c.basis_filter if body.basis_filter is None else body.basis_filter,
              basis_window_s=body.basis_window_s or c.basis_window_s,
              leverage=body.leverage or c.leverage, num_slots=body.num_slots or c.num_slots,
              equity_inr=body.equity_inr or c.starting_capital_inr,
              timeout_seconds=body.timeout_seconds or c.timeout_seconds,
              stop_loss_pct=body.stop_loss_pct or c.stop_loss_pct,
              fee_round_trip_pct=c.entry_taker_fee_pct + c.exit_taker_fee_pct)
    REPLAY.update({"running": True, "error": None})
    try:
        await asyncio.to_thread(ENGINE.recorder._flush)
        res = await asyncio.to_thread(replay_session, path, **kw)
        REPLAY["result"] = res
        await ENGINE.store.log("INFO", f"Replay {body.date}: {res['trades']} trades, net ₹{res['net_pnl_inr']:+.2f} @ hurdle {kw['hurdle_pct']:.3f}%")
        return res
    except Exception as e:
        REPLAY["error"] = str(e)
        raise HTTPException(status_code=500, detail=f"replay failed: {e}")
    finally:
        REPLAY["running"] = False


@api.get("/replay/last")
async def replay_last():
    return REPLAY


class SweepRequest(BaseModel):
    date: str
    hurdles: List[float] = Field(default_factory=lambda: [0.05, 0.08, 0.10, 0.12, 0.15, 0.20])
    basis_windows: List[int] = Field(default_factory=lambda: [60, 180, 300, 600])
    leverage: Optional[int] = Field(default=None, ge=1, le=100)
    num_slots: Optional[int] = Field(default=None, ge=1, le=50)
    basis_filter: bool = True


@api.post("/replay/sweep")
async def replay_sweep_run(body: SweepRequest):
    if REPLAY["running"]:
        raise HTTPException(status_code=409, detail="replay already running")
    if not body.hurdles or not body.basis_windows:
        raise HTTPException(status_code=422, detail="hurdles and basis_windows must be non-empty")
    if len(body.hurdles) > 12 or len(body.basis_windows) > 8:
        raise HTTPException(status_code=400, detail="grid too large (max 12 hurdles × 8 windows)")
    if any(h <= 0 or h > 10 for h in body.hurdles) or any(w < 30 or w > 3600 for w in body.basis_windows):
        raise HTTPException(status_code=422, detail="hurdles in (0,10], windows in [30,3600]")
    path = ENGINE.recorder.root / f"{body.date}.jsonl"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"no recording for {body.date}")
    c = ENGINE.cfg
    REPLAY.update({"running": True, "error": None})
    try:
        await asyncio.to_thread(ENGINE.recorder._flush)
        res = await asyncio.to_thread(
            replay_sweep, path, hurdles=body.hurdles, basis_windows=body.basis_windows, coins=c.coins,
            basis_filter=body.basis_filter, leverage=body.leverage or c.leverage, num_slots=body.num_slots or c.num_slots,
            equity_inr=c.starting_capital_inr, timeout_seconds=c.timeout_seconds, stop_loss_pct=c.stop_loss_pct,
            fee_round_trip_pct=c.entry_taker_fee_pct + c.exit_taker_fee_pct)
        REPLAY["sweep"] = res
        b = res["best"]
        if b: await ENGINE.store.log("INFO", f"Sweep {body.date}: best hurdle {b['hurdle_pct']}% / window {b['basis_window_s']}s → ₹{b['net_pnl_inr']:+.2f} ({b['trades']} trades)")
        return res
    except Exception as e:
        REPLAY["error"] = str(e)
        raise HTTPException(status_code=500, detail=f"sweep failed: {e}")
    finally:
        REPLAY["running"] = False


# ------------------------- Screener / universe --------------------------------
SCREEN: Dict[str, Any] = {"result": None}


@api.get("/screener")
async def screener_run(top_n: int = 10, min_volume_usdt: float = 5e6):
    tick_file = ENGINE.recorder.root / (datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%Y-%m-%d") + ".jsonl")
    try:
        res = await screen(current=ENGINE.cfg.coins, top_n=max(1, min(top_n, 30)), tick_file=tick_file,
                           hurdle_bps=ENGINE.hurdle_pct() * 100.0, min_volume_usdt=min_volume_usdt)
    except Exception as e:
        raise HTTPException(status_code=424, detail=f"screener data fetch failed: {e}")
    res["current"] = ENGINE.cfg.coins
    SCREEN["result"] = res
    return res


class UniverseUpdate(BaseModel):
    coins: List[str] = Field(min_length=1, max_length=30)


@api.post("/universe")
async def universe_apply(body: UniverseUpdate):
    try:
        await ENGINE.set_coins(body.coins)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"ok": True, "coins": ENGINE.cfg.coins, "running": ENGINE.running}


@api.get("/analytics/coins")
async def analytics_coins():
    return {"items": ENGINE.coin_analytics()}


@api.post("/telegram/digest")
async def telegram_digest_now():
    if not ENGINE.telegram.enabled:
        raise HTTPException(status_code=400, detail="Telegram not configured")
    try:
        await ENGINE.send_daily_digest(raise_on_error=True, bench=BENCH.get("result"))
    except Exception as e:
        raise HTTPException(status_code=424, detail=f"Telegram error: {e}")
    return {"ok": True, "status": ENGINE.telegram.status()}


@api.put("/config")
async def update_config(body: ConfigUpdate):
    c = ENGINE.cfg
    data = body.model_dump(exclude_none=True)
    for k, v in data.items():
        setattr(c, k, v)
    ENGINE.persist()
    await ENGINE.store.log("INFO", f"Config updated: {data}")
    return await get_config()


@api.get("/spreads")
async def spreads():
    return {"items": ENGINE.spreads_snapshot(), "hurdle_pct": ENGINE.hurdle_pct()}


@api.get("/compare")
async def compare():
    """Head-to-head shadow simulation: CoinSwitch vs CoinDCX on identical rules, live books, own fees/latency."""
    return SHADOW.report()


@api.post("/compare/reset")
async def compare_reset():
    SHADOW.reset()
    await ENGINE.store.log("INFO", "Shadow comparison stats reset")
    return SHADOW.report()


@api.get("/liquidity/{coin}")
async def liquidity(coin: str, notional_inr: Optional[float] = None, direction: str = "LONG"):
    coin = coin.upper()
    if coin not in ENGINE.cfg.coins:
        raise HTTPException(status_code=404, detail="coin not in universe")
    notional = notional_inr or ENGINE.allocated_per_slot_inr() * max(1, ENGINE.cfg.leverage)
    liq = ENGINE.liquidity(coin, direction.upper(), notional)
    if liq is None:
        raise HTTPException(status_code=503, detail="no order-book depth yet for this coin")
    feed = ENGINE.coinswitch_feed
    bids, asks = feed.depth.get(coin, ([], []))
    fx = ENGINE.fx.rate
    return {"coin": coin, "direction": direction.upper(), **liq,
            "book_inr": {"bids": [[round(p * fx, 6), q] for p, q in bids[:10]], "asks": [[round(p * fx, 6), q] for p, q in asks[:10]]}}


@api.get("/positions/open")
async def positions_open():
    return {"items": [{**asdict(p), **ENGINE.unrealized(p)} for p in await ENGINE.store.all_open()]}


@api.post("/positions/{coin}/close")
async def positions_close(coin: str):
    pos = await ENGINE.store.get_open(coin)
    if not pos:
        raise HTTPException(status_code=404, detail="no open position for coin")
    lagg = ENGINE._laggard(coin)
    if not lagg:
        raise HTTPException(status_code=503, detail="no market data")
    price = lagg.bid if pos.direction == "LONG" else lagg.ask
    await ENGINE._close_position(pos, price, "MANUAL")
    return {"ok": True}


@api.get("/positions/closed")
async def positions_closed(limit: int = 200, coin: Optional[str] = None):
    items = [asdict(p) for p in await ENGINE.store.all_closed(limit=limit)]
    if coin:
        items = [p for p in items if p["coin"] == coin.upper()]
    return {"items": items}


CSV_COLUMNS = ["id", "coin", "direction", "mode", "size_inr", "contracts", "entry_price_inr", "exit_price_inr",
               "binance_ref_usdt", "target_price_inr", "stop_price_inr", "opened_at", "closed_at",
               "gross_pnl_inr", "fees_inr", "net_pnl_inr", "close_reason", "exec_latency_ms",
               "entry_order_id", "exit_order_id"]


@api.get("/positions/closed/export.csv")
async def positions_closed_csv(coin: Optional[str] = None):
    rows = [asdict(p) for p in await ENGINE.store.all_closed(limit=5000)]
    if coin:
        rows = [r for r in rows if r["coin"] == coin.upper()]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_COLUMNS + ["opened_at_iso", "closed_at_iso", "hold_sec"], extrasaction="ignore")
    w.writeheader()
    for r in rows:
        r["opened_at_iso"] = datetime.fromtimestamp(r["opened_at"], tz=timezone.utc).isoformat()
        r["closed_at_iso"] = datetime.fromtimestamp(r["closed_at"], tz=timezone.utc).isoformat() if r.get("closed_at") else ""
        r["hold_sec"] = round(r["closed_at"] - r["opened_at"], 2) if r.get("closed_at") else ""
        w.writerow(r)
    fname = f"quantlag_trades_{coin.upper() if coin else 'all'}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.csv"
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@api.post("/coins/{coin}/pause")
async def coin_pause(coin: str):
    if not ENGINE.set_paused(coin.upper(), True):
        raise HTTPException(status_code=404, detail="unknown coin")
    await ENGINE.store.log("WARN", f"{coin.upper()} slot PAUSED — no new entries (open position still managed)")
    return {"ok": True, "coin": coin.upper(), "paused": True, "paused_coins": sorted(ENGINE.paused)}


@api.post("/coins/{coin}/resume")
async def coin_resume(coin: str):
    if not ENGINE.set_paused(coin.upper(), False):
        raise HTTPException(status_code=404, detail="unknown coin")
    await ENGINE.store.log("INFO", f"{coin.upper()} slot RESUMED")
    return {"ok": True, "coin": coin.upper(), "paused": False, "paused_coins": sorted(ENGINE.paused)}


@api.get("/hurdle/simulate")
async def hurdle_simulate(hurdle_pct: float, window_s: int = 600):
    if hurdle_pct <= 0 or hurdle_pct > 10:
        raise HTTPException(status_code=400, detail="hurdle_pct must be in (0, 10]")
    draft = ENGINE.hurdle_simulate(hurdle_pct, window_s)
    current = ENGINE.hurdle_simulate(ENGINE.hurdle_pct(), window_s)
    return {"draft": draft, "current": current}


# ------------------------- Benchmark ------------------------------------------
BENCH: Dict[str, Any] = {"running": False, "bench": None, "task": None, "result": None, "error": None,
                         "started_at": None}
if BENCH_RESULT_PATH.exists():
    try:
        BENCH["result"] = json.loads(BENCH_RESULT_PATH.read_text())
    except Exception:
        pass


class BenchmarkRequest(BaseModel):
    duration_s: float = Field(default=60, ge=10, le=600)
    symbols: List[str] = Field(default_factory=lambda: ["BTC", "ETH", "SOL"])


async def _run_benchmark(bench: LatencyBenchmark) -> None:
    try:
        res = await bench.run()
        BENCH["result"] = res
        BENCH_RESULT_PATH.write_text(json.dumps(res))
        await ENGINE.store.log("INFO", f"Latency benchmark finished ({bench.duration:.0f}s, {', '.join(bench.symbols)})")
    except Exception as e:
        BENCH["error"] = str(e)
        await ENGINE.store.log("ERROR", f"Latency benchmark failed: {e}")
    finally:
        BENCH["running"] = False
        BENCH["bench"] = None


@api.post("/benchmark/run")
async def benchmark_run(body: BenchmarkRequest):
    if BENCH["running"]:
        raise HTTPException(status_code=409, detail="benchmark already running")
    bench = LatencyBenchmark(body.duration_s, [s.upper() for s in body.symbols][:5])
    BENCH.update({"running": True, "bench": bench, "error": None, "started_at": datetime.now(timezone.utc).timestamp()})
    BENCH["task"] = asyncio.create_task(_run_benchmark(bench))
    await ENGINE.store.log("INFO", f"Latency benchmark started ({body.duration_s:.0f}s)")
    return {"ok": True, "running": True, "duration_s": body.duration_s, "symbols": bench.symbols}


@api.get("/benchmark/status")
async def benchmark_status():
    bench: Optional[LatencyBenchmark] = BENCH["bench"]
    return {
        "running": BENCH["running"],
        "started_at": BENCH["started_at"],
        "error": BENCH["error"],
        "partial": bench.partial() if bench else None,
        "result": BENCH["result"],
    }


@api.get("/benchmark/verdict")
async def benchmark_verdict(coinswitch_fee_pct: Optional[float] = None, coindcx_fee_pct: Optional[float] = None):
    if not BENCH["result"]:
        raise HTTPException(status_code=404, detail="no benchmark result yet — run one first")
    fees: Dict[str, float] = {}
    if coinswitch_fee_pct is not None: fees["coinswitch"] = coinswitch_fee_pct
    else: fees["coinswitch"] = ENGINE.cfg.entry_taker_fee_pct
    if coindcx_fee_pct is not None: fees["coindcx"] = coindcx_fee_pct
    return evaluate_verdict(
        BENCH["result"], slippage_pct=ENGINE.cfg.slippage_buffer_pct, extra_pct=ENGINE.cfg.hurdle_extra_pct,
        fees_pct=fees, slot_inr=ENGINE.allocated_per_slot_inr(), fx=ENGINE.fx.rate,
    )


@api.get("/equity-curve")
async def equity_curve():
    return {"items": await ENGINE.store.get_equity_curve()}


@api.get("/logs")
async def logs(limit: int = 100):
    return {"items": await ENGINE.store.recent_logs(limit=limit)}


@api.get("/telemetry")
async def telemetry():
    snap = ENGINE.snapshot()
    return {
        "ws_connected": ENGINE.running,
        "fx_usdt_inr": snap["fx_usdt_inr"],
        "avg_exec_latency_ms": snap["avg_exec_latency_ms"],
        "mode": snap["mode"],
        "simulate": ENGINE.cfg.simulate,
        "coinswitch_client_ready": ENGINE.coinswitch is not None,
        "feed": ENGINE.feed_health(),
        "telegram": ENGINE.telegram.status(),
    }


@api.get("/telegram/status")
async def telegram_status():
    return ENGINE.telegram.status()


@api.post("/telegram/test")
async def telegram_test():
    """Sends a real test alert to the configured chat; 400 if not configured, 502 on API error."""
    tg = ENGINE.telegram
    if not tg.enabled:
        raise HTTPException(status_code=400, detail="TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not configured in backend/.env")
    try:
        me = await tg.get_me()
        snap = ENGINE.snapshot()
        await tg.send(
            "🔔 <b>QuantLag HFT test alert</b>\n"
            f"Mode: {snap['mode']} · Equity: ₹{snap['equity_inr']:.2f}\n"
            f"Trades: {snap['trades']} · Realized PnL: ₹{snap['realized_pnl_inr']:+.2f}\n"
            f"Sent at {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}",
            raise_on_error=True,
        )
    except Exception as e:
        raise HTTPException(status_code=424, detail=f"Telegram error: {e}")
    await ENGINE.store.log("INFO", f"Telegram test alert sent via @{me.get('username', '?')}")
    return {"ok": True, "bot": me.get("username"), "status": tg.status()}


@api.post("/telegram/summary")
async def telegram_summary_now():
    """Pushes the hourly summary immediately."""
    if not ENGINE.telegram.enabled:
        raise HTTPException(status_code=400, detail="Telegram not configured")
    try:
        await ENGINE._send_hourly_summary(raise_on_error=True)
    except Exception as e:
        raise HTTPException(status_code=424, detail=f"Telegram error: {e}")
    return {"ok": True, "status": ENGINE.telegram.status()}


@api.post("/coinswitch/check")
async def coinswitch_check():
    """Live-mode preflight: verifies CoinSwitch keys work by calling wallet_balance.
    Returns 400 with detail if keys are missing/invalid.
    """
    if not ENGINE.coinswitch:
        raise HTTPException(status_code=400, detail="CoinSwitch keys not configured in .env")
    try:
        wb = await ENGINE.coinswitch.wallet_balance()
        return {"ok": True, "wallet_balance": wb}
    except Exception as e:
        raise HTTPException(status_code=424, detail=f"CoinSwitch API error: {e}")


@api.get("/hourly-summary")
async def hourly_summary():
    snap = ENGINE.snapshot()
    return {
        "trades": snap["trades"],
        "wins": snap["wins"],
        "losses": snap["losses"],
        "win_rate": snap["win_rate"],
        "realized_pnl_inr": snap["realized_pnl_inr"],
        "equity_inr": snap["equity_inr"],
        "avg_hold_sec": snap["avg_hold_sec"],
        "avg_exec_latency_ms": snap["avg_exec_latency_ms"],
        "mode": snap["mode"],
    }


app.include_router(api)
app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve the frontend's production build (yarn build) from the same process/port
# when present, so deployment doesn't need a separate Node dev server running.
# Mounted last and at "/" so it never shadows the /api/* routes above.
_FRONTEND_BUILD = ROOT_DIR.parent / "frontend" / "build"
if _FRONTEND_BUILD.exists():
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=str(_FRONTEND_BUILD), html=True), name="frontend")
