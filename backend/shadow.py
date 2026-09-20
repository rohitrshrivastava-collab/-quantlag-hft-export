"""Shadow comparison: paper-simulate the same lead-lag rules on BOTH laggard exchanges at once.

Each ShadowExchange gets its own fee, execution-latency assumption and live order book, so the
head-to-head shows whether CoinSwitch's wider lag window beats CoinDCX's tighter book / faster fills.
"""
from __future__ import annotations

import asyncio
import logging
import statistics
import time
from typing import Any, Dict, List, Optional

from liquidity import assess

logger = logging.getLogger("shadow")
EXCHANGES = ("coinswitch", "coindcx")
LADDER_INR = (10_000, 100_000, 1_000_000)   # slippage ladder: how much size each book absorbs


def _median(xs: List[float]) -> Optional[float]:
    return round(statistics.median(xs), 3) if xs else None


def _pct(xs: List[float], q: float) -> Optional[float]:
    if not xs: return None
    s = sorted(xs); return round(s[min(len(s) - 1, int(q * len(s)))], 3)


class ShadowExchange:
    def __init__(self, name: str, fee_pct: float, latency_s: float) -> None:
        self.name = name
        self.fee_pct = fee_pct
        self.latency_s = latency_s
        self.basis: Dict[str, List[tuple]] = {}
        self.open: Dict[str, Dict[str, Any]] = {}
        self.pending: Dict[str, Dict[str, Any]] = {}      # coin -> entry waiting for exec latency
        self.pending_exit: Dict[str, Dict[str, Any]] = {}
        self.episode_start: Dict[str, float] = {}
        self.lag_windows: List[float] = []
        self.slips: List[float] = []
        self.depths: List[float] = []
        self.holds: List[float] = []
        self.target_holds: List[float] = []   # signal → target catch-up time (the exploitable lag)
        self.spreads_abs: List[float] = []
        self.signals = self.trades = self.wins = 0
        self.phantom_skips = self.thin_skips = self.edge_skips = self.shrunk = 0
        self.gross = self.fees = self.net = 0.0
        self.reasons: Dict[str, int] = {}
        self.equity_curve: List[Dict[str, float]] = []
        self.ladder: Dict[int, List[float]] = {n: [] for n in LADDER_INR}   # notional → one-side slippage % samples
        self._ladder_ts: Dict[str, float] = {}

    def _sample_ladder(self, coin: str, now: float, feed, fx: float) -> None:
        if now - self._ladder_ts.get(coin, 0.0) < 2.0 or coin not in feed.depth: return
        self._ladder_ts[coin] = now
        bids, asks = feed.depth[coin]
        for n in LADDER_INR:
            worst = 0.0
            for levels in (bids, asks):
                liq = assess(levels, n / fx, 100.0, 10.0, 0.0, float("inf"))
                worst = max(worst, liq.slippage_pct if liq.state == "LIQUID" else 100.0)
            xs = self.ladder[n]; xs.append(worst)
            if len(xs) > 3000: self.ladder[n] = xs[-3000:]

    # ---- helpers -----------------------------------------------------------
    def _basis(self, coin: str, gross_pct: float, now: float, window_s: int, enabled: bool) -> float:
        s = self.basis.setdefault(coin, [])
        s.append((now, gross_pct))
        cutoff = now - window_s
        if s and s[0][0] < cutoff:
            self.basis[coin] = s = [x for x in s if x[0] >= cutoff]
        if not enabled or len(s) < 20: return 0.0
        vals = sorted(v for _, v in s)
        return vals[len(vals) // 2]

    def hurdle(self, cfg, slip_one_side: float = 0.0) -> float:
        return 2 * self.fee_pct + max(cfg.slippage_buffer_pct, 2 * slip_one_side) + cfg.hurdle_extra_pct

    def _liq(self, feed, coin: str, direction: str, notional_inr: float, fx: float, cfg, leader_fresh: bool):
        if coin not in feed.depth: return None
        bids, asks = feed.depth[coin]
        levels = asks if direction == "LONG" else bids
        liq = assess(levels, notional_inr / fx, cfg.max_slippage_pct / 2, cfg.depth_bps,
                     feed.frozen_s(coin), cfg.phantom_frozen_s if leader_fresh else float("inf"))
        return liq

    # ---- per-coin step -----------------------------------------------------------
    def step(self, coin: str, now: float, leader, lagg, feed, fx: float, cfg, slot_notional_inr: float) -> None:
        leader_inr_mid = leader.mid * fx
        gross_pct = (leader_inr_mid - lagg.mid) / lagg.mid * 100.0
        basis = self._basis(coin, gross_pct, now, cfg.basis_window_s, cfg.basis_filter)
        adj = gross_pct - basis if cfg.basis_filter else gross_pct
        self.spreads_abs.append(abs(adj))
        if len(self.spreads_abs) > 5000: self.spreads_abs = self.spreads_abs[-5000:]
        hurdle = self.hurdle(cfg)
        leader_fresh = (now * 1000 - leader.ts_ms) < 2000
        self._sample_ladder(coin, now, feed, fx)

        # lag-window episodes: |adj| ≥ hurdle → until it decays below half the hurdle
        if abs(adj) >= hurdle and coin not in self.episode_start:
            self.episode_start[coin] = now
        elif coin in self.episode_start and (abs(adj) < hurdle / 2 or now - self.episode_start[coin] > 120):
            self.lag_windows.append(now - self.episode_start.pop(coin))
            if len(self.lag_windows) > 2000: self.lag_windows = self.lag_windows[-2000:]

        # pending entry → fill after exec latency at the then-current VWAP
        p = self.pending.get(coin)
        if p and now >= p["fire_at"]:
            self.pending.pop(coin)
            liq = self._liq(feed, coin, p["direction"], p["notional_inr"], fx, cfg, leader_fresh)
            entry = (liq.vwap * fx) if (liq and liq.vwap > 0) else (lagg.ask if p["direction"] == "LONG" else lagg.bid)
            stop = entry * (1 - cfg.stop_loss_pct / 100) if p["direction"] == "LONG" else entry * (1 + cfg.stop_loss_pct / 100)
            self.open[coin] = {**p, "entry": entry, "stop": stop, "contracts": p["notional_inr"] / entry,
                               "opened_at": now, "timeout_at": p["signal_ts"] + cfg.timeout_seconds}
            return

        pe = self.pending_exit.get(coin)
        if pe and now >= pe["fire_at"]:
            self.pending_exit.pop(coin)
            pos = self.open.pop(coin, None)
            if pos: self._settle(pos, coin, now, lagg, feed, fx, cfg, pe["reason"], leader_fresh)
            return

        pos = self.open.get(coin)
        if pos:
            if pe: return
            cur = lagg.bid if pos["direction"] == "LONG" else lagg.ask
            reason = None
            if pos["direction"] == "LONG":
                if cur >= pos["target"]: reason = "TARGET_HIT"
                elif cur <= pos["stop"]: reason = "STOP_LOSS"
            else:
                if cur <= pos["target"]: reason = "TARGET_HIT"
                elif cur >= pos["stop"]: reason = "STOP_LOSS"
            if not reason and now >= pos["timeout_at"]: reason = "TIMEOUT"
            if reason: self.pending_exit[coin] = {"fire_at": now + self.latency_s, "reason": reason}
            return
        if p: return

        # entry signal
        if abs(adj) <= hurdle: return
        direction = "LONG" if adj > 0 else "SHORT"
        self.signals += 1
        notional = slot_notional_inr
        liq = self._liq(feed, coin, direction, notional, fx, cfg, leader_fresh)
        if liq:
            if liq.state in ("PHANTOM", "NO_DEPTH"):
                self.phantom_skips += 1; return
            fillable_inr = liq.fillable * fx
            if fillable_inr < notional:
                if fillable_inr < cfg.min_order_inr:
                    self.thin_skips += 1; return
                notional = fillable_inr; self.shrunk += 1
                liq = self._liq(feed, coin, direction, notional, fx, cfg, leader_fresh) or liq
            if abs(adj) < self.hurdle(cfg, liq.slippage_pct):
                self.edge_skips += 1; return
            self.slips.append(liq.slippage_pct); self.depths.append(liq.depth_notional * fx)
            if len(self.slips) > 2000: self.slips = self.slips[-2000:]; self.depths = self.depths[-2000:]
        target = leader_inr_mid / (1 + basis / 100.0) if cfg.basis_filter else leader_inr_mid
        self.pending[coin] = {"direction": direction, "notional_inr": notional, "target": target,
                              "signal_ts": now, "fire_at": now + self.latency_s, "adj_pct": adj}

    def _settle(self, pos, coin, now, lagg, feed, fx, cfg, reason, leader_fresh) -> None:
        exit_side = "SHORT" if pos["direction"] == "LONG" else "LONG"
        liq = self._liq(feed, coin, exit_side, pos["notional_inr"], fx, cfg, leader_fresh)
        price = (liq.vwap * fx) if (liq and liq.vwap > 0) else (lagg.bid if pos["direction"] == "LONG" else lagg.ask)
        gross = (price - pos["entry"]) * pos["contracts"] if pos["direction"] == "LONG" else (pos["entry"] - price) * pos["contracts"]
        fees = pos["notional_inr"] * 2 * self.fee_pct / 100.0
        net = gross - fees
        self.trades += 1; self.wins += int(net >= 0)
        self.gross += gross; self.fees += fees; self.net += net
        self.holds.append(now - pos["opened_at"])
        if reason == "TARGET_HIT": self.target_holds.append(now - pos["signal_ts"])
        self.reasons[reason] = self.reasons.get(reason, 0) + 1
        self.equity_curve.append({"t": round(now, 1), "net": round(self.net, 2)})
        if len(self.equity_curve) > 1000: self.equity_curve = self.equity_curve[-1000:]

    def metrics(self, runtime_s: float, feed) -> Dict[str, Any]:
        hours = max(runtime_s / 3600.0, 1e-9)
        return {
            "exchange": self.name, "fee_pct_per_side": self.fee_pct, "exec_latency_s": self.latency_s,
            "runtime_s": round(runtime_s), "signals": self.signals, "trades": self.trades,
            "open": len(self.open), "wins": self.wins,
            "win_rate": round(self.wins / self.trades * 100, 1) if self.trades else None,
            "gross_inr": round(self.gross, 2), "fees_inr": round(self.fees, 2), "net_inr": round(self.net, 2),
            "net_inr_per_hour": round(self.net / hours, 2), "trades_per_hour": round(self.trades / hours, 1),
            "avg_hold_s": round(sum(self.holds) / len(self.holds), 1) if self.holds else None,
            "lag_window_s": {"median": _median(self.lag_windows), "p75": _pct(self.lag_windows, 0.75), "n": len(self.lag_windows)},
            "catch_up_s": {"median": _median(self.target_holds), "p75": _pct(self.target_holds, 0.75), "n": len(self.target_holds)},
            "avg_abs_spread_pct": round(sum(self.spreads_abs) / len(self.spreads_abs), 4) if self.spreads_abs else None,
            "slippage_pct": {"median": _median(self.slips), "p90": _pct(self.slips, 0.9)},
            "depth_10bps_inr": {"median": _median(self.depths)},
            "slippage_ladder": {str(n): {"median": _median(xs), "p90": _pct(xs, 0.9),
                                         "unfillable_pct": round(sum(1 for x in xs if x >= 100) / len(xs) * 100, 1) if xs else None}
                                for n, xs in self.ladder.items()},
            "skips": {"phantom": self.phantom_skips, "thin": self.thin_skips, "edge": self.edge_skips, "shrunk": self.shrunk},
            "exit_reasons": self.reasons,
            "feed": {"connected": feed.connected, "subscribed": feed.subscribed, "msgs_per_s": round(feed.msgs_per_s(), 1),
                     "wire_latency_ms": round(feed.wire_latency_ms) if feed.wire_latency_ms is not None else None,
                     "coins_with_depth": len(feed.depth)},
            "equity_curve": self.equity_curve[-200:],
        }


class ShadowRunner:
    """Owns the second laggard feed and steps both simulated exchanges every 0.4 s."""
    def __init__(self, engine) -> None:
        self.engine = engine
        self.started_at: Optional[float] = None
        self.sims: Dict[str, ShadowExchange] = {}
        self.other_feed = None
        self._task: Optional[asyncio.Task] = None
        self.reset()

    def reset(self) -> None:
        cfg = self.engine.cfg
        self.sims = {
            "coinswitch": ShadowExchange("coinswitch", cfg.coinswitch_taker_fee_pct, cfg.coinswitch_exec_latency_s),
            "coindcx": ShadowExchange("coindcx", cfg.coindcx_taker_fee_pct, cfg.coindcx_exec_latency_s),
        }
        self.started_at = time.time()

    def _other_name(self) -> str:
        return "coindcx" if self.engine.cfg.laggard_exchange == "coinswitch" else "coinswitch"

    def feeds(self) -> Dict[str, Any]:
        return {self.engine.cfg.laggard_exchange: self.engine.coinswitch_feed, self._other_name(): self.other_feed}

    async def start(self) -> None:
        if self._task or self.engine.cfg.simulate: return
        self._task = asyncio.create_task(self._loop())

    async def _ensure_other_feed(self) -> None:
        from bot_engine import make_laggard_feed
        name = self._other_name()
        coins = list(self.engine.cfg.coins)
        f = self.other_feed
        if f is None or f.coins != coins or getattr(f, "_exchange", "") != name:
            if f: f.stop()
            f = make_laggard_feed(name, coins, lambda: self.engine.fx.rate, url=self.engine.cfg.coinswitch_ws_url)
            f._exchange = name
            self.other_feed = f
            await f.start()

    async def _loop(self) -> None:
        try:
            while True:
                try:
                    if self.engine.running and self.engine.live_feed:
                        await self._ensure_other_feed()
                        now = time.time(); fx = max(self.engine.fx.rate, 1e-6); cfg = self.engine.cfg
                        slot = self.engine.allocated_per_slot_inr() * max(1, int(cfg.leverage))
                        for name, feed in self.feeds().items():
                            if not feed: continue
                            sim = self.sims[name]
                            for coin in cfg.coins:
                                leader = self.engine.live_feed.book.get(coin)
                                lagg = feed.fresh(coin, cfg.laggard_stale_after_s)
                                if leader and lagg: sim.step(coin, now, leader, lagg, feed, fx, cfg, slot)
                except Exception as e:  # keep the comparison alive on data hiccups
                    logger.warning(f"shadow step error: {e}")
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            pass

    def report(self) -> Dict[str, Any]:
        runtime = time.time() - (self.started_at or time.time())
        feeds = self.feeds()
        ex = {n: (self.sims[n].metrics(runtime, feeds[n]) if feeds.get(n) else None) for n in EXCHANGES}
        return {"runtime_s": round(runtime), "primary": self.engine.cfg.laggard_exchange, "exchanges": ex,
                "verdict": verdict(ex), "assumptions": {
                    "slot_notional_inr": round(self.engine.allocated_per_slot_inr() * max(1, int(self.engine.cfg.leverage)), 2),
                    "max_slippage_pct": self.engine.cfg.max_slippage_pct, "phantom_frozen_s": self.engine.cfg.phantom_frozen_s,
                    "timeout_s": self.engine.cfg.timeout_seconds, "basis_filter": self.engine.cfg.basis_filter}}


def verdict(ex: Dict[str, Optional[Dict[str, Any]]]) -> Dict[str, Any]:
    cs, dx = ex.get("coinswitch"), ex.get("coindcx")
    if not cs or not dx:
        return {"winner": None, "confidence": "none", "lines": ["Both feeds must be connected for a verdict."]}
    lines: List[str] = []
    def g(m, *path):
        for p in path:
            m = (m or {}).get(p) if isinstance(m, dict) else None
        return m
    lw_cs, lw_dx = g(cs, "lag_window_s", "median"), g(dx, "lag_window_s", "median")
    if lw_cs is not None and lw_dx is not None:
        lines.append(f"Lag window (median): CoinSwitch {lw_cs}s vs CoinDCX {lw_dx}s → {'CoinSwitch wider' if lw_cs > lw_dx else 'CoinDCX wider' if lw_dx > lw_cs else 'equal'} ({abs(lw_cs - lw_dx):.1f}s).")
    sp_cs, sp_dx = cs.get("avg_abs_spread_pct"), dx.get("avg_abs_spread_pct")
    if sp_cs is not None and sp_dx is not None:
        lines.append(f"Mean |divergence| vs Binance: CoinSwitch {sp_cs:.4f}% vs CoinDCX {sp_dx:.4f}% → CoinSwitch lags {sp_cs / max(sp_dx, 1e-9):.1f}× more.")
    cu_cs, cu_dx = g(cs, "catch_up_s", "median"), g(dx, "catch_up_s", "median")
    if cu_cs is not None and cu_dx is not None:
        lines.append(f"Catch-up time signal→target (median): CoinSwitch {cu_cs}s vs CoinDCX {cu_dx}s.")
    sl_cs, sl_dx = g(cs, "slippage_pct", "median"), g(dx, "slippage_pct", "median")
    if sl_cs is not None and sl_dx is not None:
        lines.append(f"Entry slippage at slot size (median): CoinSwitch {sl_cs:.3f}% vs CoinDCX {sl_dx:.3f}% → {'CoinDCX tighter' if sl_dx < sl_cs else 'CoinSwitch tighter'}.")
    dp_cs, dp_dx = g(cs, "depth_10bps_inr", "median"), g(dx, "depth_10bps_inr", "median")
    if dp_cs is not None and dp_dx is not None:
        lines.append(f"Depth within 10 bps (median): CoinSwitch ₹{dp_cs:,.0f} vs CoinDCX ₹{dp_dx:,.0f}.")
    lad = []
    for n in ("10000", "100000", "1000000"):
        a, b = g(cs, "slippage_ladder", n, "median"), g(dx, "slippage_ladder", n, "median")
        if a is not None and b is not None:
            lad.append(f"₹{int(n)/1e5:g}L: {a:.3f}% / {b:.3f}%" if int(n) >= 1e5 else f"₹{int(n)/1e3:g}k: {a:.3f}% / {b:.3f}%")
    if lad: lines.append("Slippage ladder (CoinSwitch / CoinDCX, worst side, median): " + " · ".join(lad) + ".")
    ph_cs, ph_dx = g(cs, "skips", "phantom"), g(dx, "skips", "phantom")
    lines.append(f"Phantom/thin skips: CoinSwitch {ph_cs}+{g(cs,'skips','thin')} vs CoinDCX {ph_dx}+{g(dx,'skips','thin')} of {cs['signals']} / {dx['signals']} signals.")
    lines.append(f"Fees+latency: CoinSwitch {cs['fee_pct_per_side']}%/side, {cs['exec_latency_s']}s exec vs CoinDCX {dx['fee_pct_per_side']}%/side, {dx['exec_latency_s']}s exec.")
    n_cs, n_dx = cs["net_inr_per_hour"], dx["net_inr_per_hour"]
    lines.append(f"Net ₹/hour after fees+slippage: CoinSwitch {n_cs:+.2f} ({cs['trades']} trades, win {cs['win_rate']}%) vs CoinDCX {n_dx:+.2f} ({dx['trades']} trades, win {dx['win_rate']}%).")
    total = cs["trades"] + dx["trades"]
    winner = None if total == 0 else ("coinswitch" if n_cs > n_dx else "coindcx")
    conf = "none" if total == 0 else "low" if total < 20 or cs["runtime_s"] < 1800 else "medium" if total < 60 else "high"
    if winner:
        lines.append(f"→ {'CoinSwitch' if winner == 'coinswitch' else 'CoinDCX'} shows the higher net edge so far "
                     f"({'wider window outweighs thinner book' if winner == 'coinswitch' else 'tighter book + faster fills outweigh the narrower window'}). Confidence: {conf}.")
    return {"winner": winner, "confidence": conf, "lines": lines}
