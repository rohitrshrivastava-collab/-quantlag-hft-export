"""Iteration 4 backend tests: Telegram alerts + equity curve + spread heatmap + regression."""
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


# ---------------- Telegram ----------------
def test_telegram_status_shape(s):
    r = s.get(f"{API}/telegram/status", timeout=10)
    assert r.status_code == 200
    d = r.json()
    assert d["enabled"] is True
    assert d["bot_username"] == "Rohittelegram_bot"
    assert "…" in d["chat_id_masked"] or "..." in d["chat_id_masked"]
    for k in ("sent_count", "fail_count", "last_sent_ts", "last_error"):
        assert k in d


def test_telegram_test_sends_message(s):
    before = s.get(f"{API}/telegram/status").json()["sent_count"]
    r = s.post(f"{API}/telegram/test", timeout=15)
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True
    assert body.get("bot") == "Rohittelegram_bot"
    time.sleep(0.5)
    after = s.get(f"{API}/telegram/status").json()["sent_count"]
    assert after == before + 1, f"sent_count did not increment: {before}->{after}"


def test_telegram_summary_sends(s):
    before = s.get(f"{API}/telegram/status").json()["sent_count"]
    r = s.post(f"{API}/telegram/summary", timeout=15)
    assert r.status_code == 200
    assert r.json().get("ok") is True
    time.sleep(0.5)
    after = s.get(f"{API}/telegram/status").json()["sent_count"]
    assert after == before + 1


def test_telemetry_includes_telegram_and_feed(s):
    d = s.get(f"{API}/telemetry", timeout=10).json()
    assert "telegram" in d
    tg = d["telegram"]
    for k in ("enabled", "chat_id_masked", "bot_username", "sent_count", "fail_count", "last_sent_ts", "last_error"):
        assert k in tg, f"missing telegram.{k}"
    assert tg["enabled"] is True
    assert tg["bot_username"] == "Rohittelegram_bot"
    assert d["feed"]["source"] == "COINSWITCH_WS"


# ---------------- Spread heatmap ----------------
def test_spreads_heat_fields(s):
    sp = s.get(f"{API}/spreads", timeout=10).json()
    items = sp["items"]
    assert len(items) == 10
    for it in items:
        h = it["heat"]
        assert isinstance(h, list) and len(h) == 60, f"{it['coin']} heat len={len(h)}"
        for v in h:
            assert isinstance(v, (int, float))
            assert abs(v) < 5, f"{it['coin']} unusual heat val {v}"
        # near_hurdle_hits should equal count where abs>=0.8
        computed_hits = sum(1 for v in h if abs(v) >= 0.8)
        assert it["near_hurdle_hits_60s"] == computed_hits, \
            f"{it['coin']} hits mismatch: {it['near_hurdle_hits_60s']} vs {computed_hits}"
        assert 0 <= it["near_hurdle_hits_60s"] <= 60
        # peak_ratio_60s == max abs of heat, 3 dp
        expected_peak = round(max(abs(v) for v in h), 3) if h else 0
        assert abs(it["peak_ratio_60s"] - expected_peak) < 1e-6, \
            f"{it['coin']} peak mismatch: {it['peak_ratio_60s']} vs {expected_peak}"
        # existing fields unchanged
        for k in ("sparkline", "gross_spread_pct", "binance_bid", "binance_ask", "coinswitch_bid", "coinswitch_ask"):
            assert k in it


# ---------------- Equity curve grows ----------------
def test_equity_curve_grows(s):
    c1 = len(s.get(f"{API}/equity-curve", timeout=10).json()["items"])
    time.sleep(12)
    c2 = len(s.get(f"{API}/equity-curve", timeout=10).json()["items"])
    assert c2 > c1, f"equity curve did not grow: {c1} -> {c2}"


# ---------------- Regression ----------------
def test_regression_endpoints(s):
    for path in ("status", "config", "positions/open", "positions/closed", "logs", "hourly-summary"):
        r = s.get(f"{API}/{path}", timeout=10)
        assert r.status_code == 200, path
    r = s.post(f"{API}/coinswitch/check", timeout=10)
    assert r.status_code == 400


def test_bot_still_running_paper(s):
    d = s.get(f"{API}/status", timeout=10).json()
    assert d["running"] is True
    assert d["mode"] == "PAPER"
