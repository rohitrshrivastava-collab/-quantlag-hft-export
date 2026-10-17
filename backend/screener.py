"""Dynamic asset-universe screener: rank candidate coins by 24h liquidity × observed lead-lag activity."""
from __future__ import annotations

import json
import math
import statistics
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp

# Binance USDT-M perpetuals that also trade on CoinSwitch PRO / CoinDCX — used when
# fapi.binance.com/exchangeInfo is geo-blocked from the host running the screener.
BINANCE_PERPS_FALLBACK = {
    "BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "AVAX", "LINK", "NEAR", "DOT", "MATIC", "POL", "LTC", "BCH",
    "TRX", "ATOM", "UNI", "APT", "ARB", "OP", "SUI", "SEI", "INJ", "TIA", "FIL", "ETC", "AAVE", "PEPE", "SHIB",
    "WIF", "FET", "RENDER", "JUP", "ORDI", "STX", "LDO", "TON", "WLD", "RUNE", "ENA", "PENDLE", "ONDO", "TAO",
    "HBAR", "ALGO", "XLM", "ICP", "IMX", "GALA", "SAND", "MANA", "AXS", "CRV", "MKR", "COMP", "SNX", "EOS", "XTZ",
    "ZEC", "DASH", "NEO", "KAVA", "GRT", "1INCH", "ENS", "DYDX", "GMX", "BLUR", "CFX", "MINA", "ROSE", "CHZ",
    "APE", "FLOW", "EGLD", "THETA", "ZIL", "ONE", "CELO", "ANKR", "BAT", "STORJ", "SKL", "HOT", "TRB", "AR",
    "PYTH", "STRK", "ZRO", "NOT", "BOME", "FLOKI", "BONK", "TRUMP", "HYPE", "VIRTUAL", "AI16Z", "MOVE", "ME", "USUAL",
}
STABLES = {"USDT", "USDC", "BUSD", "DAI", "FDUSD", "TUSD"}


async def coindcx_futures_universe(session: aiohttp.ClientSession) -> Dict[str, Dict[str, Any]]:
    """{coin: {volume_24h_usdt, last, vol_pct, pair}} for CoinDCX USDT-margined futures.

    CoinDCX public RT feed carries per-pair `ls` (last), `v` (24h quote volume),
    `h`/`l` (24h high/low) and `pc` (24h change %). We derive a short-term
    volatility proxy = 24h price range / last (falls back to |pc|)."""
    out: Dict[str, Dict[str, Any]] = {}
    async with session.get("https://public.coindcx.com/market_data/v3/current_prices/futures/rt") as r:
        d = await r.json(content_type=None)
    for pair, p in (d.get("prices") or {}).items():
        if not pair.startswith("B-") or not pair.endswith("_USDT"): continue
        coin = pair[2:-5]
        if coin in STABLES or coin.startswith("1000"): continue
        try:
            last = float(p.get("ls") or 0); vol = float(p.get("v") or 0)
            hi = float(p.get("h") or 0); lo = float(p.get("l") or 0); pc = float(p.get("pc") or 0)
        except (TypeError, ValueError):
            continue
        if last <= 0: continue
        vol_pct = (hi - lo) / last * 100.0 if hi > 0 and lo > 0 and hi >= lo else abs(pc)
        out[coin] = {"pair": pair, "last": last, "volume_24h_usdt": vol, "vol_pct": round(vol_pct, 3)}
    return out


async def build_universe(*, laggard: str, top_n: int, max_price_usd: float = 0.0,
                         min_volume_usdt: float = 5e6, min_vol_pct: float = 1.0,
                         keep: Optional[List[str]] = None) -> Dict[str, Any]:
    """Live shortlist of the best-suited pairs for lag-arb.

    Filters (all must pass): listed on Binance perps AND the active laggard,
    24h quote volume ≥ min_volume_usdt, short-term volatility ≥ min_vol_pct,
    and (if max_price_usd>0) live price ≤ max_price_usd. Survivors are ranked by
    volatility (strongest lag-arb edge). `keep` (currently-tracked coins that still
    qualify) are held to reduce churn; freed slots are filled by the highest-vol
    newcomers. Returns up to `top_n` coins."""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as s:
        market = await coindcx_futures_universe(s)
        bn = await binance_perps(s)
    binance_source = "binance_exchangeInfo" if bn else "fallback_list"
    bn = bn or BINANCE_PERPS_FALLBACK
    keep_set = {c.upper() for c in (keep or [])}
    rows: List[Dict[str, Any]] = []
    for coin, v in market.items():
        on_both = coin in bn
        vol_ok = v["volume_24h_usdt"] >= min_volume_usdt
        volp_ok = v["vol_pct"] >= min_vol_pct
        price_ok = (max_price_usd <= 0) or (v["last"] <= max_price_usd)
        qualifies = on_both and vol_ok and volp_ok and price_ok
        rows.append({"coin": coin, "last": v["last"], "volume_24h_usdt": round(v["volume_24h_usdt"], 0),
                     "vol_pct": v["vol_pct"], "on_both": on_both, "vol_ok": vol_ok,
                     "volp_ok": volp_ok, "price_ok": price_ok, "qualifies": qualifies})
    qualified = sorted((r for r in rows if r["qualifies"]), key=lambda r: r["vol_pct"], reverse=True)
    top_n = max(1, top_n)
    kept = [r["coin"] for r in qualified if r["coin"] in keep_set]
    fresh = [r["coin"] for r in qualified if r["coin"] not in keep_set]
    selected = (kept + fresh)[:top_n]
    rows.sort(key=lambda r: (r["qualifies"], r["vol_pct"]), reverse=True)
    return {"selected": selected, "laggard": laggard, "top_n": top_n,
            "candidates": len(rows), "qualified": len(qualified),
            "binance_source": binance_source, "coindcx_instruments": len(market),
            "min_volume_usdt": min_volume_usdt, "min_vol_pct": min_vol_pct,
            "max_price_usd": max_price_usd, "rows": rows[:60]}


async def binance_perps(session: aiohttp.ClientSession) -> Optional[set]:
    try:
        async with session.get("https://fapi.binance.com/fapi/v1/exchangeInfo") as r:
            if r.status != 200: return None
            d = await r.json(content_type=None)
        return {s["baseAsset"] for s in d.get("symbols", [])
                if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING"}
    except Exception:
        return None


def lag_activity_from_ticks(path: Path, coins: set, max_lines: int = 800_000) -> Dict[str, Dict[str, float]]:
    """Per-coin std-dev and 95th pct of |raw spread| (bps) from today's recorded ticks."""
    vals: Dict[str, List[float]] = {}
    if not path.exists(): return {}
    with open(path) as fh:
        for i, line in enumerate(fh):
            if i > max_lines: break
            try: r = json.loads(line)
            except ValueError: continue
            c = r["c"]
            if c not in coins: continue
            lb, la, cb, ca = r["lb"], r["la"], r["cb"], r["ca"]
            if not (lb and la and cb and ca): continue
            lm = (cb + ca) / 2
            vals.setdefault(c, []).append(((lb + la) / 2 - lm) / lm * 1e4)
    out = {}
    for c, v in vals.items():
        if len(v) < 30: continue
        med = statistics.median(v)
        dev = sorted(abs(x - med) for x in v)
        out[c] = {"std_bps": round(statistics.pstdev(v), 3), "p95_dev_bps": round(dev[int(0.95 * (len(dev) - 1))], 3), "ticks": len(v)}
    return out


async def screen(*, current: List[str], top_n: int, tick_file: Optional[Path], hurdle_bps: float,
                 min_volume_usdt: float = 5e6) -> Dict[str, Any]:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as s:
        dcx = await coindcx_futures_universe(s)
        bn = await binance_perps(s)
    binance_source = "binance_exchangeInfo" if bn else "fallback_list"
    bn = bn or BINANCE_PERPS_FALLBACK
    cands = {c: v for c, v in dcx.items() if c in bn and v["volume_24h_usdt"] >= min_volume_usdt}
    lag = lag_activity_from_ticks(tick_file, set(cands) | set(current)) if tick_file else {}
    vols = [math.log10(max(v["volume_24h_usdt"], 1)) for v in cands.values()] or [0]
    vmin, vmax = min(vols), max(vols)
    known = [min(1.0, lg["p95_dev_bps"] / max(hurdle_bps, 1e-6)) for lg in lag.values()]
    neutral = statistics.median(known) if known else 0.5   # unknown coins get the observed median, not a free 0.5
    rows = []
    for c, v in cands.items():
        vscore = (math.log10(max(v["volume_24h_usdt"], 1)) - vmin) / (vmax - vmin) if vmax > vmin else 1.0
        lg = lag.get(c)
        # lag score: how often divergence approaches the hurdle (p95 dev vs hurdle), capped at 1
        lscore = min(1.0, lg["p95_dev_bps"] / max(hurdle_bps, 1e-6)) if lg else None
        score = 0.6 * vscore + 0.4 * (lscore if lscore is not None else neutral)
        rows.append({"coin": c, "volume_24h_usdt": round(v["volume_24h_usdt"], 0), "last": v["last"],
                     "lag_std_bps": lg["std_bps"] if lg else None, "lag_p95_dev_bps": lg["p95_dev_bps"] if lg else None,
                     "lag_ticks": lg["ticks"] if lg else 0, "volume_score": round(vscore, 3),
                     "lag_score": None if lscore is None else round(lscore, 3), "score": round(score, 3),
                     "tracked": c in current})
    rows.sort(key=lambda r: r["score"], reverse=True)
    recommended = [r["coin"] for r in rows[:top_n]]
    return {"candidates": len(rows), "binance_source": binance_source, "coindcx_instruments": len(dcx),
            "min_volume_usdt": min_volume_usdt, "hurdle_bps": hurdle_bps, "lag_data_coins": len(lag),
            "neutral_lag_score": round(neutral, 3),
            "recommended": recommended, "add": [c for c in recommended if c not in current],
            "drop": [c for c in current if c not in recommended], "rows": rows[:60]}
