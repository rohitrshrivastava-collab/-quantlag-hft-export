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
- `backend/bot_engine.py::LiveCoinSwitchBookFeed` — public book WS (primary
  laggard feed); `LiveCoinSwitchTickerPoller` is now only the REST fallback.
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

### Iteration 3 (2026-06, "CoinSwitch WebSocket Feed")
- `LiveCoinSwitchBookFeed` — public CoinSwitch PRO Futures order-book stream
  (Socket.IO v4 spoken directly over aiohttp WS: `0`→`40/exchange_2,`,
  `2`→`3` ping/pong, `42/exchange_2,[FETCH_ORDER_BOOK_CS_PRO,{…}]` snapshots).
  Symbol is read from `symbol` OR `s` (live payload uses `s`). No API keys needed.
  Measured from preview pod: ~120 ms wire latency, ~44 snapshots/s for 10 coins.
- When `SIMULATE_MARKET=false`, real Binance WS + real CoinSwitch WS power BOTH
  PAPER and LIVE modes (paper-trade on real quotes).
- Staleness gate: `LAGGARD_STALE_AFTER_S` (default 10). `_feed_watchdog_loop`
  starts the REST ticker poller when WS is stale (keys required) and stops it on
  recovery; without keys, laggard is marked STALE and new entries pause.
- `/api/telemetry.feed` → source (SIMULATOR|COINSWITCH_WS|REST_POLLER|STALE),
  laggard {connected, subscribed, last_msg_age_s, wire_latency_ms, msgs_per_s,
  msg_count, reconnects, last_error, coins[{coin, age_ms, stale}]},
  leader {connected, last_msg_age_s, reconnects, coins_with_quotes},
  rest_fallback {available, active}.
- Dashboard "Laggard Feed" health strip (`FeedHealthStrip`).
- Preview backend `.env` now runs `SIMULATE_MARKET=false` (real public data).
- Regression: 9/9 backend + frontend pass (`iteration_3.json`).

### Iteration 4 (2026-06, "Telegram + Equity Curve + Spread Heatmap")
- `TelegramReporter` now real: getMe on start, sendMessage (HTML), lock +
  50 ms gap, 429 retry_after handling, sent/fail counters, last_error.
  User's bot/chat configured in backend/.env (`@Rohittelegram_bot`).
- Endpoints: `GET /api/telegram/status`, `POST /api/telegram/test`,
  `POST /api/telegram/summary`; `/api/telemetry.telegram`.
- Dashboard `TelegramPanel` (status, sent/failed, "Send test alert",
  "Summary now").
- `EquityCurveChart` (recharts AreaChart, baseline reference line, 5 s samples
  from `/api/equity-curve`).
- Spread heatmap: `_record_heat` keeps 60 one-second buckets per coin of the
  signed max |spread|/hurdle ratio; `/api/spreads` adds `heat[60]`,
  `near_hurdle_hits_60s` (|ratio| ≥ 0.8) and `peak_ratio_60s`. Dashboard
  `HeatBar` + flame hit-count badge + "peak 60s NN% of hurdle".
- Testing: backend 8/8 after fix (bot_username resolved at start), frontend
  all pass (`iteration_4.json`).

### Iteration 5 (2026-06, "Ops kit + Exchange-pair decision kit")
- `backend/rate_limiter.py` TokenBucket/RateLimiterGroup; wired into
  `CoinSwitchFuturesClient._request` (orders 20/min, default 60/min via
  `CS_RATE_LIMIT_*` env) + 429 retry with jitter; stats in
  `telemetry.feed.rest_fallback.rate_limiter`.
- CSV export `GET /api/positions/closed/export.csv?coin=` + `?coin=` filter on
  `/api/positions/closed`; dashboard select + Export button.
- Per-coin kill switch `POST /api/coins/{coin}/pause|resume`; `PAUSED` signal
  state; pause/play button on each spread card.
- Hurdle tuner `GET /api/hurdle/simulate?hurdle_pct&window_s` (episodes of
  |spread| ≥ hurdle over 10-min heat retention); live panel under config.
- `benchmark/latency_bench.py` (asyncio + websockets): TCP RTT, WS
  tick-to-receipt p50/p99, msg/s, max gap, reconnects, spread bps, depth
  ±10 bps, divergence-vs-Binance quantiles for Binance/CoinSwitch/CoinDCX.
  Server-side runner `POST /api/benchmark/run`, `GET /api/benchmark/status`,
  result persisted to `benchmark/last_result.json`.
- `backend/verdict.py` decision rules (gates + 100-pt score) →
  `GET /api/benchmark/verdict?coinswitch_fee_pct&coindcx_fee_pct`; dashboard
  Benchmark panel + Verdict card with live fee inputs.
- `docs/EXCHANGE_PAIR_DECISION.md`: benchmark guide, CoinSwitch vs CoinDCX
  mechanics table, verdict framework + recommendation.
- Fixed: config draft reset every poll (stale closure → useRef), mobile
  overflow from recharts container (`min-w-0`).
- Testing: `iteration_5.json` backend 17/17, frontend pass after fixes.

### Iteration 6 (2026-06, "Account controls · Basis filter · CoinDCX adapter · Analytics · Digest")
- `POST /api/account {equity_inr, num_slots, leverage}`: equity reset (zeroes
  PnL/stats, keeps closed history, 409 while positions open); slots 1–50;
  leverage 1–100 → notional = margin × lev (contracts, PnL, fees scale); LIVE
  pushes `update_leverage` to CoinSwitch (`/futures/leverage`) or CoinDCX for
  idle symbols. Dashboard "Account Controls" panel + header badge.
- Basis filter (`BASIS_FILTER`, `BASIS_WINDOW_S`=300): rolling median of raw
  spread subtracted before the hurdle gate; target price basis-adjusted;
  `/api/spreads` adds `raw_spread_pct`, `basis_pct`; toggle in config panel.
- CoinDCX adapter: `LiveCoinDCXBookFeed` (Socket.IO v2 deltas+snapshots) and
  `coindcx_client.py` (HMAC-SHA256; market_order/cancel/list_orders/positions/
  update_leverage/wallet). `LAGGARD_EXCHANGE=coindcx` switches feed + executor
  (`_live_market` abstraction). Verified feed live from pod (124 ms wire).
  COINDCX_API_KEY/SECRET empty until user supplies.
- `GET /api/analytics/coins` + Slot Analytics table with Bench/Resume.
- Daily digest `send_daily_digest` at `DIGEST_TIME_IST` (23:30) + `POST
  /api/telegram/digest`; includes per-coin table, feed health, last benchmark.
- Testing: `iteration_6.json` backend 10/10, frontend pass, 390px no overflow.

## Prioritized backlog
- P1: Multi-source FX (Binance P2P median) for robustness beyond CoinGecko.
- P1: Live-test CoinDCX executor once user supplies COINDCX_API_KEY/SECRET
  (order shape verified against docs, not against a real account yet).
- P2: Persist paused coins / account settings across restarts (SQLite in CLI).
- P2: Dynamic asset-universe screening (24h volume / liquidity).

## Deployment
- EC2 t3.small (Mumbai) recommended. Use `tmux` + `python standalone_bot/run_bot.py`.
- IP-restrict API keys; scope to Futures trading only, no withdrawals.
