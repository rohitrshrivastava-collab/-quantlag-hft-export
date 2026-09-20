"""Iteration 9: CoinDCX check + test-order endpoints (keys invalid on exchange side)."""
import os
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://arbitrage-engine-24.preview.emergentagent.com").rstrip("/")


def _post(path, body=None):
    return requests.post(f"{BASE_URL}{path}", json=body or {}, timeout=30)


def test_coindcx_check_returns_424_with_401_hint():
    r = _post("/api/coindcx/check")
    assert r.status_code == 424, r.text
    detail = r.json().get("detail", "")
    assert "CoinDCX API 401" in detail, detail
    assert "401 = key/secret rejected" in detail, detail


def test_test_order_requires_confirm_true():
    r = _post("/api/coindcx/test-order", {"coin": "DOGE", "notional_usdt": 6, "confirm": False})
    assert r.status_code == 400, r.text
    assert "confirm=true" in r.json().get("detail", "")


def test_test_order_confirm_true_returns_424_with_steps():
    r = _post("/api/coindcx/test-order", {"coin": "DOGE", "notional_usdt": 6, "confirm": True})
    assert r.status_code == 424, r.text
    detail = r.json().get("detail")
    assert isinstance(detail, dict), detail
    assert "CoinDCX API 401" in detail.get("message", ""), detail
    steps = detail.get("steps")
    assert isinstance(steps, list) and len(steps) >= 1
    last = steps[-1]
    assert last.get("step") == "error"
    assert last.get("ok") is False


def test_status_still_ok_after_test_order():
    r = requests.get(f"{BASE_URL}/api/status", timeout=15)
    assert r.status_code == 200
    body = r.json()
    # Bot should stay in paper mode
    assert body.get("mode", "").lower() == "paper" or body.get("paper") is True or "paper" in str(body).lower()


def test_notional_validation_too_small():
    r = _post("/api/coindcx/test-order", {"coin": "DOGE", "notional_usdt": 0, "confirm": True})
    assert r.status_code == 422, r.text


def test_notional_validation_too_large():
    r = _post("/api/coindcx/test-order", {"coin": "DOGE", "notional_usdt": 100, "confirm": True})
    assert r.status_code == 422, r.text


def test_logs_contain_test_order_error():
    # Fire a confirm=true attempt first to ensure log line exists
    _post("/api/coindcx/test-order", {"coin": "DOGE", "notional_usdt": 6, "confirm": True})
    r = requests.get(f"{BASE_URL}/api/logs", params={"limit": 50}, timeout=15)
    assert r.status_code == 200
    body = r.json()
    items = body if isinstance(body, list) else body.get("items") or body.get("logs") or []
    text = " | ".join(str(x) for x in items)
    assert "CoinDCX test order failed" in text, text[:2000]
