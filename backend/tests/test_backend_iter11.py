"""Iteration 11 backend tests: CoinDCX user WS + telemetry + lifecycle regression."""
import os
import time
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/") or \
           open("/app/frontend/.env").read().split("REACT_APP_BACKEND_URL=")[1].split("\n")[0].strip()
API = f"{BASE_URL}/api"


# --- Telemetry ---
class TestTelemetry:
    def test_telemetry_shape(self):
        r = requests.get(f"{API}/telemetry", timeout=15)
        assert r.status_code == 200
        j = r.json()
        feed = j.get("feed", {})
        assert feed.get("source") == "COINDCX_WS", f"feed.source={feed.get('source')}"
        assert feed.get("laggard_exchange") == "coindcx"
        uw = feed.get("user_ws")
        assert isinstance(uw, dict), f"user_ws missing: {feed}"
        assert uw.get("connected") is True
        assert uw.get("joined") is True
        assert uw.get("active") is True
        for k in ("order_updates", "position_updates", "fills_via_ws", "fills_via_rest"):
            assert isinstance(uw.get(k), (int, float)), f"{k} not numeric: {uw.get(k)}"
        # last_fill_source may be WS if trades happened
        assert uw.get("last_fill_source") in ("WS", "REST", "", None)


# --- Config & Status ---
class TestConfigStatus:
    def test_config(self):
        r = requests.get(f"{API}/config", timeout=15)
        assert r.status_code == 200
        j = r.json()
        assert j.get("paper_trading") is True
        assert j.get("laggard_exchange") == "coindcx"
        assert j.get("coindcx_client_ready") is True
        assert j.get("num_slots") == 1
        assert j.get("leverage") == 1

    def test_status(self):
        r = requests.get(f"{API}/status", timeout=15)
        assert r.status_code == 200
        j = r.json()
        assert j.get("running") is True
        assert j.get("mode") == "PAPER"


# --- Closed positions ---
class TestClosedPositions:
    def test_closed_positions_have_live_ada_sol(self):
        r = requests.get(f"{API}/positions/closed?limit=20", timeout=15)
        assert r.status_code == 200
        body = r.json()
        items = body.get("items") if isinstance(body, dict) else body
        assert isinstance(items, list), f"items not a list: {body}"
        live = [i for i in items if i.get("mode") == "LIVE"]
        assert len(live) >= 2, f"Expected >=2 LIVE closed positions, got {len(live)}"
        by_coin = {}
        for i in live:
            by_coin.setdefault(i.get("coin"), []).append(i)
        ada_list = by_coin.get("ADA", [])
        sol_list = by_coin.get("SOL", [])
        ada = next((i for i in ada_list if i.get("direction") == "SHORT" and i.get("close_reason") == "TIMEOUT"), None)
        sol = next((i for i in sol_list if i.get("direction") == "LONG" and i.get("close_reason") == "TARGET_HIT"), None)
        assert ada is not None, f"ADA SHORT TIMEOUT LIVE not found; ADA items: {ada_list}"
        assert sol is not None, f"SOL LONG TARGET_HIT LIVE not found; SOL items: {sol_list}"
        assert ada.get("direction") == "SHORT"
        assert ada.get("close_reason") == "TIMEOUT"
        assert ada.get("entry_order_id")
        assert ada.get("exit_order_id")
        assert isinstance(ada.get("net_pnl_inr"), (int, float))
        assert sol.get("direction") == "LONG"
        assert sol.get("close_reason") == "TARGET_HIT"
        assert isinstance(sol.get("net_pnl_inr"), (int, float))


# --- CoinDCX safe endpoints ---
class TestCoinDCX:
    def test_coindcx_check(self):
        r = requests.post(f"{API}/coindcx/check", json={}, timeout=20)
        assert r.status_code == 200
        j = r.json()
        assert j.get("ok") is True
        assert j.get("margin_currency") == "INR"
        assert isinstance(j.get("wallets"), list)

    def test_test_order_without_confirm_returns_400(self):
        # DO NOT include confirm:true
        payload = {"coin": "DOGE", "notional_usdt": 6, "leverage": 2, "confirm": False}
        r = requests.post(f"{API}/coindcx/test-order", json=payload, timeout=15)
        assert r.status_code == 400, f"Expected 400, got {r.status_code}: {r.text}"


# --- Lifecycle regression ---
class TestLifecycle:
    def test_stop_start_user_ws_reconnects(self):
        r = requests.post(f"{API}/bot/stop", timeout=15)
        assert r.status_code == 200
        time.sleep(2)
        r = requests.post(f"{API}/bot/start", timeout=15)
        assert r.status_code == 200
        # wait for user WS to reconnect
        joined = False
        source_ok = False
        for _ in range(20):  # up to ~20s
            time.sleep(1)
            tj = requests.get(f"{API}/telemetry", timeout=10).json()
            feed = tj.get("feed", {})
            uw = feed.get("user_ws") or {}
            if uw.get("joined") is True:
                joined = True
            if feed.get("source") == "COINDCX_WS":
                source_ok = True
            if joined and source_ok:
                break
        assert joined, f"user_ws.joined did not become true after restart; last uw={uw}"
        assert source_ok, f"feed.source not COINDCX_WS after restart; last={feed.get('source')}"

    def test_pause_resume_btc(self):
        r = requests.post(f"{API}/coins/BTC/pause", timeout=10)
        assert r.status_code == 200
        assert r.json().get("ok") is True
        r = requests.post(f"{API}/coins/BTC/resume", timeout=10)
        assert r.status_code == 200
        assert r.json().get("ok") is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
