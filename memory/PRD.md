# QuantLag HFT — Lead-Lag Latency Arbitrage Bot (PRD)

## Original problem statement
Automated cross-exchange arbitrage bot exploiting price-discovery delay between
Binance USDT-M Futures (leader) and CoinSwitch PRO Crypto Futures INR (laggard).
Starting capital ₹4000 across 10 coin slots (₹400/slot). Runs locally (VS Code)
and on AWS EC2 Ubuntu (ap-south-1 Mumbai). WebSocket-first data pipeline,
FX-normalized net-spread hurdle gate, timeout guard, volatility stop-loss,
Telegram alerts + hourly summaries, SQLite persistence, paper/live toggle.

## User personas
- Solo quant trader running the bot from VS Code / EC2
- Reviewing performance via web dashboard in real time

## Architecture
- `backend/bot_engine.py` — async engine (DataIngestion, SignalEngine,
  PortfolioManager, OrderExecutor, TelegramReporter, MemoryStore).
- `backend/server.py` — FastAPI hosting the engine + `/api/*` endpoints.
- `frontend/src/App.js` — React dashboard (dark HFT ops console).
- `standalone_bot/` — CLI runner with `SQLiteStore` for VS Code / EC2 deploy.
- `README.md` — architecture, setup, going-live checklist.

## What's been implemented (2026-01)
- Async engine with 10-coin scan loop (~400 ms), FX-aware simulator,
  Binance WS adapter, USDT/INR FX poller (CoinGecko), hurdle gate,
  LONG/SHORT logic, timeout/stop-loss/target exits, partial-fill safe
  paper executor, Telegram HTML alerts + hourly summary loop.
- 13 REST endpoints (status/spreads/positions/config/telemetry/etc.).
- React dashboard: portfolio KPIs, 10-slot spread grid w/ sparklines,
  open positions table w/ Close, closed trades log, session summary,
  config panel (fees, hurdle, timeout, stop-loss), event log, mode
  toggle, start/stop, USDT-INR ticker.
- Standalone CLI bot with SQLite persistence & crash recovery.
- .env.example, requirements.txt, README with EC2 Mumbai setup guide.
- 100% backend test pass (iteration_1.json).

## Prioritized backlog
- P0: Wire real CoinSwitch PRO REST place-order client into `_open_position`
  (currently a WARN stub — deliberate safety).
- P0: Implement CoinSwitch PRO WS adapter (`LiveCoinSwitchFeed`).
- P1: Token-bucket rate limiter around REST calls (skeleton in engine).
- P1: Multi-source FX (Binance P2P median) for robustness beyond CoinGecko.
- P2: Equity curve chart on dashboard (data available, chart not drawn).
- P2: CSV export of closed trades, per-coin filter.

## Deployment
- EC2 t3.small (Mumbai) recommended.  `tmux` + `python standalone_bot/run_bot.py`.
- IP-restrict API keys; scope to Futures trading only, no withdrawals.
