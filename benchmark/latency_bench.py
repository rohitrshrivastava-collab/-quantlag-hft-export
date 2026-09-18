#!/usr/bin/env python3
"""Cross-exchange latency benchmark — Binance Futures vs CoinSwitch PRO Futures vs CoinDCX Futures.

Runs, concurrently for `duration` seconds:
  * TCP-connect "pings" (1 RTT to each exchange edge, every second)  -> p50 / p99
  * WebSocket tick-to-receipt latency  (exchange event ts  ->  local receipt) -> p50 / p99
  * WS stability: msgs/s, max inter-message gap, reconnects
  * Book quality: median top-of-book spread (bps) and depth within ±10 bps (USDT notional)
  * Laggard divergence: |laggard_mid / binance_mid - 1| in bps at every laggard tick
    (quantile curve is stored so a hurdle can be applied afterwards)

Usage (EC2 / VS Code):
    pip install -r benchmark/requirements.txt
    python benchmark/latency_bench.py --duration 60 --symbols BTC,ETH,SOL --json out.json

Only `websockets` is required. Clock: keep the host NTP-synced (chrony on EC2) — tick-to-receipt
uses exchange timestamps and is meaningless with a skewed clock.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import platform
import socket
import statistics
import time
from typing import Any, Callable, Dict, List, Optional

import websockets

EXCHANGES: Dict[str, Dict[str, str]] = {
    "binance":    {"label": "Binance Futures (LEAD)",     "ws_host": "fstream.binance.com", "role": "leader"},
    "coinswitch": {"label": "CoinSwitch PRO Futures (LAG)", "ws_host": "ws.coinswitch.co",  "role": "laggard"},
    "coindcx":    {"label": "CoinDCX Futures (LAG)",       "ws_host": "stream.coindcx.com", "role": "laggard"},
}
DEPTH_BPS = 10.0


def pct(vals: List[float], p: float) -> Optional[float]:
    if not vals: return None
    s = sorted(vals)
    k = (len(s) - 1) * p / 100.0
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def summarize(vals: List[float]) -> Dict[str, Any]:
    if not vals:
        return {"n": 0, "p50": None, "p90": None, "p99": None, "mean": None, "min": None, "max": None}
    return {"n": len(vals), "p50": round(pct(vals, 50), 1), "p90": round(pct(vals, 90), 1),
            "p99": round(pct(vals, 99), 1), "mean": round(statistics.fmean(vals), 1),
            "min": round(min(vals), 1), "max": round(max(vals), 1)}


def quantile_curve(vals: List[float], steps: int = 100) -> List[float]:
    if not vals: return []
    return [round(pct(vals, i * 100.0 / steps), 3) for i in range(steps + 1)]


class ExchangeStats:
    def __init__(self, name: str) -> None:
        self.name = name
        self.tcp_rtt_ms: List[float] = []
        self.tick_lat_ms: List[float] = []
        self.gaps_ms: List[float] = []
        self.spread_bps: List[float] = []
        self.depth_usdt: List[float] = []
        self.dev_bps: List[float] = []            # laggard vs leader mid divergence
        self.msgs = 0
        self.reconnects = 0
        self.errors: List[str] = []
        self.last_msg_ts = 0.0
        self.mid: Dict[str, float] = {}           # symbol -> mid (USDT)
        self.books: Dict[str, Dict[str, Dict[float, float]]] = {}  # sym -> {"bids":{px:qty}, "asks":{px:qty}}

    def on_msg(self) -> None:
        now = time.time()
        if self.last_msg_ts:
            self.gaps_ms.append((now - self.last_msg_ts) * 1000.0)
        self.last_msg_ts = now
        self.msgs += 1

    def on_tick(self, exch_ts_ms: float) -> None:
        lat = time.time() * 1000.0 - exch_ts_ms
        if -5000 < lat < 60000:
            self.tick_lat_ms.append(lat)

    def on_book(self, sym: str, bids: List[tuple], asks: List[tuple]) -> None:
        """bids/asks: list of (price, qty) — best first."""
        if not bids or not asks: return
        bb, ba = bids[0][0], asks[0][0]
        if bb <= 0 or ba <= 0 or ba < bb: return
        mid = (bb + ba) / 2.0
        self.mid[sym] = mid
        self.spread_bps.append((ba - bb) / mid * 1e4)
        lim = mid * DEPTH_BPS / 1e4
        depth = sum(p * q for p, q in bids if p >= bb - lim) + sum(p * q for p, q in asks if p <= ba + lim)
        self.depth_usdt.append(depth)

    def result(self, duration: float) -> Dict[str, Any]:
        return {
            "label": EXCHANGES[self.name]["label"],
            "role": EXCHANGES[self.name]["role"],
            "tcp_rtt_ms": summarize(self.tcp_rtt_ms),
            "ws_tick_to_receipt_ms": summarize(self.tick_lat_ms),
            "ws_msgs": self.msgs,
            "ws_msgs_per_s": round(self.msgs / max(duration, 1e-9), 2),
            "ws_max_gap_ms": round(max(self.gaps_ms), 0) if self.gaps_ms else None,
            "ws_reconnects": self.reconnects,
            "ws_errors": self.errors[-5:],
            "spread_bps_median": round(statistics.median(self.spread_bps), 3) if self.spread_bps else None,
            "depth_10bps_usdt_median": round(statistics.median(self.depth_usdt), 0) if self.depth_usdt else None,
            "divergence_bps": summarize(self.dev_bps) if self.dev_bps else None,
            "divergence_quantiles": quantile_curve(self.dev_bps),
        }


class LatencyBenchmark:
    def __init__(self, duration_s: float = 60.0, symbols: Optional[List[str]] = None,
                 exchanges: Optional[List[str]] = None) -> None:
        self.duration = float(duration_s)
        self.symbols = [s.upper() for s in (symbols or ["BTC", "ETH", "SOL"])]
        self.names = exchanges or list(EXCHANGES)
        self.stats = {n: ExchangeStats(n) for n in self.names}
        self.started_at = 0.0
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------ pings
    async def _pinger(self, name: str) -> None:
        host = EXCHANGES[name]["ws_host"]
        st = self.stats[name]
        while not self._stop.is_set():
            t0 = time.perf_counter()
            try:
                r, w = await asyncio.wait_for(asyncio.open_connection(host, 443), timeout=5)
                st.tcp_rtt_ms.append((time.perf_counter() - t0) * 1000.0)
                w.close()
                try: await w.wait_closed()
                except Exception: pass
            except Exception as e:
                st.errors.append(f"tcp: {e.__class__.__name__}")
            await asyncio.sleep(1.0)

    # ------------------------------------------------------------ ws helpers
    def _leader_mid(self, sym: str) -> Optional[float]:
        return self.stats["binance"].mid.get(sym) if "binance" in self.stats else None

    def _record_divergence(self, name: str, sym: str) -> None:
        lm = self._leader_mid(sym); m = self.stats[name].mid.get(sym)
        if lm and m:
            self.stats[name].dev_bps.append(abs(m / lm - 1.0) * 1e4)

    async def _ws_loop(self, name: str, runner: Callable) -> None:
        st = self.stats[name]
        backoff = 1.0
        first = True
        while not self._stop.is_set():
            try:
                if not first: st.reconnects += 1
                first = False
                await runner(st)
                backoff = 1.0
            except asyncio.CancelledError:
                return
            except Exception as e:
                st.errors.append(f"ws: {str(e)[:80] or e.__class__.__name__}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10)

    async def _recv_until_stop(self, ws, handler: Callable[[str], Any]) -> None:
        while not self._stop.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if isinstance(raw, bytes): raw = raw.decode()
            await handler(raw)

    # ---------------------------------------------------------------- binance
    async def _binance(self, st: ExchangeStats) -> None:
        streams = "/".join(f"{s.lower()}usdt@bookTicker" for s in self.symbols) + "/" + \
                  "/".join(f"{s.lower()}usdt@depth5@100ms" for s in self.symbols)
        url = f"wss://fstream.binance.com/stream?streams={streams}"
        async with websockets.connect(url, ping_interval=20, max_size=2**22) as ws:
            async def handle(raw: str) -> None:
                st.on_msg()
                m = json.loads(raw); d = m.get("data", {}); stream = m.get("stream", "")
                sym = d.get("s", "")[:-4]
                if "bookTicker" in stream:
                    st.on_tick(float(d["E"]))
                    bb, ba = float(d["b"]), float(d["a"])
                    if sym in self.symbols and bb > 0 and ba > 0:
                        st.mid[sym] = (bb + ba) / 2.0
                elif "depth" in stream:
                    bids = [(float(p), float(q)) for p, q in d.get("b", [])]
                    asks = [(float(p), float(q)) for p, q in d.get("a", [])]
                    st.on_book(sym, bids, asks)
            await self._recv_until_stop(ws, handle)

    # -------------------------------------------------------------- coinswitch
    async def _coinswitch(self, st: ExchangeStats) -> None:
        url = "wss://ws.coinswitch.co/pro/realtime-rates-socket/futures/exchange_2/?EIO=4&transport=websocket"
        ns = "/exchange_2"
        async with websockets.connect(url, ping_interval=None, max_size=2**22) as ws:
            async def handle(raw: str) -> None:
                if raw.startswith("0"):
                    await ws.send(f"40{ns},")
                elif raw == "2":
                    await ws.send("3")
                elif raw.startswith(f"40{ns}"):
                    for s in self.symbols:
                        await ws.send(f'42{ns},' + json.dumps(["FETCH_ORDER_BOOK_CS_PRO", {"event": "subscribe", "pair": f"{s}USDT"}]))
                elif raw.startswith(f"42{ns},"):
                    evt = json.loads(raw.split(",", 1)[1])
                    d = evt[1] if len(evt) > 1 and isinstance(evt[1], dict) else {}
                    if "bids" not in d: return
                    st.on_msg()
                    st.on_tick(float(d.get("timestamp") or 0))
                    sym = str(d.get("symbol") or d.get("s") or "")[:-4]
                    bids = [(float(p), float(q)) for p, q in d.get("bids", [])]
                    asks = [(float(p), float(q)) for p, q in d.get("asks", [])]
                    st.on_book(sym, bids, asks)
                    self._record_divergence("coinswitch", sym)
            await self._recv_until_stop(ws, handle)

    # ----------------------------------------------------------------- coindcx
    async def _coindcx(self, st: ExchangeStats) -> None:
        url = "wss://stream.coindcx.com/socket.io/?EIO=3&transport=websocket"
        async with websockets.connect(url, ping_interval=None, max_size=2**22) as ws:
            async def pinger() -> None:
                while not self._stop.is_set():
                    await asyncio.sleep(20)
                    await ws.send("2")
            ping_task = asyncio.create_task(pinger())

            def apply(sym: str, side: str, levels: Dict[str, str], snapshot: bool) -> None:
                book = st.books.setdefault(sym, {"bids": {}, "asks": {}})
                if snapshot: book[side] = {}
                for p, q in levels.items():
                    fp, fq = float(p), float(q)
                    if fq <= 0: book[side].pop(fp, None)
                    else: book[side][fp] = fq

            async def handle(raw: str) -> None:
                if raw == "40":
                    for s in self.symbols:
                        await ws.send('42' + json.dumps(["join", {"channelName": f"B-{s}_USDT@orderbook@20-futures"}]))
                    return
                if raw == "3" or not raw.startswith("42"): return
                evt = json.loads(raw[2:]); name = evt[0]
                if name not in ("depth-update", "depth-snapshot"): return
                payload = evt[1].get("data") if isinstance(evt[1], dict) else None
                d = json.loads(payload) if isinstance(payload, str) else (payload or {})
                st.on_msg()
                st.on_tick(float(d.get("ts") or 0))
                sym = str(d.get("s", "")).replace("B-", "").replace("_USDT", "").replace("USDT", "")
                apply(sym, "bids", d.get("bids", {}), name == "depth-snapshot")
                apply(sym, "asks", d.get("asks", {}), name == "depth-snapshot")
                book = st.books[sym]
                bids = sorted(book["bids"].items(), reverse=True)[:20]
                asks = sorted(book["asks"].items())[:20]
                st.on_book(sym, bids, asks)
                self._record_divergence("coindcx", sym)
            try:
                await self._recv_until_stop(ws, handle)
            finally:
                ping_task.cancel()

    # -------------------------------------------------------------------- run
    def partial(self) -> Dict[str, Any]:
        elapsed = (time.time() - self.started_at) if self.started_at else 0.0
        return {"elapsed_s": round(elapsed, 1), "duration_s": self.duration,
                "exchanges": {n: s.result(max(elapsed, 1e-9)) for n, s in self.stats.items()}}

    async def run(self) -> Dict[str, Any]:
        runners = {"binance": self._binance, "coinswitch": self._coinswitch, "coindcx": self._coindcx}
        self.started_at = time.time()
        tasks = []
        for n in self.names:
            tasks.append(asyncio.create_task(self._pinger(n)))
            tasks.append(asyncio.create_task(self._ws_loop(n, runners[n])))
        try:
            await asyncio.sleep(self.duration)
        finally:
            self._stop.set()
            for t in tasks: t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return {
            "started_at": self.started_at,
            "finished_at": time.time(),
            "duration_s": self.duration,
            "symbols": self.symbols,
            "host": {"hostname": socket.gethostname(), "platform": platform.platform()},
            "exchanges": {n: s.result(self.duration) for n, s in self.stats.items()},
        }


def format_table(result: Dict[str, Any]) -> str:
    hdr = f"{'exchange':<28}{'tcp p50':>9}{'tcp p99':>9}{'tick p50':>10}{'tick p99':>10}{'msg/s':>8}{'maxgap':>9}{'rc':>4}{'spr bps':>9}{'dep10bps$':>11}{'dev p50':>9}{'dev p99':>9}"
    lines = [hdr, "-" * len(hdr)]
    for n, r in result["exchanges"].items():
        t, w, dv = r["tcp_rtt_ms"], r["ws_tick_to_receipt_ms"], r.get("divergence_bps") or {}
        f = lambda v, d=1: "—" if v is None else (f"{v:.{d}f}")
        lines.append(f"{r['label']:<28}{f(t['p50']):>9}{f(t['p99']):>9}{f(w['p50']):>10}{f(w['p99']):>10}"
                     f"{r['ws_msgs_per_s']:>8.1f}{f(r['ws_max_gap_ms'],0):>9}{r['ws_reconnects']:>4}"
                     f"{f(r['spread_bps_median'],2):>9}{f(r['depth_10bps_usdt_median'],0):>11}{f(dv.get('p50'),2):>9}{f(dv.get('p99'),2):>9}")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--duration", type=float, default=60, help="seconds per run (default 60)")
    ap.add_argument("--symbols", default="BTC,ETH,SOL", help="comma-separated base assets")
    ap.add_argument("--json", dest="json_out", default="", help="write full result JSON to this path")
    a = ap.parse_args()
    bench = LatencyBenchmark(a.duration, a.symbols.split(","))
    print(f"Benchmarking {', '.join(bench.symbols)} for {a.duration:.0f}s against {', '.join(bench.names)} …")
    result = asyncio.run(bench.run())
    print(format_table(result))
    if a.json_out:
        with open(a.json_out, "w") as fh: json.dump(result, fh, indent=2)
        print(f"\nwrote {a.json_out}")


if __name__ == "__main__":
    main()
