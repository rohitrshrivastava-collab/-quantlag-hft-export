"""Iter13: /api/laggard runtime pair switch, /api/wallet, entry_fx, status checks."""
import os
import time
import pytest
import requests

BASE = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE:
    # fallback for backend runner env
    with open("/app/frontend/.env") as f:
        for l in f:
            if l.startswith("REACT_APP_BACKEND_URL="):
                BASE = l.split("=", 1)[1].strip().rstrip("/")

API = f"{BASE}/api"


def test_status_live_coindcx():
    r = requests.get(f"{API}/status", timeout=15)
    assert r.status_code == 200
    d = r.json()
    assert d["leverage"] == 10
    assert d["num_slots"] == 10
    assert d["laggard_exchange"] == "coindcx"
    assert d["mode"] == "LIVE"
    assert d["running"] is True


def test_wallet_shape_and_cache():
    r = requests.get(f"{API}/wallet", timeout=20)
    assert r.status_code == 200
    d = r.json()
    assert d["exchange"] == "coindcx"
    assert d["keys"] is True
    assert d["error"] is None
    assert d["margin_currency"] == "INR"
    bals = d["balances"]
    assert isinstance(bals, list) and len(bals) >= 2
    by_ccy = {b["currency"]: b for b in bals}
    assert "INR" in by_ccy and "USDT" in by_ccy
    assert by_ccy["INR"]["balance"] > 0
    assert by_ccy["INR"]["locked"] >= 0
    ts1 = d["fetched_at"]

    # cache within 10s
    time.sleep(1)
    r2 = requests.get(f"{API}/wallet", timeout=20).json()
    assert r2["fetched_at"] == ts1, "wallet should be cached < 10s"

    # force refresh
    r3 = requests.get(f"{API}/wallet?force=true", timeout=20).json()
    assert r3["fetched_at"] != ts1, "force=true should refresh fetched_at"


def test_laggard_invalid_400():
    r = requests.post(f"{API}/laggard", json={"exchange": "foo"}, timeout=15)
    assert r.status_code == 400


def test_laggard_current_noop():
    r = requests.post(f"{API}/laggard", json={"exchange": "coindcx"}, timeout=15)
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["laggard_exchange"] == "coindcx"


def test_laggard_round_trip_or_guarded():
    # only round-trip if no open positions
    op = requests.get(f"{API}/positions/open", timeout=15).json()
    items = op if isinstance(op, list) else op.get("items", op.get("positions", []))
    n_open = len(items) if isinstance(items, list) else 0

    if n_open > 0:
        r = requests.post(f"{API}/laggard", json={"exchange": "coinswitch"}, timeout=15)
        assert r.status_code == 409, f"expected 409 with {n_open} open, got {r.status_code}"
        return

    # switch to coinswitch
    r = requests.post(f"{API}/laggard", json={"exchange": "coinswitch"}, timeout=20)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["laggard_exchange"] == "coinswitch"
    assert d.get("warning"), "expected warning about missing CoinSwitch keys"

    try:
        time.sleep(6)
        tel = requests.get(f"{API}/telemetry", timeout=15).json()
        feed = tel.get("feed", {})
        assert feed.get("source") == "COINSWITCH_WS", f"feed.source={feed.get('source')}"
        assert feed.get("laggard_exchange") == "coinswitch"

        w = requests.get(f"{API}/wallet", timeout=20).json()
        assert w["exchange"] == "coinswitch"
        assert w["keys"] is False
    finally:
        # ALWAYS switch back to coindcx
        r2 = requests.post(f"{API}/laggard", json={"exchange": "coindcx"}, timeout=20)
        assert r2.status_code == 200
        assert r2.json()["laggard_exchange"] == "coindcx"

    # give it up to 8s
    ok = False
    for _ in range(8):
        time.sleep(1)
        tel = requests.get(f"{API}/telemetry", timeout=15).json()
        feed = tel.get("feed", {})
        if feed.get("source") == "COINDCX_WS" and feed.get("user_ws", {}).get("joined") is True:
            ok = True
            break
    assert ok, f"feed did not return to COINDCX_WS with user_ws joined; last={feed}"

    s = requests.get(f"{API}/status", timeout=15).json()
    assert s["laggard_exchange"] == "coindcx"
    assert s["running"] is True
    assert s["mode"] == "LIVE"


def test_positions_closed_has_entry_fx():
    r = requests.get(f"{API}/positions/closed?limit=5", timeout=15)
    assert r.status_code == 200
    body = r.json()
    items = body if isinstance(body, list) else body.get("items", body.get("positions", []))
    if not items:
        pytest.skip("no closed positions yet")
    for p in items:
        assert "entry_fx" in p, f"missing entry_fx in {list(p.keys())}"
        assert isinstance(p["entry_fx"], (int, float))


def test_config_present():
    r = requests.get(f"{API}/config", timeout=15)
    assert r.status_code == 200
    c = r.json()
    for f in ("laggard_exchange", "leverage", "num_slots", "paper_trading"):
        assert f in c
