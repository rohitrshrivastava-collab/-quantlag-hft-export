"""SQLite-backed store for the standalone CLI bot.

Mirrors the MemoryStore interface used by bot_engine.BotEngine so the same
engine can run either against volatile memory (dashboard) or a durable
on-disk SQLite DB (production / EC2).
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite

from backend.bot_engine import Position


class SQLiteStore:
    def __init__(self, path: str = "quantlag.db") -> None:
        self.path = path
        self._db: Optional[aiosqlite.Connection] = None

    async def init(self) -> None:
        self._db = await aiosqlite.connect(self.path)
        await self._db.executescript("""
        CREATE TABLE IF NOT EXISTS positions (
            id TEXT PRIMARY KEY, coin TEXT, direction TEXT, size_inr REAL,
            contracts REAL, entry_price_inr REAL, binance_ref_usdt REAL,
            target_price_inr REAL, stop_price_inr REAL, opened_at REAL,
            timeout_at REAL, mode TEXT, status TEXT,
            exit_price_inr REAL, closed_at REAL, gross_pnl_inr REAL,
            fees_inr REAL, net_pnl_inr REAL, close_reason TEXT,
            exec_latency_ms REAL
        );
        CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
        CREATE INDEX IF NOT EXISTS idx_positions_coin ON positions(coin);
        CREATE TABLE IF NOT EXISTS equity_curve (ts REAL, equity REAL);
        CREATE TABLE IF NOT EXISTS event_log (ts REAL, level TEXT, msg TEXT);
        """)
        await self._db.commit()
        # in-memory mirror of open positions for fast reads
        self.open: Dict[str, Position] = {}
        async with self._db.execute("SELECT * FROM positions WHERE status='OPEN'") as cur:
            cols = [d[0] for d in cur.description]
            async for row in cur:
                p = Position(**dict(zip(cols, row)))
                self.open[p.coin] = p

    async def add_open(self, p: Position) -> None:
        self.open[p.coin] = p
        d = asdict(p)
        await self._db.execute(
            f"INSERT OR REPLACE INTO positions ({','.join(d.keys())}) VALUES ({','.join(['?']*len(d))})",
            list(d.values()))
        await self._db.commit()

    async def get_open(self, coin: str) -> Optional[Position]:
        return self.open.get(coin)

    async def all_open(self) -> List[Position]:
        return list(self.open.values())

    async def close(self, p: Position) -> None:
        self.open.pop(p.coin, None)
        d = asdict(p)
        await self._db.execute(
            f"INSERT OR REPLACE INTO positions ({','.join(d.keys())}) VALUES ({','.join(['?']*len(d))})",
            list(d.values()))
        await self._db.commit()

    async def all_closed(self, limit: int = 500) -> List[Position]:
        async with self._db.execute(
            "SELECT * FROM positions WHERE status='CLOSED' ORDER BY closed_at DESC LIMIT ?", (limit,)
        ) as cur:
            cols = [d[0] for d in cur.description]
            return [Position(**dict(zip(cols, r))) async for r in cur]

    async def push_equity(self, ts: float, equity: float) -> None:
        await self._db.execute("INSERT INTO equity_curve VALUES (?,?)", (ts, equity))
        await self._db.commit()

    async def get_equity_curve(self) -> List[Dict[str, float]]:
        async with self._db.execute("SELECT ts, equity FROM equity_curve ORDER BY ts DESC LIMIT 5000") as cur:
            return [{"ts": r[0], "equity": r[1]} async for r in cur]

    async def log(self, level: str, msg: str) -> None:
        await self._db.execute("INSERT INTO event_log VALUES (?,?,?)", (time.time(), level, msg))
        await self._db.commit()

    async def recent_logs(self, limit: int = 100) -> List[Dict[str, Any]]:
        async with self._db.execute("SELECT ts, level, msg FROM event_log ORDER BY ts DESC LIMIT ?", (limit,)) as cur:
            return [{"ts": r[0], "level": r[1], "msg": r[2]} async for r in cur]
