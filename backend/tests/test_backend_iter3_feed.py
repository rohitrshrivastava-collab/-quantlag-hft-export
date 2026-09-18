"""Iteration 3 backend tests: CoinSwitch WS feed telemetry + regression."""
import os
import time
import pytest
import requests

BASE_URL = (os.environ.get("REACT_APP_BACKEND_URL")
            or open("/app/frontend/.env").read().split("REACT_APP_BACKEND_URL=")[1].split()[0]).rstrip("/")
API = f"{BASE_URL}/api"


@pytest.fixture(scope="module")
def s():
    sess = requests.Session()
    sess.headers.update({"Content-Type": "application/json"})
    return sess


# ------------ NEW FEATURE: /api/telemetry.feed ------------
def test_telemetry_feed_shape(s):
    d = s.get(f"{API}/telemetry", timeout=10).json()
    assert d["simulate"] is False
    feed = d["feed"]
    assert feed["source"] == "COINSWITCH_WS"
    assert feed["simulate"] is False
    lag = feed["laggard"]
    assert lag["connected"] is True
    assert lag["subscribed"] is True
    assert isinstance(lag["wire_latency_ms"], (int, float))
    assert 5 <= lag["wire_latency_ms"] <= 2000, f"wire_latency out of range: {lag['wire_latency_ms']}"
    assert lag["msgs_per_s"] > 5, f"msgs_per_s too low: {lag['msgs_per_s']}"
    coins = lag["coins"]
    assert isinstance(coins, list) and len(coins) == 10
    for c in coins:
        assert isinstance(c["age_ms"], (int, float))
        assert c["age_ms"] < 10000, f"{c['coin']} age_ms={c['age_ms']}"
        assert c["stale"] is False
    leader = feed["leader"]
    assert leader["connected"] is True
    assert leader["coins_with_quotes"] == 10
    rf = feed["rest_fallback"]
    assert rf == {"available": False, "active": False}


def test_spreads_have_inr_and_usdt_quotes(s):
    tel = s.get(f"{API}/telemetry", timeout=10).json()
    fx = tel["fx_usdt_inr"]
    sp = s.get(f"{API}/spreads", timeout=10).json()
    items = sp["items"]
    assert len(items) == 10
    for it in items:
        assert it["binance_bid"] is not None and it["binance_ask"] is not None
        assert it["binance_bid"] > 0 and it["binance_ask"] > 0
        assert it["coinswitch_bid"] is not None and it["coinswitch_ask"] is not None
        assert it["coinswitch_ask"] >= it["coinswitch_bid"]
        # INR-scaled: coinswitch_bid ~ binance_bid * fx (within 3%)
        expected = it["binance_bid"] * fx
        diff = abs(it["coinswitch_bid"] - expected) / expected
        assert diff < 0.03, f"{it['coin']} csw_bid={it['coinswitch_bid']} vs expected={expected} diff={diff:.3%}"
        assert abs(it["gross_spread_pct"]) < 2


# ------------ Regression: existing endpoints ------------
def test_status(s):
    r = s.get(f"{API}/status", timeout=10)
    assert r.status_code == 200
    d = r.json()
    for k in ("running", "mode", "equity_inr"):
        assert k in d


def test_config_simulate_false(s):
    r = s.get(f"{API}/config", timeout=10)
    assert r.status_code == 200
    d = r.json()
    assert d.get("simulate") is False


def test_positions_and_curves(s):
    for path in ("positions/open", "positions/closed", "equity-curve", "hourly-summary"):
        r = s.get(f"{API}/{path}", timeout=10)
        assert r.status_code == 200, path


def test_logs_contain_startup_msg(s):
    r = s.get(f"{API}/logs", timeout=10)
    assert r.status_code == 200
    items = r.json()["items"]
    joined = " ".join(str(l.get("msg", "")) for l in items)
    # We'll accept either PAPER-mode startup log variant
    assert "PAPER" in joined and ("CoinSwitch" in joined or "COINSWITCH" in joined.upper()), \
        f"startup log not found; sample: {joined[:400]}"


def test_coinswitch_check_400(s):
    r = s.post(f"{API}/coinswitch/check", timeout=10)
    assert r.status_code == 400


# ------------ Stop/start cycle => laggard reconnects ------------
def test_stop_start_cycle_feed_recovers(s):
    r = s.post(f"{API}/bot/stop", timeout=10)
    assert r.status_code == 200
    time.sleep(2.5)
    tel = s.get(f"{API}/telemetry", timeout=15).json()
    # after stop, laggard connected should have flipped false at some point
    stopped_ok = (tel["feed"]["laggard"]["connected"] is False) or (s.get(f"{API}/status").json()["running"] is False)
    assert stopped_ok, f"bot did not stop: {tel['feed']['laggard']}"

    r2 = s.post(f"{API}/bot/start", timeout=10)
    assert r2.status_code == 200
    # wait up to 15s for feed to recover
    ok = False
    for _ in range(15):
        time.sleep(1)
        tel = s.get(f"{API}/telemetry", timeout=15).json()
        lag = tel["feed"]["laggard"]
        if (tel["feed"]["source"] == "COINSWITCH_WS" and lag["subscribed"] and lag["connected"]
                and all(c["age_ms"] < 10000 for c in lag["coins"])):
            ok = True
            break
    assert ok, f"feed did not recover: {tel['feed']}"


# ------------ Mode toggle: LIVE returns warning, PAPER restored ------------
def test_mode_live_warning_then_paper(s):
    r = s.post(f"{API}/bot/mode", json={"paper_trading": False}, timeout=10)
    assert r.status_code == 200
    body = r.json()
    # warning should mention keys / coinswitch
    warn = str(body.get("warning", "")).lower() + " " + str(body).lower()
    assert "key" in warn or "coinswitch" in warn, f"no missing-keys warning: {body}"
    r2 = s.post(f"{API}/bot/mode", json={"paper_trading": True}, timeout=10)
    assert r2.status_code == 200
    assert s.get(f"{API}/status").json()["mode"] == "PAPER"
