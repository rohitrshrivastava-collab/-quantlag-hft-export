# QuantLag HFT — Lead-Lag Latency Arbitrage Bot

Automated cross-exchange arbitrage that exploits price-discovery delay between
**Binance USDT-M Futures** (leader) and **CoinSwitch PRO Crypto Futures** (INR
laggard).  Ships as:

1. **Standalone async Python bot** (`standalone_bot/`) — runs in VS Code
   locally or on an **AWS EC2 Ubuntu instance in `ap-south-1` (Mumbai)** for
   lowest RTT to CoinSwitch. SQLite state, Telegram alerts, crash recovery.
2. **FastAPI + React web dashboard** (`backend/`, `frontend/`) — live view of
   spreads, open/closed positions, PnL, equity curve, latency, and config.

The same async engine (`backend/bot_engine.py`) powers both.

---

## System Architecture

```
                   ┌────────────────────────────┐
                   │      DataIngestion         │
                   │  Binance WS + CoinSwitch WS│
                   │  + USDT/INR FX poller      │
                   └────────────┬───────────────┘
                                │ TopOfBook stream
                                ▼
┌────────────────┐    ┌────────────────────────┐    ┌────────────────┐
│ PortfolioMgr   │◄──►│      SignalEngine       │◄──►│ OrderExecutor  │
│ (10 slots,     │    │ - Net-spread hurdle gate│    │ - Paper fills  │
│  equal weight, │    │ - Direction (LONG/SHORT)│    │ - Live REST    │
│  ₹400 default) │    │ - Timeout / stop-loss   │    │ - Partial-fill │
└────────────────┘    └────────────┬───────────┘    └────────┬───────┘
                                   │                          │
                                   ▼                          ▼
                            ┌───────────────┐        ┌────────────────┐
                            │ Store         │        │ TelegramReport │
                            │ SQLite/Mongo  │        │ trade+hourly   │
                            └───────────────┘        └────────────────┘
```

**Modules (in `backend/bot_engine.py`)**

| Module | Responsibility |
| --- | --- |
| `MarketSimulator` / `LiveBinanceFeed` | Top-of-book feed via WebSocket (or realistic simulator when `SIMULATE_MARKET=true`) |
| `FXRateService` | Real-time USDT/INR quote (CoinGecko median, 30 s cadence, fallback to config) |
| `SignalEngine` (`_scan_loop`) | Every ~400 ms: computes `(Binance INR-mid − CoinSwitch mid) / CoinSwitch mid × 100`. Fires LONG if gross > hurdle, SHORT if `-gross > hurdle` |
| `PortfolioManager` | Dynamic equal-weight sizing: `equity / num_slots`. Enforces one-position-per-coin lock |
| `OrderExecutor` (`_open_position`, `_close_position`) | Paper: simulates fills at top-of-book with 80–220 ms latency. Live: REST hook (stubbed — plug your CoinSwitch REST client) |
| `TelegramReporter` | Sends HTML-formatted entry/exit alerts + hourly summary |
| `SQLiteStore` / `MemoryStore` | Pluggable persistence — both implement the same interface |

**Net-spread hurdle gate**
```
hurdle_pct = entry_fee_pct + exit_fee_pct + slippage_buffer_pct + hurdle_extra_pct
enter if abs(gross_spread_pct) > hurdle_pct
```

**Exit conditions** (checked every ~300 ms per open position):
- **TARGET_HIT** — CoinSwitch price catches Binance INR reference at entry
- **STOP_LOSS** — hard volatility stop (`STOP_LOSS_PCT`)
- **TIMEOUT** — time-based flush (`TIMEOUT_SECONDS`, default 45 s)

**Resilience**
- WS auto-reconnect with exponential backoff (1s → 30s cap)
- FX poller falls back to last-known / config value on REST failure
- Async token-bucket ready to add for REST fan-out; SQLite restart resumes open positions and continues managing stops/timeouts

---

## Quick Start — Local (VS Code)

```bash
git clone <your-repo> && cd <your-repo>
python -m venv venv && source venv/bin/activate    # Windows: venv\Scripts\activate
pip install -r standalone_bot/requirements.txt
cp standalone_bot/.env.example standalone_bot/.env # edit to add your keys
python standalone_bot/run_bot.py
```

Bot boots in **paper mode** with the built-in simulator by default. You'll see
console snapshots every 15 s and, if configured, Telegram alerts per trade
plus an hourly summary.

## Quick Start — AWS EC2 Ubuntu (`ap-south-1`, Mumbai)

```bash
ssh ubuntu@<ec2-public-dns>
sudo apt update && sudo apt install -y python3.11-venv git tmux
git clone <your-repo> && cd <your-repo>
python3 -m venv venv && source venv/bin/activate
pip install -r standalone_bot/requirements.txt
cp standalone_bot/.env.example standalone_bot/.env
nano standalone_bot/.env      # fill Binance/CoinSwitch/Telegram creds
tmux new -s quantlag
python standalone_bot/run_bot.py
# Ctrl-b d to detach; `tmux attach -t quantlag` to resume
```

Recommended EC2 instance: **t3.small** or **c6i.large** in `ap-south-1a`
(shortest RTT to CoinSwitch/Binance edge PoPs).

## Web Dashboard

The dashboard runs automatically in this workspace at the preview URL.
Locally:

```bash
# backend
cd backend && uvicorn server:app --reload --port 8001
# frontend
cd frontend && yarn && yarn start
```

Dashboard endpoints (`/api/*`): `status`, `spreads`, `positions/open`,
`positions/closed`, `equity-curve`, `logs`, `telemetry`, `hourly-summary`,
`config` (GET/PUT), `bot/start`, `bot/stop`, `bot/mode`,
`positions/{coin}/close`.

---

## Configuration Reference (`.env`)

| Key | Default | Meaning |
| --- | --- | --- |
| `PAPER_TRADING` | `true` | Simulate fills against real top-of-book without touching CoinSwitch REST |
| `SIMULATE_MARKET` | `true` | Use the built-in market simulator instead of real WS feeds |
| `STARTING_CAPITAL_INR` | `4000` | Base equity divided across `num_slots` |
| `ENTRY_FEE_PCT` / `EXIT_FEE_PCT` | `0.05` | CoinSwitch taker fees (per side) |
| `SLIPPAGE_BUFFER_PCT` | `0.03` | Additional slippage guard |
| `HURDLE_EXTRA_PCT` | `0.02` | Extra edge required beyond fees+slippage |
| `TIMEOUT_SECONDS` | `45` | Time-based flush window |
| `STOP_LOSS_PCT` | `0.35` | Hard volatility stop-loss |
| `FX_USDT_INR` | `84.5` | Fallback USDT/INR rate |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | *(empty)* | Optional Telegram alerts |
| `BINANCE_API_KEY` / `_SECRET` | *(empty)* | Required only when disabling `PAPER_TRADING` |
| `COINSWITCH_API_KEY` / `_SECRET` | *(empty)* | Ed25519 hex-encoded key pair from CoinSwitch PRO. Required when disabling `PAPER_TRADING`. Base URL defaults to `https://coinswitch.co` (override with `COINSWITCH_BASE_URL`) |
| `LIVE_ORDER_TIMEOUT_S` | `5.0` | Max seconds to poll `get_order_status` before treating a live order as timed out |
| `COINSWITCH_WS_URL` | *(empty)* | Optional override of the public CoinSwitch PRO Futures book WS (`wss://ws.coinswitch.co/pro/realtime-rates-socket/futures/exchange_2/?EIO=4&transport=websocket`). No keys needed. |
| `LAGGARD_STALE_AFTER_S` | `10` | Seconds of WS silence before laggard quotes are marked STALE and the REST ticker poller (keys required) takes over |

## Going Live (checklist)

1. Set `PAPER_TRADING=false` **and** `SIMULATE_MARKET=false`.
2. Fill `COINSWITCH_API_KEY` and `COINSWITCH_API_SECRET` (Ed25519 hex from
   CoinSwitch API Portal → Futures API). IP-restrict the key to your
   trading host.
3. Verify connectivity: `curl -X POST $BASE_URL/api/coinswitch/check`
   should return `{"ok": true, ...}`. In the dashboard use the **Run check**
   button in the config panel — it calls `wallet_balance` under real Ed25519
   signing and confirms your keys work.
4. Run at least **72 hours in paper mode** first to verify gross spreads
   consistently exceed the hurdle gate.
5. Deploy on EC2 Mumbai (`ap-south-1`) for RTT symmetry with CoinSwitch.

## Live-mode order path (already wired)

`backend/coinswitch_client.py` is an async Ed25519-signed client covering:
`wallet_balance`, `positions`, `instrument_info`, `ticker`, `order_book`,
`place_order`, `get_order_status`, `cancel_order`, `cancel_all_open_orders`.

- **Entry** (`_open_position` LIVE branch): converts INR slot allocation →
  USDT quantity via current FX and Binance USDT price, submits a `MARKET`
  order on `EXCHANGE_2` (`{COIN}USDT`), then polls `get_order_status`
  until terminal (`EXECUTED` / `PARTIALLY_EXECUTED` are both accepted;
  Futures treats partial as terminal per CoinSwitch docs). The engine
  records the **actual** fill quantity, average fill price × FX, and real
  `execution_fee`.
- **Exit** (`_close_position` LIVE branch): submits an opposite-side
  `MARKET` order with `reduce_only=true` for the exact filled quantity —
  handles partial closes by shrinking `pos.contracts` to whatever actually
  executed.
- **Laggard feed in live mode**: `LiveCoinSwitchTickerPoller` refreshes each
  coin's `ticker` every ~5 s. Replace with a real WS book stream when
  available.

## Safety notes

- All exchange API keys should be **IP-restricted** to your EC2 elastic IP
  and scoped to Futures trading only (no withdrawals).
- Live entries use `MARKET` orders — set `HURDLE_EXTRA_PCT` conservatively to
  absorb worst-case taker slippage.
- Live exits use `reduce_only=true` — they can never accidentally flip your
  position to the opposite side.
