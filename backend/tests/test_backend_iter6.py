"""Iteration 6 backend tests: account controls, basis filter, analytics, digest, regression."""
import os
import time
import pytest
import requests

def _load_env():
    p = "/app/frontend/.env"
    if os.path.exists(p):
        for line in open(p):
            if line.startswith("REACT_APP_BACKEND_URL="):
                return line.split("=", 1)[1].strip()
    return os.environ.get("REACT_APP_BACKEND_URL", "")

BASE_URL = (os.environ.get("REACT_APP_BACKEND_URL") or _load_env()).rstrip("/")
API = f"{BASE_URL}/api"


@pytest.fixture(scope="module")
def sess():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


# ---- Status shape ----
def test_status_has_new_fields(sess):
    r = sess.get(f"{API}/status", timeout=15)
    assert r.status_code == 200
    d = r.json()
    for k in ["leverage", "laggard_exchange", "basis_filter", "open_positions", "num_slots"]:
        assert k in d, f"missing {k}"
    assert d["laggard_exchange"] == "coinswitch"
    assert isinstance(d["basis_filter"], bool)
    assert isinstance(d["open_positions"], int)


# ---- Account controls ----
def _close_all_open(sess):
    r = sess.get(f"{API}/positions/open", timeout=15)
    if r.status_code != 200:
        return
    items = r.json().get("items", [])
    for it in items:
        coin = it.get("coin")
        if coin:
            sess.post(f"{API}/positions/{coin}/close", timeout=15)
    if items:
        time.sleep(2)


def test_account_slots_and_leverage_update(sess):
    r = sess.post(f"{API}/account", json={"num_slots": 5, "leverage": 3}, timeout=15)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d.get("ok") is True
    ch = d.get("changes", {})
    assert ch.get("num_slots") == 5
    assert ch.get("leverage") == 3
    assert d.get("leverage_errors", []) == []
    st = d.get("status", {})
    equity = st.get("equity_inr")
    aps = st.get("allocated_per_slot_inr")
    assert st.get("leverage") == 3
    assert abs(aps - equity / 5) < 0.1, f"aps {aps} vs eq/5 {equity/5}"

    # Reflected in /api/status
    s = sess.get(f"{API}/status").json()
    assert s["num_slots"] == 5
    assert s["leverage"] == 3


def test_account_validation(sess):
    r = sess.post(f"{API}/account", json={"num_slots": 0}, timeout=10)
    assert r.status_code == 422
    r = sess.post(f"{API}/account", json={"leverage": 101}, timeout=10)
    assert r.status_code == 422
    r = sess.post(f"{API}/account", json={"equity_inr": -5}, timeout=10)
    assert r.status_code == 422


def test_account_equity_reset_and_restore(sess):
    _close_all_open(sess)
    # try reset; if 409 close positions and retry (race with paper trades opening)
    for _ in range(3):
        r = sess.post(f"{API}/account", json={"equity_inr": 10000}, timeout=15)
        if r.status_code == 200:
            break
        if r.status_code == 409:
            assert "open" in r.text.lower()
            _close_all_open(sess)
            time.sleep(1)
    assert r.status_code == 200, r.text
    st = sess.get(f"{API}/status").json()
    assert abs(st["equity_inr"] - 10000) < 1
    assert abs(st["starting_capital_inr"] - 10000) < 1
    assert st["realized_pnl_inr"] == 0
    assert st["trades"] == 0

    # closed history preserved
    cl = sess.get(f"{API}/positions/closed?limit=5000").json()
    assert isinstance(cl.get("items"), list)

    # equity curve reset (small count)
    eq = sess.get(f"{API}/equity-curve").json()
    assert len(eq.get("items", [])) < 50

    # restore to 4000/10/1
    _close_all_open(sess)
    for _ in range(3):
        r = sess.post(f"{API}/account", json={"equity_inr": 4000, "num_slots": 10, "leverage": 1}, timeout=15)
        if r.status_code == 200:
            break
        _close_all_open(sess)
        time.sleep(1)
    assert r.status_code == 200, r.text
    st = sess.get(f"{API}/status").json()
    assert st["num_slots"] == 10
    assert st["leverage"] == 1
    assert abs(st["equity_inr"] - 4000) < 1


# ---- Basis filter / spreads ----
def test_spreads_have_basis_fields(sess):
    r = sess.get(f"{API}/spreads", timeout=15)
    assert r.status_code == 200
    items = r.json().get("items", [])
    assert len(items) >= 1
    for it in items:
        for k in ["raw_spread_pct", "basis_pct", "gross_spread_pct"]:
            assert k in it
            assert isinstance(it[k], (int, float))
    # Check adjusted relation when basis filter on
    cfg = sess.get(f"{API}/config").json()
    if cfg.get("basis_filter"):
        for it in items:
            diff = abs(it["gross_spread_pct"] - (it["raw_spread_pct"] - it["basis_pct"]))
            assert diff < 0.001, f"basis mismatch: {it}"


def test_basis_filter_toggle(sess):
    r = sess.put(f"{API}/config", json={"basis_filter": False}, timeout=15)
    assert r.status_code == 200
    assert r.json().get("basis_filter") is False
    time.sleep(3)
    items = sess.get(f"{API}/spreads").json().get("items", [])
    for it in items:
        assert abs(it["gross_spread_pct"] - it["raw_spread_pct"]) < 1e-9, it
    # restore
    r = sess.put(f"{API}/config", json={"basis_filter": True}, timeout=15)
    assert r.status_code == 200
    assert r.json().get("basis_filter") is True


def test_config_new_fields(sess):
    d = sess.get(f"{API}/config").json()
    for k in ["basis_filter", "basis_window_s", "leverage", "laggard_exchange",
              "coindcx_client_ready", "digest_time_ist"]:
        assert k in d
    assert d["basis_window_s"] == 300
    assert d["digest_time_ist"] == "23:30"
    assert d["coindcx_client_ready"] is False


# ---- Analytics ----
def test_analytics_coins(sess):
    r = sess.get(f"{API}/analytics/coins", timeout=15)
    assert r.status_code == 200
    items = r.json().get("items", [])
    assert len(items) == 10
    for it in items:
        for k in ["coin", "trades", "wins", "losses", "win_rate", "net_pnl_inr",
                  "gross_pnl_inr", "fees_inr", "avg_hold_sec", "timeouts",
                  "target_hits", "stops", "last_trade_ts", "paused", "basis_pct"]:
            assert k in it, f"missing {k} in {it.get('coin')}"


# ---- Telegram digest (call ONCE) ----
def test_telegram_digest_once(sess):
    before = sess.get(f"{API}/telegram/status").json().get("sent_count", 0)
    r = sess.post(f"{API}/telegram/digest", timeout=30)
    assert r.status_code == 200
    assert r.json().get("ok") is True
    time.sleep(2)
    after = sess.get(f"{API}/telegram/status").json().get("sent_count", 0)
    assert after >= before + 1, f"sent_count {before} -> {after}"


# ---- Regression ----
def test_regression_endpoints(sess):
    r = sess.get(f"{API}/telemetry").json()
    assert r["feed"]["source"] == "COINSWITCH_WS"
    assert r["feed"].get("laggard_exchange") == "coinswitch"

    r = sess.get(f"{API}/benchmark/status", timeout=15)
    assert r.status_code == 200
    assert r.json().get("result") is not None

    r = sess.get(f"{API}/hurdle/simulate?hurdle_pct=0.1", timeout=15)
    assert r.status_code == 200

    r = sess.post(f"{API}/coins/BTC/pause", timeout=10)
    assert r.status_code == 200
    r = sess.post(f"{API}/coins/BTC/resume", timeout=10)
    assert r.status_code == 200

    r = sess.get(f"{API}/positions/closed/export.csv", timeout=15)
    assert r.status_code == 200
    assert "text/csv" in r.headers.get("content-type", "")
