"""Pair-selection verdict framework — applies decision rules to a latency benchmark result.

Rules (see docs/EXCHANGE_PAIR_DECISION.md):
  Gate 1  Feed reliability : msgs>0, reconnects==0, max gap < 5 s, tick p99 < 1500 ms
  Gate 2  Execution budget : est. order RTT (2×TCP p50 + 60 ms) < 500 ms
  Score   net edge 40 · fees 20 · latency 20 · book quality 10 · stability 10
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

PAIR_META = {
    "coinswitch": {
        "pair": "Pair A", "label": "Binance Futures → CoinSwitch PRO Futures",
        "default_taker_fee_pct": 0.05,
        "order_rate_limit": "20 req / 60 s per private endpoint (HTTP 429 beyond)",
        "book_stream": "Socket.IO v4 full-snapshot order book (~5 snapshots/s per symbol)",
    },
    "coindcx": {
        "pair": "Pair B", "label": "Binance Futures → CoinDCX Futures",
        "default_taker_fee_pct": 0.075,
        "order_rate_limit": "16 req/s · 960 req/min general; orders 2000 / 60 s",
        "book_stream": "Socket.IO v2 (EIO=3) depth-snapshot + depth-update deltas (20/50 levels)",
    },
}
GATE_MAX_GAP_MS = 5000
GATE_TICK_P99_MS = 1500
GATE_EXEC_RTT_MS = 500


def _interp_share_above(quantiles: List[float], threshold: float) -> float:
    """Share of samples with value >= threshold, from a 0..100% quantile curve."""
    if not quantiles: return 0.0
    n = len(quantiles) - 1
    for i, q in enumerate(quantiles):
        if q >= threshold:
            if i == 0: return 1.0
            prev = quantiles[i - 1]
            frac = (threshold - prev) / (q - prev) if q > prev else 0.0
            return max(0.0, 1.0 - (i - 1 + frac) / n)
    return 0.0


def _mean_excess(quantiles: List[float], threshold: float) -> float:
    """Average (value - threshold) over samples above threshold (bps)."""
    above = [q - threshold for q in quantiles if q >= threshold]
    return sum(above) / len(above) if above else 0.0


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def evaluate(result: Dict[str, Any], *, slippage_pct: float, extra_pct: float,
             fees_pct: Optional[Dict[str, float]] = None, slot_inr: float = 400.0,
             fx: float = 85.0) -> Dict[str, Any]:
    ex = result.get("exchanges", {})
    fees_pct = fees_pct or {}
    rows: List[Dict[str, Any]] = []
    for name, meta in PAIR_META.items():
        r = ex.get(name)
        if not r: continue
        fee = float(fees_pct.get(name, meta["default_taker_fee_pct"]))
        hurdle_pct = 2 * fee + slippage_pct + extra_pct
        hurdle_bps = hurdle_pct * 100.0
        tcp, tick = r["tcp_rtt_ms"], r["ws_tick_to_receipt_ms"]
        exec_rtt = (2 * (tcp["p50"] or 0) + 60) if tcp["p50"] is not None else None
        gate_feed = bool(r["ws_msgs"] and r["ws_reconnects"] == 0 and (r["ws_max_gap_ms"] or 0) < GATE_MAX_GAP_MS
                         and tick["p99"] is not None and tick["p99"] < GATE_TICK_P99_MS)
        gate_exec = exec_rtt is not None and exec_rtt < GATE_EXEC_RTT_MS
        q = r.get("divergence_quantiles") or []
        opp_share = _interp_share_above(q, hurdle_bps)
        excess_bps = _mean_excess(q, hurdle_bps)
        edge_bps_per_tick = opp_share * excess_bps           # expected net bps captured per laggard tick
        ticks_per_s = r["ws_msgs_per_s"]
        opp_per_min = opp_share * ticks_per_s * 60.0
        net_edge_inr_per_opp = slot_inr * excess_bps / 1e4
        reasons: List[str] = []
        if not gate_feed: reasons.append("feed gate failed (gap/reconnect/p99)")
        if not gate_exec: reasons.append("execution RTT budget failed")
        spread = r["spread_bps_median"] or 0.0
        slip_bps = slippage_pct * 100.0
        if spread > 2 * slip_bps: reasons.append(f"spread {spread:.1f} bps > 2× slippage buffer")
        depth = r["depth_10bps_usdt_median"] or 0.0
        slot_usdt = slot_inr / max(fx, 1e-6)
        if depth < 50 * slot_usdt: reasons.append("thin book (<50× slot within 10 bps)")
        rows.append({
            "exchange": name, "pair": meta["pair"], "label": meta["label"],
            "taker_fee_pct": fee, "hurdle_pct": round(hurdle_pct, 4), "hurdle_bps": round(hurdle_bps, 2),
            "exec_rtt_est_ms": None if exec_rtt is None else round(exec_rtt, 0),
            "tick_p50_ms": tick["p50"], "tick_p99_ms": tick["p99"],
            "gate_feed": gate_feed, "gate_exec": gate_exec, "qualified": gate_feed and gate_exec,
            "opportunity_share": round(opp_share, 4), "opportunities_per_min": round(opp_per_min, 2),
            "mean_excess_bps": round(excess_bps, 2), "edge_bps_per_tick": round(edge_bps_per_tick, 4),
            "net_edge_inr_per_opportunity": round(net_edge_inr_per_opp, 3),
            "expected_inr_per_hour": round(opp_per_min * 60 * net_edge_inr_per_opp, 2),
            "spread_bps": r["spread_bps_median"], "depth_10bps_usdt": r["depth_10bps_usdt_median"],
            "flags": reasons, "order_rate_limit": meta["order_rate_limit"], "book_stream": meta["book_stream"],
        })
    if not rows:
        return {"winner": None, "rows": [], "note": "no laggard data in benchmark result"}
    max_edge = max((x["edge_bps_per_tick"] for x in rows), default=0.0) or 1e-9
    min_fee = min(x["taker_fee_pct"] for x in rows)
    for x in rows:
        s_edge = 40 * _clamp(x["edge_bps_per_tick"] / max_edge)
        s_fee = 20 * _clamp(min_fee / max(x["taker_fee_pct"], 1e-9))
        p99 = x["tick_p99_ms"] or GATE_TICK_P99_MS
        s_lat = 20 * _clamp((GATE_TICK_P99_MS - p99) / GATE_TICK_P99_MS)
        spread = x["spread_bps"] or 0.0
        s_book = 10 * _clamp(1 - spread / max(2 * slippage_pct * 100.0, 1e-9))
        s_stab = 10 if x["gate_feed"] else 0
        x["score"] = {"net_edge": round(s_edge, 1), "fees": round(s_fee, 1), "latency": round(s_lat, 1),
                      "book": round(s_book, 1), "stability": s_stab,
                      "total": round(s_edge + s_fee + s_lat + s_book + s_stab, 1)}
    qualified = [x for x in rows if x["qualified"]] or rows
    ranked = sorted(qualified, key=lambda x: x["score"]["total"], reverse=True)
    winner = ranked[0]
    margin = (ranked[0]["score"]["total"] - ranked[1]["score"]["total"]) if len(ranked) > 1 else None
    confidence = "LOW — rerun 300 s at 2–3 different hours" if margin is not None and margin < 5 else \
                 ("MEDIUM — confirm with a 300 s run" if result.get("duration_s", 0) < 120 else "HIGH")
    return {
        "winner": winner["exchange"], "winner_label": winner["label"], "winner_pair": winner["pair"],
        "margin": None if margin is None else round(margin, 1), "confidence": confidence,
        "any_qualified": any(x["qualified"] for x in rows),
        "rows": sorted(rows, key=lambda x: x["score"]["total"], reverse=True),
        "inputs": {"slippage_pct": slippage_pct, "extra_pct": extra_pct, "slot_inr": slot_inr, "fx": fx,
                   "duration_s": result.get("duration_s"), "symbols": result.get("symbols")},
    }
