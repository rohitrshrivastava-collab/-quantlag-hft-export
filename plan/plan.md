# Plan: Dynamic Coin Selection + Max-Price Filter

## Goal
Replace the fixed 10-coin whitelist with a live, automatically-refreshed universe of the
best-suited pairs, and add an on-screen "max coin price" control so you can restrict trading
to cheap coins (e.g. only coins priced ≤ $1) whenever you want.

## What changes for you

### 1. Coins are now chosen automatically (no more manual list)
Instead of trading a hard-coded list (BTC, ETH, SOL, …), the bot will continuously build its
own shortlist of tradable coins that meet all of these conditions:
- **Available on both exchanges** — listed as a futures pair on Binance AND on your active
  laggard (CoinSwitch or CoinDCX). Coins that only exist on one side are ignored.
- **High liquidity** — enough 24h trading volume to fill orders without heavy slippage.
- **High volatility** — enough recent price movement to produce lag-arbitrage opportunities.
- **Within your max-price limit** (see below).

The shortlist refreshes on a schedule (roughly once a minute) so newly-hot coins rotate in and
stale ones drop out automatically. Liquidity and volatility cut-offs use sensible built-in
defaults (not shown as on-screen controls for now, per your call).

### 2. How many coins are monitored
The bot monitors exactly as many coins as you have trade **slots** (currently 10). So with 10
slots it tracks the 10 best-ranked qualifying coins at a time. Change the slot count and the
monitored-coin count moves with it.

### 3. Ranking — which coins win the slots
When more coins qualify than there are slots, the bot keeps the ones with the **highest
short-term volatility** (the strongest lag-arb edge). Volume/liquidity is used as a pass/fail
filter, volatility decides the ranking.

### 4. New on-screen control: Max Coin Price (USD)
A new input on the dashboard lets you cap the price of coins the bot will consider.
- Set it to `1` → the bot only monitors/trades coins whose live Binance USD price is ≤ $1.
- Leave it **blank or 0** → no price cap; all qualifying coins are eligible.
- The cap applies on top of the liquidity/volatility/both-exchanges filters above.
- Changes take effect on the next universe refresh (within ~a minute).

### 5. Entry rule tightened to your exact formula
An entry now fires only when:

    Divergence % > (entry fee % + exit fee % + slippage buffer %)

The small extra safety margin the code used to add on top is **removed**, so the gate matches
your formula exactly. The USDT↔INR conversion continues to use the live, dynamic exchange rate
already in the system (no hard-coded multiplier).

## What is NOT changing
- Paper trading stays the default; live mode is unchanged.
- Slots, leverage, equity, stop-loss, order-book depth/slippage guard, spike-persistence filter,
  and the laggard toggle (CoinSwitch/CoinDCX) all keep working as they do today.
- All existing dashboard controls remain; the max-price input is added alongside them.

## Assumptions (tell me if any are wrong)
- Max-price filter compares against the coin's live Binance USD (USDT) price.
- Liquidity and volatility thresholds are fixed defaults for now, tuned to keep the shortlist
  to genuinely tradable coins; these can be exposed as controls later if you want.
- "Short-term volatility" for ranking is measured over a recent rolling window (e.g. 24h price
  swing / recent price movement), consistent with the data the bot already tracks.
