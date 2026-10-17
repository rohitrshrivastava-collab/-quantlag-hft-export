"""Order-book liquidity assessment: walk-the-book VWAP, slippage, resting depth, frozen-book (phantom) detection.
Units are native USDT throughout — feeds store USDT and callers pass USDT notional directly, no conversion."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Tuple

Level = Tuple[float, float]   # (price, qty) — best level first


@dataclass
class Liquidity:
    state: str                 # LIQUID | THIN | PHANTOM | NO_DEPTH
    top: float
    vwap: float                # expected average fill for the requested notional
    slippage_pct: float        # |vwap - top| / top × 100 (one side)
    fillable: float            # notional fillable while VWAP stays within max_slip_pct of top
    depth_notional: float      # notional resting within depth_bps of top
    levels: int
    frozen_s: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def walk(levels: List[Level], notional: float) -> Tuple[float, float]:
    """Consume `notional` along best-first levels → (vwap, filled_notional)."""
    filled = qty = 0.0
    for p, q in levels:
        take = min(q * p, notional - filled)
        if take <= 0 or p <= 0: break
        qty += take / p; filled += take
        if filled >= notional - 1e-12: break
    return (filled / qty if qty else 0.0), filled


def fillable_within(levels: List[Level], max_slip_pct: float) -> float:
    """Largest notional whose VWAP stays within max_slip_pct of the best level."""
    if not levels: return 0.0
    top = levels[0][0]
    sign = 1.0 if (len(levels) < 2 or levels[1][0] >= top) else -1.0   # asks ascend, bids descend
    limit = top * (1 + sign * max_slip_pct / 100.0)
    cum_n = cum_q = 0.0
    for p, q in levels:
        n = p * q
        new_vwap = (cum_n + n) / (cum_q + q) if (cum_q + q) > 0 else p
        if sign * (new_vwap - limit) <= 0:
            cum_n += n; cum_q += q
            continue
        denom = 1 - limit / p
        x = (limit * cum_q - cum_n) / denom if abs(denom) > 1e-12 else 0.0
        return cum_n + max(0.0, min(x, n))
    return cum_n


def depth_within(levels: List[Level], bps: float) -> float:
    if not levels: return 0.0
    top = levels[0][0]
    return sum(p * q for p, q in levels if abs(p - top) / top * 1e4 <= bps)


def assess(levels: List[Level], notional: float, max_slip_pct: float, depth_bps: float,
           frozen_s: float, phantom_after_s: float) -> Liquidity:
    if not levels:
        return Liquidity("NO_DEPTH", 0.0, 0.0, 0.0, 0.0, 0.0, 0, frozen_s)
    top = levels[0][0]
    vwap, filled = walk(levels, notional)
    slip = abs(vwap - top) / top * 100.0 if filled > 0 else 0.0
    fillable = fillable_within(levels, max_slip_pct)
    depth = depth_within(levels, depth_bps)
    if frozen_s > phantom_after_s: state = "PHANTOM"
    elif fillable < notional - 1e-9 or filled < notional - 1e-9: state = "THIN"
    else: state = "LIQUID"
    return Liquidity(state, top, vwap, round(slip, 5), fillable, depth, len(levels), round(frozen_s, 2))
