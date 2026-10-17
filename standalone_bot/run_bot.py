"""Standalone CLI runner for the QuantLag lead-lag arbitrage bot.

Usage (local VS Code / AWS EC2 Ubuntu):
    cd /path/to/repo
    python -m venv venv && source venv/bin/activate
    pip install -r standalone_bot/requirements.txt
    cp standalone_bot/.env.example standalone_bot/.env   # then fill keys
    python standalone_bot/run_bot.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

# Make sibling `backend` package importable
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.bot_engine import BotConfig, BotEngine  # noqa: E402
from standalone_bot.sqlite_store import SQLiteStore  # noqa: E402


async def latency_benchmark() -> None:
    """Ping Binance + optional CoinSwitch REST endpoints to gauge round-trip."""
    import aiohttp
    targets = [
        ("Binance Futures", "https://fapi.binance.com/fapi/v1/ping"),
        ("CoinGecko (FX)", "https://api.coingecko.com/api/v3/ping"),
    ]
    async with aiohttp.ClientSession() as s:
        for name, url in targets:
            t0 = time.perf_counter()
            try:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=5)) as r:
                    await r.text()
                    print(f"  {name:20} {(time.perf_counter()-t0)*1000:6.1f} ms  ({r.status})")
            except Exception as e:
                print(f"  {name:20} FAILED  ({e})")


async def main() -> None:
    load_dotenv(Path(__file__).parent / ".env")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    print("=" * 70)
    print(" QuantLag HFT — Lead-Lag Latency Arbitrage Bot")
    print(f" Host: {os.uname().sysname}/{os.uname().machine}  Python: {sys.version.split()[0]}")
    print("=" * 70)
    print(" API latency benchmark:")
    await latency_benchmark()
    print("=" * 70)

    cfg = BotConfig.from_env()
    print(f" Mode           : {'PAPER' if cfg.paper_trading else 'LIVE'}")
    print(f" Simulator      : {cfg.simulate}")
    print(f" Starting cap.  : ${cfg.starting_capital_usdt:,.2f}")
    print(f" Slots          : {cfg.num_slots} × ${cfg.starting_capital_usdt/cfg.num_slots:,.2f}")
    print(f" Hurdle gate    : {(cfg.entry_taker_fee_pct+cfg.exit_taker_fee_pct+cfg.slippage_buffer_pct+cfg.hurdle_extra_pct):.3f} %")
    print(f" Timeout        : {cfg.timeout_seconds}s   Stop-loss: {cfg.stop_loss_pct}%")
    print(f" Telegram       : {'ENABLED' if cfg.telegram_bot_token and cfg.telegram_chat_id else 'muted'}")
    print("=" * 70)

    store = SQLiteStore(os.environ.get("SQLITE_PATH", "quantlag.db"))
    await store.init()
    engine = BotEngine(cfg, store=store)  # type: ignore[arg-type]

    stop_evt = asyncio.Event()
    def _sig(*_): stop_evt.set()
    for s in (signal.SIGINT, signal.SIGTERM):
        try: signal.signal(s, _sig)
        except Exception: pass

    await engine.start()

    # periodic console snapshot
    async def _snap():
        while not stop_evt.is_set():
            await asyncio.sleep(15)
            s = engine.snapshot()
            print(f"[{time.strftime('%H:%M:%S')}] eq=${s['equity_usdt']:.2f}  "
                  f"pnl=${s['realized_pnl_usdt']:+.2f}  "
                  f"trades={s['trades']} (W{s['wins']}/L{s['losses']})")
    asyncio.create_task(_snap())

    await stop_evt.wait()
    print("\nShutting down…")
    await engine.stop()
    print("Bye.")


if __name__ == "__main__":
    asyncio.run(main())
