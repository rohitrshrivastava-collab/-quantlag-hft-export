"""Runtime settings persistence (JSON) so pauses / slots / leverage / equity survive restarts."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict

CFG_KEYS = ("num_slots", "leverage", "starting_capital_inr", "paper_trading", "basis_filter", "basis_window_s",
            "entry_taker_fee_pct", "exit_taker_fee_pct", "slippage_buffer_pct", "hurdle_extra_pct",
            "timeout_seconds", "stop_loss_pct", "coins")
STATE_KEYS = ("equity_inr", "realized_pnl_inr", "trade_count", "win_count", "loss_count",
              "total_hold_sec", "total_exec_latency_ms")


def load_settings(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def apply_settings(engine, data: Dict[str, Any]) -> Dict[str, Any]:
    applied: Dict[str, Any] = {}
    for k in CFG_KEYS:
        if k in data.get("config", {}):
            setattr(engine.cfg, k, data["config"][k]); applied[k] = data["config"][k]
    for k in STATE_KEYS:
        if k in data.get("state", {}):
            setattr(engine, k, data["state"][k]); applied[k] = data["state"][k]
    engine.paused = set(c for c in data.get("paused", []) if c in engine.cfg.coins)
    if engine.paused: applied["paused"] = sorted(engine.paused)
    return applied


def save_settings(engine, path: Path) -> None:
    data = {
        "saved_at": time.time(),
        "config": {k: getattr(engine.cfg, k) for k in CFG_KEYS},
        "state": {k: getattr(engine, k) for k in STATE_KEYS},
        "paused": sorted(engine.paused),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(path)
