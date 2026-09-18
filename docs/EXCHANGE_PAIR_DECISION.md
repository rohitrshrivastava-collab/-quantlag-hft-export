# Exchange Pair Decision — Binance → CoinSwitch PRO (Pair A) vs Binance → CoinDCX (Pair B)

*Quantitative systems architecture note for the QuantLag lead-lag latency-arbitrage bot (AWS EC2, ap-south-1).*

Everything below is wired into the product:

| Deliverable | Where |
|---|---|
| Python benchmark tool (asyncio + `websockets`) | `benchmark/latency_bench.py` — CLI; also run server-side from the dashboard **Exchange Pair Benchmark** panel (`POST /api/benchmark/run`, `GET /api/benchmark/status`) |
| Verdict framework (decision rules as code) | `backend/verdict.py` → `GET /api/benchmark/verdict` → dashboard **Verdict** card |
| This document | `docs/EXCHANGE_PAIR_DECISION.md` |

---

## 1. Python benchmark tool

```bash
# on the EC2 box that will run the bot (numbers from anywhere else are not your numbers)
pip install -r benchmark/requirements.txt          # only `websockets`
python benchmark/latency_bench.py --duration 60 --symbols BTC,ETH,SOL --json bench_$(date +%H%M).json
```

What it measures, concurrently, for **Binance Futures, CoinSwitch PRO Futures, CoinDCX Futures**:

| Metric | How | Why it matters |
|---|---|---|
| **TCP RTT p50 / p99** | fresh TCP connect to the exchange WS edge every 1 s (1 round-trip, no TLS) | proxy for network distance and the floor of your *order* round-trip (ICMP is often blocked; TCP connect is what your order actually rides on) |
| **WS tick-to-receipt p50 / p99** | `exchange event timestamp → local receipt` for every book message (Binance `bookTicker.E`, CoinSwitch snapshot `timestamp`, CoinDCX `depth-update.ts`) | how stale the *laggard quote you act on* is; p99 is your tail risk of trading on a stale book |
| **msg/s, max gap, reconnects** | counters + inter-arrival | WebSocket stability — a 5 s gap on the laggard means you are blind while the leader moves |
| **spread (bps) & depth within ±10 bps (USDT)** | from the reconstructed top-of-book (Binance `depth5`, CoinSwitch full snapshot, CoinDCX snapshot + deltas) | taker cost floor and whether a ₹400–₹4,000 clip moves the book |
| **divergence bps p50 / p99 + quantile curve** | on every laggard tick: `|laggard_mid / binance_mid − 1|` | the *actual* exploitable dislocation distribution; the quantile curve lets you apply any fee hurdle after the fact |

Protocol notes (already handled in the tool):

* **Binance**: raw WS, `wss://fstream.binance.com/stream?streams=…@bookTicker/…@depth5@100ms`.
* **CoinSwitch PRO**: Socket.IO **v4** (EIO=4) spoken by hand: `0` → send `40/exchange_2,`; `2` → reply `3`; subscribe with `42/exchange_2,["FETCH_ORDER_BOOK_CS_PRO",{"event":"subscribe","pair":"BTCUSDT"}]`. Full snapshots, ~5/s per symbol, symbol comes back as `s`.
* **CoinDCX**: Socket.IO **v2** (EIO=3): `40` → `42["join",{"channelName":"B-BTC_USDT@orderbook@20-futures"}]`; client must send `2` every ~20 s. `depth-update` deltas (qty `0.0` = delete) with a `depth-snapshot` every ~2 s; symbol comes back as `BTCUSDT`.

Keep the host NTP-synced (`chronyd` on Amazon Linux/Ubuntu). Tick-to-receipt is meaningless with a skewed clock. Run **≥ 3 times, 300 s each, at different hours** (Asia open, EU open, US open) before deciding.

Reference run from the preview host (not Mumbai — indicative only):

```
exchange                  tcp p50  tcp p99  tick p50  tick p99  msg/s  maxgap  rc  spr bps  dep10bps$  dev p50  dev p99
Binance Futures (LEAD)      149      247        77       535    617     102   0    0.04    785,238        —        —
CoinSwitch PRO (LAG)         15      102       148       170      9     220   0    0.04    646,302     1.10     1.50
CoinDCX Futures (LAG)       241      333       117       129      4     440   0    0.03    930,106     0.00     0.50
```

---

## 2. Exchange mechanics — CoinSwitch PRO vs CoinDCX (Futures)

| Dimension | CoinSwitch PRO Futures | CoinDCX Futures | Edge |
|---|---|---|---|
| **Order-book feed** | Socket.IO v4, **full snapshot per push** (no delta bookkeeping, no sequence-gap risk), ~5 snapshots/s/symbol, public/unauthenticated | Socket.IO v2 (EIO=3), **delta stream** (`depth-update`) + periodic `depth-snapshot`; must apply deltas, watch `vs` versions, resync on snapshot; public | A — simpler, harder to corrupt |
| **Tick-to-receipt (measured)** | ~150 ms p50 / ~170 ms p99 (Mumbai will be lower) | ~115 ms p50 / ~130 ms p99 | B slightly fresher quotes |
| **Book depth (BTC, ±10 bps)** | ~$0.6–0.8 M | ~$0.9–1.2 M | B deeper (both ≫ your ₹400 slot) |
| **Spread (BTC)** | ~0.04 bps (tick 0.10) | ~0.01–0.03 bps (tick 0.10) | tie for your size |
| **Taker fee** | per-instrument, returned by `GET /trade/api/v2/futures/instrument_info` (`taker_fee_rate`, e.g. 0.00065 = 0.065%); your config uses 0.05% | VIP-tiered by 30-day volume; base tier commonly quoted **0.075 % taker / 0.025 % maker** (USDT-M); INR-M 0.05 % / 0.02 % | **A** (≈ 5 bps/round-trip cheaper at base tier) |
| **Round-trip fee hurdle (2×taker)** | 0.10–0.13 % | 0.15 % (USDT-M base) | A |
| **Order API rate limit** | **20 req / 60 s per private endpoint** → ~3 s between orders per endpoint. HTTP 429 beyond. Bot enforces this with a token bucket (`backend/rate_limiter.py`) | 16 req/s, 960 req/min general; order create/cancel/status 2000 / 60 s | **B** by a mile — A caps you at ~10 round-trips/min *total* |
| **Order auth** | Ed25519 signature over `METHOD + path + epoch_ms` | HMAC-SHA256 over JSON body with `timestamp` (must be within 5 s of server) | tie |
| **Reduce-only / market orders** | `reduce_only=true`, `MARKET` supported | market + `stop_market`/`take_profit_*`; quick exit endpoint | tie |
| **Order/position WS** | **not available** on Futures — poll `get_order_status` | authenticated `coindcx` channel: `df-order-update`, `df-position-update`, `balance-update` | **B** (no fill polling, fewer REST calls) |
| **Settlement / FX** | USDT-margined; INR shown via internal FX | USDT-M and **INR-margined** contracts (fixed `conversion_price` published via API) | B for INR-native capital |
| **Stability observed** | 0 reconnects in all runs; steady 200 ms cadence | 0 reconnects; deltas arrive *before* the first snapshot — book is authoritative only after the first `depth-snapshot` | A simpler; B needs snapshot guard |
| **Official client support** | REST + WS docs at `api-trading.coinswitch.co` with Python samples; separate HFT/DMA surface (`dma.coinswitch.co/v5`) on request | docs at `docs.coindcx.com`; WS requires socket.io-client **2.4.0** exactly | tie |

**Take-aways**

* **Costs favour Pair A**; **throughput and freshness favour Pair B**.
* Pair A's 20 req/min order limit is the binding constraint for a 10-slot bot: each round trip = 2 orders + ≥1 status poll ⇒ **≤ ~6 full trades/min** across *all* slots. If the benchmark shows > 6 opportunities/min on Pair A, you are limited by the API, not the market.
* Pair B's delta book needs the snapshot guard and version checks; its base taker fee makes the hurdle 20 bps vs 15 bps — a 33 % higher bar per trade.

---

## 3. Verdict framework (implemented in `backend/verdict.py`)

### 3.1 Gates (hard fail → pair is *disqualified* for live capital)

| Gate | Rule | Rationale |
|---|---|---|
| **G1 feed reliability** | `ws_msgs > 0` and `reconnects == 0` and `max_gap < 5000 ms` and `tick p99 < 1500 ms` over the run | a laggard you cannot see is a stop-loss waiting to happen |
| **G2 execution budget** | `est_order_rtt = 2 × TCP p50 + 60 ms < 500 ms` | market order + fill confirmation must land inside the laggard's catch-up window (typically 0.3–2 s) |

### 3.2 Net-profitability calculation (per pair)

```
hurdle_pct      = 2 × taker_fee_pct + slippage_pct + hurdle_extra_pct       # your config: slippage 0.03, extra 0.02
hurdle_bps      = hurdle_pct × 100

opp_share       = P(|divergence| ≥ hurdle_bps)            # from the divergence quantile curve
mean_excess_bps = E[ |divergence| − hurdle_bps | above ]  # average edge *after* costs
opps_per_min    = opp_share × laggard_msgs_per_s × 60

net_edge_inr_per_opp = slot_inr × mean_excess_bps / 10 000
expected_inr_per_hour = opps_per_min × 60 × net_edge_inr_per_opp            # per slot, before API-limit cap
```

Cap `opps_per_min` at the API limit: Pair A ≈ 6/min (all slots), Pair B ≈ 300/min.

### 3.3 Score (0–100) and decision rules

| Component | Weight | Formula |
|---|---|---|
| Net edge | 40 | `40 × edge_bps_per_tick / max(edge across pairs)` where `edge_bps_per_tick = opp_share × mean_excess_bps` |
| Fees | 20 | `20 × min_fee / fee` |
| Latency | 20 | `20 × (1500 − tick p99) / 1500` |
| Book quality | 10 | `10 × (1 − spread_bps / (2 × slippage_bps))` |
| Stability | 10 | 10 if G1 passes else 0 |

**Decision rules**

1. Rank only pairs that pass **both gates**. If none pass, do not go live; fix infra (EC2 region, instance type, NTP) and rerun.
2. **Winner = highest score**. If the margin is **< 5 pts → inconclusive**: rerun 300 s at 2–3 different hours and compare medians.
3. **Profitability veto**: if the winner's `expected_inr_per_hour × slots` (after API cap) is below `1.5 × (EC2 hourly cost + expected funding drag)`, the strategy is not viable on either pair at current fees — negotiate fee tier (`api@coinswitch.co` / CoinDCX VIP) or widen to more symbols.
4. **Rate-limit veto for Pair A**: if `opps_per_min_A > 6`, cap and recompute; if Pair B then wins on expected ₹/h, prefer B despite higher fees.
5. **Tail-risk veto**: a pair with `tick p99 > 1 s` or `max gap > 2 s` in *any* of the three runs loses 10 stability points in *all* runs (one bad hour is enough to give back a day of edge).
6. Re-run the benchmark **monthly** and after any exchange incident/upgrade; feeds change (CoinSwitch moved from REST-only to WS book recently).

### 3.4 Reading the dashboard Verdict card

* **Score bars** show where a pair wins/loses (edge vs fees vs latency).
* **Fee inputs** let you plug your actual tier (from `instrument_info` / CoinDCX Fee Levels) without redeploying — the verdict recomputes live.
* **Flags** call out the exact gate or veto that fired.
* **Confidence** is LOW/MEDIUM/HIGH from margin and run length; do not trade LIVE on LOW.

---

## 4. Practical recommendation

*Default to **Pair A (CoinSwitch PRO)** for a ₹4,000 / 10-slot book*: lowest fees, simplest and most robust book feed, and the 20 req/min order cap is not binding at this capital level (you will rarely see > 6 tradable dislocations a minute after a 15 bps hurdle).

*Switch to **Pair B (CoinDCX)*** when (a) your benchmark shows Pair A capped by the API limit, (b) you obtain a CoinDCX VIP tier that brings taker ≤ 0.05 %, or (c) you want INR-margined contracts and WS order/position updates to drop fill polling. Pair B is the scaling path; Pair A is the cheapest place to prove the edge.
