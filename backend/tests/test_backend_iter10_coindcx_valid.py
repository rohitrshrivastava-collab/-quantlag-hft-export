"""Iteration 10: CoinDCX check + test-order with VALID INR-margined keys. NEVER send confirm=true."""
import os
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://arbitrage-engine-24.preview.emergentagent.com").rstrip("/")


def _post(path, body=None):
    return requests.post(f"{BASE_URL}{path}", json=body or {}, timeout=30)


# --- /api/coindcx/check with valid keys ---
def test_coindcx_check_ok_inr():
    r = _post("/api/coindcx/check")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok") is True
    assert body.get("margin_currency") == "INR", body
    wallets = body.get("wallets")
    assert isinstance(wallets, list) and len(wallets) >= 1
    currencies = {w.get("currency_short_name"): w for w in wallets}
    assert "INR" in currencies, currencies.keys()
    assert "USDT" in currencies, currencies.keys()
    # balance is numeric string
    for cur in ("INR", "USDT"):
        bal = currencies[cur].get("balance")
        assert isinstance(bal, str)
        float(bal)  # parseable

    positions = body.get("positions")
    assert isinstance(positions, list)
    for p in positions:
        # active_pos must be 0 (flat)
        assert float(p.get("active_pos", 0)) == 0.0, p

    rl = body.get("rate_limiter")
    assert isinstance(rl, dict), rl
    assert "default_per_min" in rl
    assert "buckets" in rl
    assert "http_429_count" in rl


# --- /api/coindcx/test-order safety + validation (NEVER confirm=true with real order intent) ---
def test_test_order_confirm_false_returns_400():
    r = _post("/api/coindcx/test-order", {"coin": "DOGE", "notional_usdt": 6, "leverage": 2, "confirm": False})
    assert r.status_code == 400, r.text
    assert "confirm=true" in r.json().get("detail", "").lower() or "confirm" in r.json().get("detail", "").lower()


def test_test_order_leverage_zero_422():
    r = _post("/api/coindcx/test-order", {"coin": "DOGE", "notional_usdt": 6, "leverage": 0, "confirm": False})
    assert r.status_code == 422, r.text


def test_test_order_leverage_11_422():
    r = _post("/api/coindcx/test-order", {"coin": "DOGE", "notional_usdt": 6, "leverage": 11, "confirm": False})
    assert r.status_code == 422, r.text


def test_test_order_notional_zero_422():
    r = _post("/api/coindcx/test-order", {"coin": "DOGE", "notional_usdt": 0, "leverage": 2, "confirm": False})
    assert r.status_code == 422, r.text


# --- /api/config + telemetry ---
def test_config_coindcx_client_ready():
    r = requests.get(f"{BASE_URL}/api/config", timeout=15)
    assert r.status_code == 200
    body = r.json()
    assert body.get("coindcx_client_ready") is True, body


def test_telemetry_feed_source():
    r = requests.get(f"{BASE_URL}/api/telemetry", timeout=15)
    assert r.status_code == 200
    body = r.json()
    feed = body.get("feed") or {}
    assert feed.get("source") == "COINSWITCH_WS", body


def test_bot_still_in_paper_mode():
    r = requests.get(f"{BASE_URL}/api/status", timeout=15)
    assert r.status_code == 200
    body = r.json()
    assert (
        body.get("mode", "").lower() == "paper"
        or body.get("paper") is True
        or "paper" in str(body).lower()
    ), body
