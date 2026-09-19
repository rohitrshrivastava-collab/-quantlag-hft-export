"""Iteration 7 backend tests: coindcx/check, telemetry feed (fx, recorder, restored_settings),
persistence, replay sessions/run/last, and regression endpoints."""
import os
import time
import json
import subprocess
import requests
import pytest

BASE = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
assert BASE, "REACT_APP_BACKEND_URL is required"
API = f"{BASE}/api"

SETTINGS_FILE = "/app/data/settings.json"


def get(path, **kw):
    return requests.get(f"{API}{path}", timeout=20, **kw)


def post(path, **kw):
    return requests.post(f"{API}{path}", timeout=30, **kw)


# --- CoinDCX preflight ---
def test_coindcx_check_returns_424_with_json_401_detail():
    r = post("/coindcx/check")
    assert r.status_code == 424, f"expected 424, got {r.status_code}: {r.text[:300]}"
    ct = r.headers.get("content-type", "")
    assert "application/json" in ct, f"not JSON: {ct}"
    body = r.json()
    detail = body.get("detail", "")
    if isinstance(detail, dict):
        detail = json.dumps(detail)
    assert "CoinDCX API 401" in detail, f"missing 'CoinDCX API 401' in: {detail}"
    assert "401 = key/secret rejected" in detail, f"missing hint in: {detail}"


# --- Telemetry: fx + recorder + restored_settings ---
def test_telemetry_fx_recorder_restored():
    r = get("/telemetry")
    assert r.status_code == 200
    tel = r.json()
    feed = tel.get("feed", {})
    fx = feed.get("fx", {})
    rate = fx.get("rate")
    assert isinstance(rate, (int, float)) and 60 <= rate <= 130, f"fx rate off: {rate}"
    sources = fx.get("sources", {})
    for k in ("coingecko", "coindcx", "binance_p2p"):
        assert k in sources, f"missing source {k}: {list(sources.keys())}"
    # gather numeric rates
    numeric = []
    for k, v in sources.items():
        r_ = v.get("rate") if isinstance(v, dict) else v
        if isinstance(r_, (int, float)):
            numeric.append(r_)
    assert len(numeric) >= 2, f"want >=2 non-null source rates: {sources}"
    used = fx.get("used", [])
    assert isinstance(used, list) and len(used) >= 2, f"used list: {used}"
    # median check within 0.01
    used_rates = []
    for name in used:
        v = sources.get(name)
        r_ = v.get("rate") if isinstance(v, dict) else v
        if isinstance(r_, (int, float)):
            used_rates.append(r_)
    used_rates.sort()
    n = len(used_rates)
    if n:
        median = used_rates[n // 2] if n % 2 else (used_rates[n // 2 - 1] + used_rates[n // 2]) / 2
        assert abs(rate - median) <= 0.01, f"rate {rate} != median {median} used={used_rates}"

    rec = feed.get("recorder", {})
    assert rec.get("enabled") is True, rec
    assert rec.get("lines_written", 0) > 0, rec
    assert rec.get("retention_days") == 7, rec
    assert isinstance(rec.get("sessions"), list), rec

    rs = feed.get("restored_settings")
    assert isinstance(rs, dict), f"restored_settings not a dict: {rs}"


def test_status_fx_matches_telemetry():
    tel = get("/telemetry").json()
    st = get("/status").json()
    fx_rate = tel["feed"]["fx"]["rate"]
    assert abs(st.get("fx_usdt_inr") - fx_rate) <= 0.001


# --- Persistence + restart ---
def _read_settings():
    with open(SETTINGS_FILE) as f:
        return json.load(f)


def test_persistence_and_restart():
    # pause DOGE and set num_slots=7 leverage=2
    r1 = post("/coins/DOGE/pause")
    assert r1.status_code == 200, r1.text
    r2 = post("/account", json={"num_slots": 7, "leverage": 2})
    assert r2.status_code == 200, r2.text

    time.sleep(2)
    s = _read_settings()
    assert "DOGE" in s.get("paused", []), s
    assert s["config"]["num_slots"] == 7
    assert s["config"]["leverage"] == 2
    assert isinstance(s["state"].get("equity_inr"), (int, float))

    # restart backend
    subprocess.run(["sudo", "supervisorctl", "restart", "backend"], check=False, capture_output=True)
    # wait for up
    up = False
    for _ in range(30):
        time.sleep(1)
        try:
            r = get("/status")
            if r.status_code == 200:
                up = True
                break
        except Exception:
            pass
    assert up, "backend did not come up"
    time.sleep(3)  # feed to settle

    st = get("/status").json()
    assert st.get("num_slots") == 7, st
    assert st.get("leverage") == 2, st

    sp = get("/spreads").json()
    doge = None
    items = sp if isinstance(sp, list) else sp.get("items", [])
    for it in items:
        if it.get("coin") == "DOGE":
            doge = it
            break
    assert doge is not None, f"no DOGE in spreads"
    assert doge.get("paused") is True, doge

    tel = get("/telemetry").json()
    rs = tel["feed"].get("restored_settings", {})
    assert rs.get("num_slots") == 7, rs
    assert "DOGE" in rs.get("paused", []), rs

    # restore
    r3 = post("/coins/DOGE/resume")
    assert r3.status_code == 200
    r4 = post("/account", json={"num_slots": 10, "leverage": 1})
    assert r4.status_code == 200
    time.sleep(2)
    s2 = _read_settings()
    assert s2["config"]["num_slots"] == 10
    assert s2["config"]["leverage"] == 1
    assert "DOGE" not in s2.get("paused", [])


# --- Replay ---
@pytest.fixture(scope="module")
def today_session():
    r = get("/replay/sessions")
    assert r.status_code == 200
    body = r.json()
    items = body.get("items", [])
    assert isinstance(items, list) and len(items) >= 1, body
    rec = body.get("recorder", {})
    assert isinstance(rec, dict)
    # pick most recent (assume last or sorted)
    date = items[-1].get("date")
    for it in items:
        assert "date" in it and "size_mb" in it and "modified" in it
    return date


def test_replay_sessions_shape(today_session):
    assert today_session and len(today_session) == 10  # YYYY-MM-DD


def test_replay_run_basic(today_session):
    payload = {"date": today_session, "hurdle_pct": 0.05, "leverage": 3}
    r = post("/replay/run", json=payload)
    assert r.status_code == 200, r.text[:400]
    d = r.json()
    for k in ("file", "lines", "span_s", "params", "trades", "wins", "win_rate",
              "net_pnl_inr", "final_equity_inr", "return_pct", "max_drawdown_pct",
              "per_coin", "equity_curve", "last_trades"):
        assert k in d, f"missing key {k} in {list(d.keys())}"
    assert d["lines"] > 0
    assert d["span_s"] > 0
    p = d["params"]
    assert p["hurdle_pct"] == 0.05
    assert p["leverage"] == 3
    for k in ("num_slots", "equity_inr", "basis_filter"):
        assert k in p, p
    assert isinstance(d["trades"], int)
    eq_in = p["equity_inr"]
    assert abs(d["final_equity_inr"] - (eq_in + d["net_pnl_inr"])) <= 0.01
    assert isinstance(d["per_coin"], list)
    assert isinstance(d["equity_curve"], list)
    if d["equity_curve"]:
        assert "ts" in d["equity_curve"][0] and "equity" in d["equity_curve"][0]
    assert isinstance(d["last_trades"], list)


def test_replay_run_high_hurdle_zero_trades(today_session):
    r = post("/replay/run", json={"date": today_session, "hurdle_pct": 5.0})
    assert r.status_code == 200, r.text[:300]
    d = r.json()
    assert d["trades"] == 0
    assert abs(d["net_pnl_inr"]) <= 1e-6


def test_replay_last():
    r = get("/replay/last")
    assert r.status_code == 200
    d = r.json()
    assert d.get("running") is False
    assert d.get("result") is not None
    assert d.get("error") in (None, "")


def test_replay_run_404_bad_date():
    r = post("/replay/run", json={"date": "2000-01-01"})
    assert r.status_code == 404, r.status_code


def test_replay_run_422_bad_hurdle(today_session):
    r = post("/replay/run", json={"date": today_session, "hurdle_pct": 0})
    assert r.status_code == 422, f"{r.status_code}: {r.text[:200]}"


# --- Regression ---
@pytest.mark.parametrize("path", [
    "/spreads",
    "/analytics/coins",
    "/benchmark/status",
    "/hurdle/simulate?hurdle_pct=0.1",
    "/positions/closed/export.csv",
    "/telegram/status",
])
def test_regression_endpoints(path):
    r = get(path)
    assert r.status_code == 200, f"{path} -> {r.status_code}: {r.text[:200]}"
