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

### Iteration 7 (2026-06, "CoinDCX preflight · Persistence · Robust FX · Session Replay")
- `POST /api/coindcx/check` wallet+positions preflight (user keys stored in
  backend/.env but CoinDCX returns 401 Invalid credentials — even spot
  `users/info`; likely IP whitelist / permission / secret copy issue).
  Upstream errors now use HTTP 424 (preview ingress swallows 502).
- `settings_store.py`: settings.json in `DATA_DIR` (default /app/data) with
  config (slots, leverage, capital, mode, basis, fees, timeout, stop), state
  (equity, PnL, counters) and paused coins; restored at engine init; saved on
  every config/account/pause/mode change and each trade close.
- `FXRateService` = median of CoinGecko, CoinDCX USDTINR mid, Binance P2P
  top-10 median (observed 95.96 / 99.59 / 103 → blend 99.56); sources +
  freshness in `telemetry.feed.fx`; chips in config panel.
- `replay.py`: `TickRecorder` → `data/ticks/YYYY-MM-DD.jsonl` (IST day,
  1 s flush, 7-day prune, `RECORD_TICKS`, `TICK_RETENTION_DAYS`);
  `replay_session()` offline pass of the exact signal rules (basis, hurdle,
  slots, leverage, target/stop/timeout). Endpoints `/api/replay/sessions`,
  `/api/replay/run`, `/api/replay/last`; dashboard Session Replay panel with
  params, metrics, equity curve, per-coin table.
- Testing: `iteration_7.json` backend + frontend all pass (incl. restart
  persistence round-trip).

### Iteration 8 (2026-06, "Replay Sweep · Coin Screener · Trade persistence · CoinDCX recheck")
- `POST /api/replay/sweep` grid (hurdle × basis window, ticks loaded once) →
  heat table + best cell in dashboard SweepPanel. First real-data result:
  hurdle 0.10–0.15% / 300 s window profitable (80–89% win) vs 0.05% losing.
- `screener.py`: CoinDCX futures 24h quote volume (`v`) ∩ Binance perps
  (exchangeInfo, fallback static list when geo-blocked) × lag activity (p95
  |divergence| bps from today's ticks; unknown coins get the observed median
  lag score). `GET /api/screener`, `POST /api/universe` → `BotEngine.set_coins`
  (rebuilds per-coin state + feeds, refuses with open positions, persisted).
- `MemoryStore` persistence → `data/trades.json` (open/closed/equity curve),
  restored on startup. CoinDCX keys still 401 (user to fix on exchange side).
- Testing: `iteration_8.json` backend 16/16, frontend pass.

### Iteration 9 (2026-06, "CoinDCX test-order flow")
- `POST /api/coindcx/test-order {coin, notional_usdt, confirm}`: wallet →
  instrument sizing (public instrument endpoint: min_notional, quantity_increment)
  → leverage 1 → market BUY → market SELL close → verify flat; returns step
  list; 400 without confirm; 424 with {message, steps} on API error. Telegram +
  TRADE log on success. Dashboard sub-panel gated on a passing CoinDCX check
  AND a confirm checkbox. Keys still rejected by CoinDCX (401) — egress IP of
  preview pod: 35.225.230.28 (will differ on EC2).
- Testing: `iteration_9.json` backend 7/7, frontend pass (safety gate verified).

### Iteration 10 (2026-06, "CoinDCX LIVE verified")
- New user keys accepted. Futures margin wallet is INR → client gained
  `margin_currency` (env `COINDCX_MARGIN_CURRENCY=INR`, auto-detect from
  wallets) used in orders/positions/leverage/list. Sizing rounds UP to
  min_notional×1.05 (exchange enforces ₹612 min at its own FX) and checks free
  margin / leverage. `get_order(id)` + 250 ms polling replaces status scans.
- REAL round-trip executed once: 75 DOGE buy 0.08476 → sell 0.08475, fees
  0.0075 USDT, cost ≈ 0.008 USDT, position flat. Leverage-update call works.
- Testing: `iteration_10.json` backend 8/8, frontend pass (no further orders).

### Iteration 11 (2026-06, "CoinDCX order-update WS · Pair B LIVE trial PASSED")
- `CoinDCXUserStream` (coindcx_client.py): authenticated Socket.IO EIO=3 join of
  channel `coindcx` (HMAC of `{"channel":"coindcx"}`), consumes `df-order-update`
  / `df-position-update`; `wait_for_fill` resolves via asyncio.Event per order id
  (REST `get_order` only every 1 s as safety net). Started/stopped with engine
  when `LAGGARD_EXCHANGE=coindcx`. Telemetry `feed.user_ws` {connected, joined,
  order_updates, fills_via_ws/rest, last_fill_latency_ms, last_fill_source};
  dashboard pill `feed-user-ws-badge` ("DCX FILLS · WS LIVE").
- `size_qty` snaps entries to `quantity_increment` (floor, lifted to
  min_notional×1.05; closes rounded to nearest step). `SlotTooSmall` (min order
  >1.5× slot) → WARN + 10-min per-coin cooldown; order reject → 30 s cooldown;
  close reject → position KEPT OPEN and retried after 10 s (no phantom closes).
- backend/.env now `LAGGARD_EXCHANGE="coindcx"`.
- REAL LIVE TRIAL (₹700, 1 slot, 1x; BTC/ETH/LINK/AVAX paused because their
  CoinDCX minimums are 25–80 USDT): SHORT ADA 32 @0.2198 → TIMEOUT @0.2202
  net ₹-2.02; LONG SOL 0.06 @108.14 → TARGET_HIT @108.15 net ₹-0.75. All 4
  fills via WS (614–939 ms after create ack; create REST ≈1.2 s). Wallet
  1820.62 → 1816.72 INR, exchange flat. Bot returned to PAPER afterwards.
- Testing: `iteration_11.json` backend 8/8, frontend pass (1920 + 390 px).

### Iteration 12 (2026-06, "Phantom-liquidity guard · CoinSwitch vs CoinDCX shadow head-to-head")
- `liquidity.py`: walk-the-book VWAP, `fillable_within(max_slip)`, ±bps depth, frozen-book
  detection. Feeds keep top-20 depth + `book_change_ts`; `frozen_s(coin)`.
- Engine: `liquidity(coin, dir, notional)` before every entry → PHANTOM/NO_DEPTH skip,
  THIN skip below `MIN_ORDER_INR`, else **shrink** to fillable; dynamic hurdle
  `fees + max(buffer, 2×slip) + extra`; paper entry/exit at VWAP. New signal states
  PHANTOM/THIN; `Position.liq_state/liq_slippage_pct/requested_notional_inr`;
  `telemetry.feed.liquidity_guard`; `/api/spreads[].liquidity`; `GET /api/liquidity/{coin}`.
  Config (persisted, PUT /api/config, dashboard inputs): `max_slippage_pct` 0.05,
  `phantom_frozen_s` 3, `min_order_inr` 100 (+ `depth_bps` 10 env).
- `shadow.py` ShadowRunner: second laggard feed + identical rules paper-simulated on BOTH
  exchanges (own fee/latency: CS 0.05%/1.0 s, DCX 0.059%/1.9 s), 10 Hz. Metrics: response
  lag to ≥2 bp Binance moves, lag window, catch-up time, book spread, slippage ladder
  ₹10k/1L/10L, depth, skips, PnL/h, verdict. `GET /api/compare`, `POST /api/compare/reset`,
  dashboard ComparePanel + LiquidityRow per coin.
- Findings (18 min sample, docs §5): both books mirror Binance (spread 0.009%, slippage ≈0
  to ₹10L); CoinSwitch lags 2× more on average (0.0069% vs 0.0034% divergence) with a
  12% >10 s freeze tail, but median catch-up 0.34 s vs 0.46 s — both SHORTER than 1–2 s
  execution → both net ≈ −39 ₹/h at 0.15% hurdle. Wider window does NOT outweigh; the
  lever is execution latency / maker orders / higher hurdle on fresh books.
- Note: user switched dashboard to LIVE (₹601, 4 slots, CoinDCX); all coins skipped by
  SlotTooSmall (₹150 slot < exchange min ≈ ₹600) so no real orders fire.
- Testing: `iteration_12.json` backend 9/9, frontend pass (1920 + 390 px).

### Iteration 13 (2026-06, "Pair switch button · Wallet balance · LIVE ₹1800@10x · FX-consistent marking")
- `POST /api/laggard {exchange}` → `BotEngine.set_laggard_exchange` (409 with open
  positions; restarts feeds + user stream; pushes leverage in LIVE; persisted via
  `laggard_exchange` in settings.json). Header `pair-toggle` button + banner message.
- `GET /api/wallet` (10 s cache, `?force=true`) → active exchange futures wallet; header
  `wallet-balance-chip` + Account panel `account-wallet-row`. CoinSwitch shows "no keys".
- User went LIVE on CoinDCX: equity ₹1800, 10 slots, 10x (leverage pushed to all 10 coins,
  no errors). Real 10x trades executing (~₹1.5–2.5k notional, fills via WS 0.6–0.9 s,
  create+fill 1.1–2.0 s). Net still fee-negative at 0.15% hurdle (see §5 findings).
- Bug fixed: FX blend jumped ±1.4% when a source went stale → fake STOP_LOSS (NEAR −₹11.19
  on a flat USDT trade). Now `FXRateService` prefers CoinDCX USDT/INR; `Position.entry_fx`
  pins the booking rate; `_manage_position`/`_close_position` mark in the entry-FX frame.
- Break-even target floor: target ≥ entry × (1 + fees + extra) for LONG (mirror for SHORT)
  so TARGET_HIT can no longer close below cost (also in shadow sim).
- Shadow sim books at a pinned fx0.
- Testing: `iteration_13.json` backend 7/7, frontend pass; pair round-trip verified live.

### Iteration 14 (2026-06, "0.30% hurdle · fresh-book filter · warm CoinDCX session · CoinSwitch keys")
- Hurdle raised to 0.30% (`hurdle_extra_pct` 0.17). New `max_entry_frozen_s` (1.0 s, env
  `MAX_ENTRY_FROZEN_S`, PUT /api/config, dashboard "Fresh book ≤ (s)"): entries skipped
  with state `STALE_BOOK` when the laggard top-5 hasn't changed within it (`stale_skips`).
- `CoinDCXFuturesClient` now keeps ONE warm keep-alive `ClientSession` (+ `keep_warm()` GET
  every 15 s from `_keep_warm_loop`): order create dropped from ≈1.2 s to **344–451 ms**;
  `last_create_ms/last_warm_ms` in `telemetry.feed.user_ws` and the DCX FILLS chip. Fill
  confirmation via WS (~620–650 ms) is now the bottleneck → total ≈1.0–1.3 s (was ~2 s).
- CoinSwitch PRO keys installed (backend/.env) and verified via `/api/coinswitch/check`;
  futures wallet is USDT-margined and holds 0.0055 USDT (empty) — LIVE on Pair A would be
  rejected for margin until funded. `wallet()` parses `base_asset_balances`.
- First 0.30% live trade: NEAR LONG 4 @3.749 → STOP_LOSS @3.735 (Binance spike reverted),
  net −₹7.33 at 10x; slot shrunk ₹1764→₹1492 by the liquidity guard.
- Testing: self-tested (curl + screenshot); CoinSwitch wallet via engine script.

### Iteration 15 (2026-06, "Spike-persistence filter · latency split · WS idle fix · leverage-cap retry")
- Scan loop 0.1 s (`scan_interval_s`); recorder/sparkline throttled to 0.4 s. Spike filter:
  a hurdle crossing must persist `spike_persist_ms` (300, PUT /api/config, dashboard input)
  → states `ARMING_LONG/SHORT`; `status.spike_filter {crossings, persisted, reverted,
  arming}` (first 10 min live: 8 crossings, 6 reverted, 2 fired). Shadow sim mirrors it.
- `Position.create_ms / fill_confirm_ms`; `status.avg_create_ms / avg_fill_confirm_ms /
  live_fill_samples / last_*`; Exec Latency card sub-line `latency-split`.
- CoinDCX user WS: idle `receive` timeout is now a no-op (was reconnecting every 30 s).
- CoinDCX "Max allowed leverage for current position size = 7.0x" (ONE) → parse, push that
  leverage for the pair, retry once; effective leverage used for margin bookkeeping.
- Testing: `iteration_14.json` backend 6/6, frontend pass (read-only; bot LIVE).

### Iteration 16 (2026-06, "CoinSwitch trial prep — waiting for futures-wallet USDT")
- User funded CoinSwitch with ₹500 → it sits in the SPOT/INR wallet (`/user/portfolio`:
  INR 500.11); futures wallet (USDT-margined only) still 0.0055 USDT. No public API to move
  spot→futures (only the HFT/DMA product has `/dma/api/v1/funds/transfer`), so the user must
  buy USDT and "Transfer to Futures" in the app.
- `coinswitch_trial_watcher` (server.py, every 20 s): when laggard=coinswitch, LIVE and
  futures USDT ≥ 4 with no open positions → equity = usdt×fx×0.98, 1 slot, 10x, Telegram +
  log "CoinSwitch trial armed". `GET /api/coinswitch/trial`; dashboard banner
  `coinswitch-trial-banner` with the app steps.
- CoinSwitch client: warm keep-alive session (`keep_warm` via ticker, ≈330–350 ms RTT),
  `last_create_ms`/`last_fill_latency_ms`, `spec()`/`size_qty()` from instrument_info
  (DOGE min 87, SOL 0.1, XRP 5.4; taker 0.065%/side, max lev 57–86). `_live_market`
  CoinSwitch branch sizes + SlotTooSmall guard + returns create/fill timings.
- `feed.exec` telemetry (exchange-agnostic) + `CS ORDERS` chip; shadow CoinSwitch fee → 0.065%.
- User currently LIVE on pair CoinSwitch (4 slots, 10x, equity ₹1675) — orders will be
  rejected for margin until funded; harmless.

### Iteration 17 (2026-06, "CoinSwitch live loop measured")
- User moved 5 USDT into the CoinSwitch futures wallet; watcher armed (equity ₹488, 1 slot, 10x).
- Two real DOGE round-trips (87 DOGE ≈ 7.6 USDT) on CoinSwitch measured create ≈420 ms warm,
  exchange execution ≈400 ms, fill visible via REST poll ≈0.9–2.0 s (20/min order budget →
  0.5 s polling); fee ≈0.068 %/side from wallet delta (`execution_fee` constant 0.57 = bogus
  unit). Wallet 5.0055 → 4.9847 USDT. Docs §5.5.
- Bugs fixed: `get_order_status` payload is `data.order` (was `data` → fills never seen);
  create returns `RAISED` so polling is mandatory; unfilled RAISED orders are cancelled to
  avoid orphans; fee derived from `taker_fee_rate`; trial watcher idempotent across reloads.
- User then switched the pair back to CoinDCX (₹1740, 2 slots, 10x) from the dashboard.

### Iteration 18 (2026-06, "Spec compliance audit: top-of-book signals · book-share cap · dynamic stop")
- Signals now evaluated against the laggard TOP OF BOOK: `long_edge = (BinanceRef − best ask)/ask`,
  `short_edge = (best bid − BinanceRef)/bid` (BinanceRef = Binance mid, basis-adjusted); LONG fires
  when long_edge > hurdle, SHORT when short_edge > hurdle; `adj_pct` = 0 while Binance is inside
  the laggard spread. `/api/spreads[].long_edge_pct/short_edge_pct`; card line "vs ask / vs bid".
- `max_book_share_pct` (25%): notional ≤ that share of liquidity within depth_bps, combined with
  the slippage cap (min of both), else shrink/skip.
- Dynamic stop: `stop_mode` dynamic|fixed; σ of laggard mid (~0.4 s samples over vol_window_s
  300 s) scaled to the timeout horizon; stop = clamp(vol_k·σ, stop_min_pct 0.15, stop_max_pct 1.0);
  per-coin `recommended_stop_pct/stop_basis/vol_pct`; `Position.stop_pct/stop_basis`; logged at OPEN.
- `breakeven_floor` now a toggle (default on); `spike_persist_ms = 0` = execute immediately.
- Shadow sim mirrors all of the above. Dashboard: stop-mode select, break-even checkbox, 4 inputs.
- Note: user set timeout_seconds = 10 (spec suggests 30–60).
- Testing: `iteration_15.json` backend 6/6, frontend pass.

### Iteration 19 (2026-06, "Dynamic coin universe · Max-price filter · Exact-formula hurdle")
- `screener.build_universe()`: live shortlist from CoinDCX public futures RT feed (last, 24h
  quote volume, 24h range→volatility) ∩ Binance perps (exchangeInfo, fallback list). Filters:
  both-exchanges, volume ≥ `universe_min_volume_usdt` (5M), volatility ≥ `universe_min_vol_pct`
  (1%), price ≤ `max_coin_price_usd` (0 = no cap). Ranked by volatility; hysteresis keeps
  currently-tracked coins that still qualify, fills freed slots with highest-vol newcomers.
- `universe_watcher` (server.py, every `universe_refresh_s`=60s): rebuilds & applies via
  `set_coins` when `auto_universe` on, bot running, and NO open positions (swap restarts feeds,
  never mid-trade). Standalone task (survives set_coins stop/start). `GET /api/universe/auto`.
- New config (persisted, PUT /api/config, GET /api/config, env): `auto_universe` (True),
  `max_coin_price_usd` (0), `universe_min_volume_usdt`, `universe_min_vol_pct`, `universe_refresh_s`.
- Dashboard: prominent **Max Coin Price (USD)** control (`max-coin-price-input` + `max-coin-price-save-btn`)
  in the top **Account Controls** bar (was buried in the config panel), with the `auto-universe-checkbox`
  and a live "Currently scanning: …" list (`auto-universe-coins`). "Extra Hurdle %" relabelled "Break-even margin %".
- **Max-price enforced two ways**: (a) INSTANT scan-time gate in `_scan_coin` — any monitored coin whose
  live Binance price > cap is skipped with state `PRICE_CAPPED` (amber badge), effective immediately with
  NO feed restart and regardless of open positions; (b) `build_universe` also excludes capped coins when the
  auto-universe swaps. 0/blank = no cap (all coins eligible). `universe_watcher` now heartbeats (`ticks`) and
  reports why it deferred a swap (positions open) — the price cap does NOT wait for a swap window.
- ENTRY GATE TIGHTENED: `hurdle_pct()` = entry_fee + exit_fee + slippage_buffer ONLY
  (dropped `hurdle_extra_pct`, so 0.13% at current settings, was 0.30%). `dynamic_hurdle_pct`
  likewise. `hurdle_extra_pct` (0.17) now only feeds the break-even floor (target still ≥
  entry + fees + 0.17%). This materially increases live trade frequency at a lower edge — by design.
- Testing: `iteration_16.json` frontend 5/5 pass (backend verified via curl). Bot was LIVE
  (CoinDCX, 4 slots, 5x) throughout — testing was non-destructive.

- `settings.json`, `trades.json`, `ticks/YYYY-MM-DD.jsonl`, `../benchmark/last_result.json`

### Iteration 20 (2026-06, "Open-position live P/L column + loss investigation")
- `BotEngine.unrealized(pos)`: live mark-to-market of an open position (marked at the exit side,
  less round-trip fees) → `mark_price_inr`, `unrealized_gross_inr`, `unrealized_net_inr`.
  `GET /api/positions/open` merges these into each row.
- Dashboard Open Positions table: new colour-coded **P/L (₹)** column (`open-position-pnl-{coin}`).
- LOSS INVESTIGATION (user reported "all paper trades closed on loss"): NOT supported by data —
  PAPER history = 361 trades, 82.8% win rate, net +₹8,097, 265 TARGET_HIT. Every position opens
  ~₹1-3 RED (taker crosses bid-ask + 0.10% fees) and turns green only on convergence; TIMEOUTs net
  small losses, TARGET_HITs win. Lowered 0.13% hurdle admits more marginal timeout-losers but
  target-hits dominate. Fix if desired: raise the entry hurdle.


## Prioritized backlog
- P1: Execution latency below the measured lag (0.34–0.46 s): warm keep-alive session +
  pre-signed CoinDCX orders, Mumbai EC2. Then re-run `/api/compare` ≥1 h at IST/US open.
- P1: Maker-mode variant (rest limit at stale laggard price when Binance jumps) to flip fee drag.
- P1: Fresh-book filter for entries (`frozen_s < 1 s`) + hurdle 0.25–0.35% experiment via shadow.
- P1: Execution latency: CoinDCX order create REST ≈1.2 s + fill confirm 0.6–0.9 s
  → ~2 s round trip. Consider pre-warmed aiohttp session / keep-alive for the
  order path, and widen hurdle or use limit-IOC to survive the delay.
- P1: Trial economics: both LIVE trades net-negative at 1x/₹700 (fees 0.059% taker
  ×2 ≈ ₹0.82 per trade). Re-run with hurdle ≥0.15% (replay sweep result) before
  scaling capital.
- P2: Scheduled auto-screening (daily re-rank + Telegram proposal, optional auto-apply).
- P2: Multi-day replay (concatenate several tick files) and sweep over timeout/stop too.
- P2: Verify CoinSwitch PRO listing of screener picks via `instrument_info` once keys exist.
- P2: Dynamic volatility-adjusted stop-loss.

## Deployment
- EC2 t3.small (Mumbai) recommended. Use `tmux` + `python standalone_bot/run_bot.py`.
- IP-restrict API keys; scope to Futures trading only, no withdrawals.
