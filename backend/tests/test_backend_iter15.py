"""Iteration 15: top-of-book edges (long_edge/short_edge), dynamic volatility stop,
max_book_share_pct, breakeven_floor, spike_persist_ms=0 semantics."""
import os
import time
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL").rstrip("/")


@pytest.fixture(scope="module")
def s():
    sess = requests.Session()
    sess.headers.update({"Content-Type": "application/json"})
    return sess


def _items(payload):
    return payload.get("items") if isinstance(payload, dict) else payload


# --- /api/spreads: edges + vol_pct + recommended_stop_pct + stop_basis ---
def test_spreads_edges_and_stop_fields(s):
    r = s.get(f"{BASE_URL}/api/spreads", timeout=15)
    assert r.status_code == 200
    items = _items(r.json())
    assert isinstance(items, list) and len(items) > 0
    allowed_basis = {"dynamic", "fixed", "fixed (warming up)"}
    for it in items:
        sym = it.get("symbol")
        le = it.get("long_edge_pct")
        se = it.get("short_edge_pct")
        assert isinstance(le, (int, float)), f"{sym} long_edge_pct not number: {le}"
        assert isinstance(se, (int, float)), f"{sym} short_edge_pct not number: {se}"
        # invariant: at most one > 0
        assert not (le > 0 and se > 0), f"{sym} both edges positive: long={le} short={se}"
        vp = it.get("vol_pct")
        assert vp is None or isinstance(vp, (int, float)), f"{sym} vol_pct={vp}"
        rs = it.get("recommended_stop_pct")
        assert isinstance(rs, (int, float)), f"{sym} recommended_stop_pct not number: {rs}"
        sb = it.get("stop_basis")
        assert sb in allowed_basis, f"{sym} stop_basis={sb}"
        if sb == "dynamic":
            assert 0.15 <= rs <= 1.0 + 1e-9, f"{sym} dynamic rec_stop out of range: {rs}"


# --- /api/config default fields ---
def test_config_new_fields(s):
    r = s.get(f"{BASE_URL}/api/config", timeout=15)
    assert r.status_code == 200
    cfg = r.json()
    assert cfg.get("stop_mode") == "dynamic", f"stop_mode={cfg.get('stop_mode')}"
    assert float(cfg.get("vol_k")) == 2, f"vol_k={cfg.get('vol_k')}"
    assert float(cfg.get("stop_min_pct")) == 0.15, f"stop_min_pct={cfg.get('stop_min_pct')}"
    assert float(cfg.get("stop_max_pct")) == 1.0, f"stop_max_pct={cfg.get('stop_max_pct')}"
    assert float(cfg.get("max_book_share_pct")) == 25, f"max_book_share_pct={cfg.get('max_book_share_pct')}"
    assert cfg.get("breakeven_floor") is True, f"breakeven_floor={cfg.get('breakeven_floor')}"
    assert float(cfg.get("vol_window_s")) == 300, f"vol_window_s={cfg.get('vol_window_s')}"


# --- stop_mode toggle round-trip ---
def test_stop_mode_roundtrip(s):
    cfg0 = s.get(f"{BASE_URL}/api/config", timeout=15).json()
    fixed_target = float(cfg0.get("stop_loss_pct"))

    # -> fixed
    r = s.put(f"{BASE_URL}/api/config", json={"stop_mode": "fixed"}, timeout=15)
    assert r.status_code == 200, f"PUT fixed -> {r.status_code} {r.text}"
    time.sleep(2)
    items = _items(s.get(f"{BASE_URL}/api/spreads", timeout=15).json())
    for it in items:
        assert it.get("stop_basis") == "fixed", f"{it.get('symbol')} stop_basis={it.get('stop_basis')} (expected fixed)"
        assert abs(float(it.get("recommended_stop_pct")) - fixed_target) < 1e-6, \
            f"{it.get('symbol')} rec_stop {it.get('recommended_stop_pct')} != {fixed_target}"

    # -> dynamic
    r2 = s.put(f"{BASE_URL}/api/config", json={"stop_mode": "dynamic"}, timeout=15)
    assert r2.status_code == 200
    time.sleep(2)
    items2 = _items(s.get(f"{BASE_URL}/api/spreads", timeout=15).json())
    for it in items2:
        sb = it.get("stop_basis")
        assert sb in ("dynamic", "fixed (warming up)"), f"{it.get('symbol')} stop_basis={sb}"

    # invalid
    r3 = s.put(f"{BASE_URL}/api/config", json={"stop_mode": "banana"}, timeout=15)
    assert r3.status_code == 422, f"expected 422, got {r3.status_code} {r3.text}"


def test_vol_k_and_book_share_roundtrip(s):
    r = s.put(f"{BASE_URL}/api/config", json={"vol_k": 3, "max_book_share_pct": 40}, timeout=15)
    assert r.status_code == 200, r.text
    cfg = s.get(f"{BASE_URL}/api/config", timeout=15).json()
    assert float(cfg.get("vol_k")) == 3
    assert float(cfg.get("max_book_share_pct")) == 40
    # restore
    r2 = s.put(f"{BASE_URL}/api/config", json={"vol_k": 2, "max_book_share_pct": 25}, timeout=15)
    assert r2.status_code == 200
    cfg2 = s.get(f"{BASE_URL}/api/config", timeout=15).json()
    assert float(cfg2.get("vol_k")) == 2
    assert float(cfg2.get("max_book_share_pct")) == 25


# --- /api/positions/closed items include stop_pct & stop_basis ---
def test_positions_closed_stop_fields(s):
    r = s.get(f"{BASE_URL}/api/positions/closed?limit=3", timeout=15)
    assert r.status_code == 200
    items = _items(r.json())
    assert isinstance(items, list)
    for it in items:
        assert "stop_pct" in it, f"stop_pct missing: keys={list(it.keys())}"
        assert "stop_basis" in it, f"stop_basis missing: keys={list(it.keys())}"


# --- /api/compare returns both exchanges with verdict ---
def test_compare_has_both_and_verdict(s):
    r = s.get(f"{BASE_URL}/api/compare", timeout=15)
    assert r.status_code == 200
    d = r.json()
    exs = d.get("exchanges", {})
    # compare returns the two laggards vs Binance leader
    assert len(exs) >= 2, f"exchanges={list(exs.keys())}"
    assert "coindcx" in exs, f"coindcx missing from {list(exs.keys())}"
    # verdict may live at top level or per-exchange
    verdict = d.get("verdict")
    if verdict is None:
        # any per-exchange verdict is fine
        assert any("verdict" in ex for ex in exs.values()), f"no verdict found in {d}"
    else:
        assert isinstance(verdict, (str, dict))
