"""Tick recording (daily JSONL) and offline session replay through the engine's signal rules."""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

IST = timezone(timedelta(hours=5, minutes=30))


class TickRecorder:
    """Buffers ticks and appends them to data/ticks/YYYY-MM-DD.jsonl (IST day); prunes old days."""
    def __init__(self, root: Path, retention_days: int = 7, enabled: bool = True) -> None:
        self.root = root
        self.retention_days = retention_days
        self.enabled = enabled
        self._buf: List[str] = []
        self._task: Optional[asyncio.Task] = None
        self.lines_written = 0
        self.last_flush_ts = 0.0
        self.root.mkdir(parents=True, exist_ok=True)

    def record(self, coin: str, lb: float, la: float, cb: float, ca: float, ts: Optional[float] = None) -> None:
        if not self.enabled: return
        t = ts or time.time()
        self._buf.append(json.dumps({"t": round(t, 3), "c": coin, "lb": lb, "la": la, "cb": cb, "ca": ca},
                                    separators=(",", ":")))

    def _path_for(self, t: float) -> Path:
        return self.root / (datetime.fromtimestamp(t, IST).strftime("%Y-%m-%d") + ".jsonl")

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        if self._task: self._task.cancel()
        self._flush()

    def _flush(self) -> None:
        if not self._buf: return
        buf, self._buf = self._buf, []
        by_day: Dict[Path, List[str]] = {}
        for line in buf:
            by_day.setdefault(self._path_for(json.loads(line)["t"]), []).append(line)
        for p, lines in by_day.items():
            with open(p, "a") as fh: fh.write("\n".join(lines) + "\n")
        self.lines_written += len(buf)
        self.last_flush_ts = time.time()

    def prune(self) -> List[str]:
        cutoff = datetime.now(IST) - timedelta(days=self.retention_days)
        removed = []
        for p in self.root.glob("*.jsonl"):
            try:
                if datetime.strptime(p.stem, "%Y-%m-%d").replace(tzinfo=IST) < cutoff.replace(hour=0, minute=0, second=0, microsecond=0):
                    p.unlink(); removed.append(p.name)
            except ValueError:
                continue
        return removed

    async def _loop(self) -> None:
        try:
            n = 0
            while True:
                await asyncio.sleep(1.0)
                await asyncio.to_thread(self._flush)
                n += 1
                if n % 3600 == 0: await asyncio.to_thread(self.prune)
        except asyncio.CancelledError:
            self._flush()

    def sessions(self) -> List[Dict[str, Any]]:
        out = []
        for p in sorted(self.root.glob("*.jsonl"), reverse=True):
            st = p.stat()
            out.append({"date": p.stem, "size_mb": round(st.st_size / 1e6, 2), "modified": st.st_mtime})
        return out

    def status(self) -> Dict[str, Any]:
        return {"enabled": self.enabled, "lines_written": self.lines_written, "buffered": len(self._buf),
                "retention_days": self.retention_days, "sessions": self.sessions()[:10]}


def iter_ticks(path: Path, max_lines: int = 5_000_000):
    with open(path) as fh:
        for i, line in enumerate(fh):
            if i >= max_lines: break
            try: r = json.loads(line)
            except ValueError: continue
            yield r


def replay_session(path: Path, *, coins: List[str], hurdle_pct: float, basis_filter: bool, basis_window_s: int,
                   leverage: int, num_slots: int, equity_usdt: float, timeout_seconds: int, stop_loss_pct: float,
                   fee_round_trip_pct: float, start_ts: Optional[float] = None, end_ts: Optional[float] = None,
                   max_lines: int = 5_000_000, rows=None) -> Dict[str, Any]:
    """Offline pass of the live signal rules over one recorded day. Fills at top-of-book like PAPER mode.
    `rows` (pre-loaded tick dicts) can be passed to avoid re-reading the file (used by sweeps)."""
    basis_hist: Dict[str, List[tuple]] = {c: [] for c in coins}
    open_pos: Dict[str, Dict[str, Any]] = {}
    trades: List[Dict[str, Any]] = []
    equity = equity_usdt
    curve: List[Dict[str, float]] = []
    last_curve_t = 0.0
    n = 0
    first_t = last_t = None
    lev = max(1, int(leverage))

    def basis_for(coin: str, t: float, gross: float) -> float:
        h = basis_hist[coin]; h.append((t, gross))
        cutoff = t - basis_window_s
        if h and h[0][0] < cutoff:
            basis_hist[coin] = h = [x for x in h if x[0] >= cutoff]
        if len(h) < 20: return 0.0
        v = sorted(x[1] for x in h); return v[len(v) // 2]

    def close(coin: str, px: float, t: float, reason: str) -> None:
        nonlocal equity
        p = open_pos.pop(coin)
        gross = (px - p["entry"]) * p["contracts"] if p["dir"] == "LONG" else (p["entry"] - px) * p["contracts"]
        fees = p["notional"] * fee_round_trip_pct / 100.0
        net = gross - fees
        equity += net
        trades.append({"coin": coin, "direction": p["dir"], "opened_at": p["t"], "closed_at": t, "hold_s": round(t - p["t"], 2),
                       "entry": p["entry"], "exit": px, "gross": round(gross, 4), "fees": round(fees, 4), "net": round(net, 4),
                       "reason": reason, "basis_pct": round(p["basis"], 4)})

    source = rows if rows is not None else iter_ticks(path, max_lines)
    if True:
        for r in source:
            n += 1
            t, coin = r["t"], r["c"]
            if coin not in basis_hist: continue
            if start_ts and t < start_ts: continue
            if end_ts and t > end_ts: break
            first_t = first_t or t; last_t = t
            lb, la, cb, ca = r["lb"], r["la"], r["cb"], r["ca"]
            if not (lb and la and cb and ca): continue
            lagg_mid = (cb + ca) / 2; leader_mid = (lb + la) / 2
            gross_pct = (leader_mid - lagg_mid) / lagg_mid * 100.0
            basis = basis_for(coin, t, gross_pct)
            adj = gross_pct - basis if basis_filter else gross_pct
            p = open_pos.get(coin)
            if p:
                px = cb if p["dir"] == "LONG" else ca
                hit = (px >= p["target"]) if p["dir"] == "LONG" else (px <= p["target"])
                stopped = (px <= p["stop"]) if p["dir"] == "LONG" else (px >= p["stop"])
                if hit: close(coin, px, t, "TARGET_HIT")
                elif stopped: close(coin, px, t, "STOP_LOSS")
                elif t - p["t"] >= timeout_seconds: close(coin, px, t, "TIMEOUT")
            elif len(open_pos) < num_slots and abs(adj) > hurdle_pct:
                d = "LONG" if adj > 0 else "SHORT"
                margin = min(equity / num_slots, max(0.0, equity - sum(x["margin"] for x in open_pos.values())))
                if margin < 50: continue
                entry = ca if d == "LONG" else cb
                notional = margin * lev
                target_ref = leader_mid / (1 + basis / 100.0) if basis_filter else leader_mid
                stop = entry * (1 - stop_loss_pct / 100) if d == "LONG" else entry * (1 + stop_loss_pct / 100)
                open_pos[coin] = {"dir": d, "t": t, "entry": entry, "contracts": notional / entry, "margin": margin,
                                  "notional": notional, "target": target_ref, "stop": stop, "basis": basis}
            if t - last_curve_t >= 5:
                curve.append({"ts": t, "equity": round(equity, 4)}); last_curve_t = t
    for coin in list(open_pos):
        close(coin, open_pos[coin]["entry"], last_t or time.time(), "EOD_FLAT")
    wins = sum(1 for x in trades if x["net"] >= 0)
    per_coin = []
    for c in coins:
        ts_ = [x for x in trades if x["coin"] == c]
        if not ts_: continue
        per_coin.append({"coin": c, "trades": len(ts_), "wins": sum(1 for x in ts_ if x["net"] >= 0),
                         "net": round(sum(x["net"] for x in ts_), 4), "timeouts": sum(1 for x in ts_ if x["reason"] == "TIMEOUT"),
                         "avg_hold_s": round(sum(x["hold_s"] for x in ts_) / len(ts_), 1)})
    return {"file": path.name, "lines": n, "span_s": round((last_t - first_t), 1) if first_t and last_t else 0,
            "params": {"hurdle_pct": hurdle_pct, "basis_filter": basis_filter, "basis_window_s": basis_window_s, "leverage": lev,
                       "num_slots": num_slots, "equity_usdt": equity_usdt, "timeout_seconds": timeout_seconds,
                       "stop_loss_pct": stop_loss_pct, "fee_round_trip_pct": fee_round_trip_pct},
            "trades": len(trades), "wins": wins, "win_rate": round(wins / len(trades) * 100, 1) if trades else 0.0,
            "net_pnl_usdt": round(equity - equity_usdt, 4), "final_equity_usdt": round(equity, 4),
            "return_pct": round((equity / equity_usdt - 1) * 100, 3) if equity_usdt else 0.0,
            "max_drawdown_pct": _max_dd(curve), "per_coin": sorted(per_coin, key=lambda x: x["net"], reverse=True),
            "equity_curve": curve[::max(1, len(curve) // 400)], "last_trades": trades[-50:]}


def _max_dd(curve: List[Dict[str, float]]) -> float:
    peak, dd = None, 0.0
    for p in curve:
        e = p["equity"]; peak = e if peak is None else max(peak, e)
        if peak: dd = max(dd, (peak - e) / peak * 100)
    return round(dd, 3)


def replay_sweep(path: Path, *, hurdles: List[float], basis_windows: List[int], max_lines: int = 1_500_000,
                 **params) -> Dict[str, Any]:
    """Grid of hurdle × basis-window replays over one pre-loaded day."""
    rows = list(iter_ticks(path, max_lines))
    grid = []
    for h in hurdles:
        for w in basis_windows:
            r = replay_session(path, rows=rows, hurdle_pct=h, basis_window_s=w, **params)
            grid.append({"hurdle_pct": h, "basis_window_s": w, "trades": r["trades"], "win_rate": r["win_rate"],
                         "net_pnl_usdt": r["net_pnl_usdt"], "return_pct": r["return_pct"], "max_drawdown_pct": r["max_drawdown_pct"]})
    best = max(grid, key=lambda g: (g["net_pnl_usdt"], -g["max_drawdown_pct"])) if grid else None
    return {"file": path.name, "lines": len(rows), "hurdles": hurdles, "basis_windows": basis_windows,
            "params": {k: v for k, v in params.items() if k != "coins"}, "grid": grid, "best": best}
