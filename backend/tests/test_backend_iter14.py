"""Iteration 14: spike-persistence filter, latency split, CoinDCX user-WS idle timeout"""
import os
import time
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://arbitrage-engine-24.preview.emergentagent.com").rstrip("/")


@pytest.fixture(scope="module")
def s():
    sess = requests.Session()
    sess.headers.update({"Content-Type": "application/json"})
    return sess


# --- /api/status: spike_filter & latency split ---
def test_status_spike_filter_and_latency(s):
    r = s.get(f"{BASE_URL}/api/status", timeout=15)
    assert r.status_code == 200
    d = r.json()
    sf = d.get("spike_filter")
    assert isinstance(sf, dict), f"missing spike_filter: {d.keys()}"
    for k in ("crossings", "persisted", "reverted"):
        assert isinstance(sf.get(k), int), f"{k} not int: {sf.get(k)}"
    assert sf.get("persist_ms") == 300, f"persist_ms={sf.get('persist_ms')}"
    assert isinstance(sf.get("arming"), list)

    # latency split
    for k in ("avg_create_ms", "avg_fill_confirm_ms", "last_create_ms", "last_fill_confirm_ms"):
        v = d.get(k)
        assert v is None or isinstance(v, (int, float)), f"{k}={v}"
    assert isinstance(d.get("live_fill_samples"), int)


# --- /api/config: spike_persist_ms & scan_interval_s + PUT round-trip ---
def test_config_spike_persist_roundtrip(s):
    r = s.get(f"{BASE_URL}/api/config", timeout=15)
    assert r.status_code == 200
    cfg = r.json()
    assert cfg.get("spike_persist_ms") == 300, f"spike_persist_ms={cfg.get('spike_persist_ms')}"
    assert abs(float(cfg.get("scan_interval_s", 0)) - 0.1) < 1e-6, f"scan_interval_s={cfg.get('scan_interval_s')}"

    # PUT to 500
    r2 = s.put(f"{BASE_URL}/api/config", json={"spike_persist_ms": 500}, timeout=15)
    assert r2.status_code == 200, f"PUT 500 -> {r2.status_code} {r2.text}"
    r3 = s.get(f"{BASE_URL}/api/config", timeout=15).json()
    assert r3.get("spike_persist_ms") == 500

    # PUT back to 300
    r4 = s.put(f"{BASE_URL}/api/config", json={"spike_persist_ms": 300}, timeout=15)
    assert r4.status_code == 200
    r5 = s.get(f"{BASE_URL}/api/config", timeout=15).json()
    assert r5.get("spike_persist_ms") == 300

    # invalid PUT 20000 -> 422
    r6 = s.put(f"{BASE_URL}/api/config", json={"spike_persist_ms": 20000}, timeout=15)
    assert r6.status_code == 422, f"expected 422, got {r6.status_code} {r6.text}"


# --- /api/spreads signal_state enum ---
def test_spreads_signal_state(s):
    allowed = {"WAITING", "ARMING_LONG", "ARMING_SHORT", "HURDLE_MET_LONG", "HURDLE_MET_SHORT",
               "IN_TRADE", "PAUSED", "PHANTOM", "THIN", "STALE_BOOK"}
    r = s.get(f"{BASE_URL}/api/spreads", timeout=15)
    assert r.status_code == 200
    data = r.json()
    items = data.get("items") if isinstance(data, dict) else data
    assert isinstance(items, list) and len(items) > 0
    for it in items:
        ss = it.get("signal_state")
        assert ss in allowed, f"invalid signal_state={ss} in {it.get('symbol')}"


# --- /api/telemetry idle-timeout no reconnect surge ---
def test_telemetry_userws_no_reconnect_growth(s):
    r1 = s.get(f"{BASE_URL}/api/telemetry", timeout=15).json()
    uw1 = r1.get("feed", {}).get("user_ws", {})
    assert uw1.get("joined") is True, f"user_ws joined={uw1.get('joined')}"
    rc1 = uw1.get("reconnects", 0)
    assert rc1 <= 2, f"initial reconnects too high: {rc1}"
    time.sleep(40)
    r2 = s.get(f"{BASE_URL}/api/telemetry", timeout=15).json()
    uw2 = r2.get("feed", {}).get("user_ws", {})
    rc2 = uw2.get("reconnects", 0)
    assert rc2 == rc1, f"reconnects grew {rc1} -> {rc2} during 40s idle"


# --- /api/compare exchanges.*.skips has 'stale' key ---
def test_compare_skips_stale(s):
    r = s.get(f"{BASE_URL}/api/compare", timeout=15)
    assert r.status_code == 200
    d = r.json()
    exs = d.get("exchanges", {})
    assert exs, "no exchanges in /api/compare"
    for name, ex in exs.items():
        skips = ex.get("skips", {})
        assert "stale" in skips, f"'stale' missing in {name}.skips keys={list(skips.keys())}"


# --- /api/positions/closed has create_ms/fill_confirm_ms ---
def test_positions_closed_latency_fields(s):
    r = s.get(f"{BASE_URL}/api/positions/closed?limit=3", timeout=15)
    assert r.status_code == 200
    data = r.json()
    items = data.get("items") if isinstance(data, dict) else data
    assert isinstance(items, list)
    for it in items:
        assert "create_ms" in it, f"create_ms missing in {it}"
        assert "fill_confirm_ms" in it, f"fill_confirm_ms missing in {it}"
        for k in ("create_ms", "fill_confirm_ms"):
            v = it.get(k)
            assert v is None or isinstance(v, (int, float)), f"{k}={v}"
