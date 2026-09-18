"""FastAPI server for QuantLag HFT dashboard.
Hosts the async bot engine (paper mode with simulator by default) and
exposes REST endpoints under /api/* consumed by the React dashboard.
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from dataclasses import asdict
from dotenv import load_dotenv
from fastapi import APIRouter, FastAPI, HTTPException
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, Field
from starlette.middleware.cors import CORSMiddleware

from bot_engine import BotConfig, BotEngine

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Auto-start bot in paper mode on server boot (dashboard shows live data)
    await ENGINE.start()
    try:
        yield
    finally:
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
    await ENGINE.store.log("INFO", f"Mode switched to {'PAPER' if body.paper_trading else 'LIVE'}")
    return {"ok": True, "mode": "PAPER" if body.paper_trading else "LIVE"}


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
    }


@api.put("/config")
async def update_config(body: ConfigUpdate):
    c = ENGINE.cfg
    data = body.model_dump(exclude_none=True)
    for k, v in data.items():
        setattr(c, k, v)
    await ENGINE.store.log("INFO", f"Config updated: {data}")
    return await get_config()


@api.get("/spreads")
async def spreads():
    return {"items": ENGINE.spreads_snapshot(), "hurdle_pct": ENGINE.hurdle_pct()}


@api.get("/positions/open")
async def positions_open():
    return {"items": [asdict(p) for p in await ENGINE.store.all_open()]}


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
async def positions_closed(limit: int = 200):
    items = [asdict(p) for p in await ENGINE.store.all_closed(limit=limit)]
    return {"items": items}


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
    }


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
