# QuantLag HFT — Lead-Lag Latency Arbitrage Bot (PRD)

## Original problem statement
Automated cross-exchange arbitrage bot exploiting price-discovery delay between
Binance USDT-M Futures (leader) and CoinSwitch PRO Crypto Futures INR (laggard).
₹4000 across 10 coin slots (₹400/slot). Runs locally (VS Code) and on AWS EC2
(ap-south-1 Mumbai). WebSocket-first, FX-normalized net-spread hurdle gate,
timeout guard, volatility stop-loss, Telegram alerts + hourly summaries,
SQLite persistence, paper/live toggle.

## Architecture
- `backend/bot_engine.py` — async engine (DataIngestion, SignalEngine,
  PortfolioManager, OrderExecutor, TelegramReporter, MemoryStore).
- `backend/coinswitch_client.py` — async Ed25519-signed CoinSwitch PRO
  Futures REST client (wallet_balance, positions, ticker, place_order,
  get_order_status, cancel_order, cancel_all_open_orders, order_book,
  instrument_info).
- `backend/server.py` — FastAPI, `/api/*` endpoints (14 total including
  `/api/coinswitch/check`).
- `frontend/src/App.js` — React dashboard.
- `standalone_bot/` — CLI runner with `SQLiteStore` for VS Code / EC2 deploy.

## What's been implemented (2026-01)
### Iteration 1
- Async engine, 10-coin scan loop, FX-aware simulator, Binance WS adapter,
  USDT/INR FX poller, hurdle gate, LONG/SHORT logic, timeout/stop-loss/target
  exits, Telegram HTML alerts + hourly summary loop.
- 13 REST endpoints. React dashboard (portfolio KPIs, spread grid w/
  sparklines, open positions, closed trades, session summary, config panel,
  event log, mode toggle, start/stop, USDT-INR ticker).
- Standalone CLI bot with SQLite persistence & crash recovery.

### Iteration 2 (2026-01, "Live Order Wiring")
- `CoinSwitchFuturesClient` (async, Ed25519 signing per docs).
- `_open_position` LIVE branch: converts INR→USDT quantity, places MARKET
  order on EXCHANGE_2, waits for fill, records actual `exec_quantity`,
  `avg_execution_price × fx`, and real `execution_fee`.
- `_close_position` LIVE branch: opposite-side MARKET with `reduce_only=true`
  for the actual filled contracts; handles partial closes by shrinking
  `pos.contracts` to executed quantity.
- `LiveCoinSwitchTickerPoller` — REST ticker poller (~5s per coin) as
  fallback laggard feed until a WS book stream is added.
- `POST /api/coinswitch/check` preflight (calls wallet_balance).
- `/api/telemetry.coinswitch_client_ready` flag.
- `/api/bot/mode` returns `warning` when flipping to LIVE without keys.
- One-shot ERROR log on LIVE-mode without keys (no per-cycle spam).
- Dashboard "Run check" button in the config panel.
- Full backend regression: 16/16 tests pass (`iteration_2.json`).

## Prioritized backlog
- P1: Real CoinSwitch WebSocket book stream (replace `LiveCoinSwitchTickerPoller`).
- P1: Multi-source FX (Binance P2P median) for robustness beyond CoinGecko.
- P1: Token-bucket rate limiter around REST fan-out.
- P2: Equity curve chart on dashboard (data available at `/api/equity-curve`).
- P2: CSV export of closed trades, per-coin filter.
- P2: Per-coin kill switch (pause a slot without stopping the whole bot).

## Deployment
- EC2 t3.small (Mumbai) recommended. Use `tmux` + `python standalone_bot/run_bot.py`.
- IP-restrict API keys; scope to Futures trading only, no withdrawals.
