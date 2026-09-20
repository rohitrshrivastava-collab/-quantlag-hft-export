"""Iteration 12 backend tests: phantom-liquidity guard + shadow compare."""
import os
import time
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/") or \
           open("/app/frontend/.env").read().split("REACT_APP_BACKEND_URL=")[1].split("\n")[0].strip()
API = f"{BASE_URL}/api"

FEES_PLUS_EXTRA = 0.05 + 0.05 + 0.02  # entry+exit taker + hurdle_extra


# --- /api/spreads liquidity block ---
class TestSpreadsLiquidity:
    def test_all_items_have_liquidity(self):
        r = requests.get(f"{API}/spreads", timeout=15)
        assert r.status_code == 200
        items = r.json().get("items", [])
        assert len(items) >= 5
        for it in items:
            liq = it.get("liquidity")
            assert isinstance(liq, dict), f"{it.get('coin')} missing liquidity: {it}"
            assert liq["state"] in ("LIQUID", "THIN", "PHANTOM", "NO_DEPTH"), f"bad state {liq['state']}"
            assert isinstance(liq["vwap_inr"], (int, float))
            if liq["state"] != "NO_DEPTH":
                assert liq["vwap_inr"] > 0, f"{it.get('coin')} vwap<=0 {liq}"
            assert liq["slippage_pct"] >= 0
            assert isinstance(liq["fillable_inr"], (int, float))
            assert isinstance(liq["depth_inr"], (int, float))
            assert isinstance(liq["levels"], int) and 0 <= liq["levels"] <= 20
            assert isinstance(liq["frozen_s"], (int, float)) and liq["frozen_s"] >= 0
            assert isinstance(liq["notional_inr"], (int, float))
            assert isinstance(liq["dyn_hurdle_pct"], (int, float))
            # dyn_hurdle_pct must be >= fees+extra (0.12)
            assert liq["dyn_hurdle_pct"] >= FEES_PLUS_EXTRA - 1e-9, \
                f"{it.get('coin')} dyn_hurdle_pct={liq['dyn_hurdle_pct']} < {FEES_PLUS_EXTRA}"


# --- /api/liquidity/{coin} ---
class TestLiquidityEndpoint:
    def test_doge_short(self):
        r = requests.get(f"{API}/liquidity/DOGE?direction=SHORT&notional_inr=5000", timeout=10)
        assert r.status_code == 200
        j = r.json()
        assert j["coin"] == "DOGE"
        assert j["direction"] == "SHORT"
        assert j["state"] in ("LIQUID", "THIN", "PHANTOM", "NO_DEPTH")
        assert isinstance(j["vwap_inr"], (int, float))
        assert j["slippage_pct"] >= 0
        assert isinstance(j["fillable_inr"], (int, float))
        book = j.get("book_inr", {})
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        assert isinstance(bids, list) and len(bids) <= 10
        assert isinstance(asks, list) and len(asks) <= 10
        # bids descending
        bid_prices = [b[0] for b in bids]
        assert bid_prices == sorted(bid_prices, reverse=True), f"bids not descending: {bid_prices}"
        # asks ascending
        ask_prices = [a[0] for a in asks]
        assert ask_prices == sorted(ask_prices), f"asks not ascending: {ask_prices}"
        # rows are [price, qty]
        for row in bids + asks:
            assert len(row) == 2
            assert isinstance(row[0], (int, float)) and isinstance(row[1], (int, float))

    def test_unknown_coin_404(self):
        r = requests.get(f"{API}/liquidity/FOO", timeout=10)
        assert r.status_code == 404


# --- /api/config new fields + PUT roundtrip ---
class TestConfigNewFields:
    def test_defaults(self):
        r = requests.get(f"{API}/config", timeout=10)
        assert r.status_code == 200
        j = r.json()
        assert abs(j["max_slippage_pct"] - 0.05) < 1e-9
        assert abs(j["phantom_frozen_s"] - 3) < 1e-9
        assert abs(j["min_order_inr"] - 100) < 1e-9
        assert abs(j["depth_bps"] - 10) < 1e-9

    def test_put_roundtrip(self):
        # save originals
        orig = requests.get(f"{API}/config", timeout=10).json()
        payload = {"max_slippage_pct": 0.08, "phantom_frozen_s": 4, "min_order_inr": 120}
        r = requests.put(f"{API}/config", json=payload, timeout=10)
        assert r.status_code == 200, f"PUT failed: {r.status_code} {r.text}"
        # verify
        j = requests.get(f"{API}/config", timeout=10).json()
        assert abs(j["max_slippage_pct"] - 0.08) < 1e-9
        assert abs(j["phantom_frozen_s"] - 4) < 1e-9
        assert abs(j["min_order_inr"] - 120) < 1e-9
        # restore
        restore = {"max_slippage_pct": 0.05, "phantom_frozen_s": 3, "min_order_inr": 100}
        r = requests.put(f"{API}/config", json=restore, timeout=10)
        assert r.status_code == 200
        j2 = requests.get(f"{API}/config", timeout=10).json()
        assert abs(j2["max_slippage_pct"] - 0.05) < 1e-9

    def test_put_out_of_range_422(self):
        r = requests.put(f"{API}/config", json={"max_slippage_pct": 5}, timeout=10)
        assert r.status_code == 422, f"expected 422, got {r.status_code}: {r.text}"


# --- /api/compare shape ---
class TestCompare:
    def test_compare_shape(self):
        r = requests.get(f"{API}/compare", timeout=20)
        assert r.status_code == 200
        j = r.json()
        assert isinstance(j.get("runtime_s"), (int, float))
        assert j.get("primary") == "coindcx"
        ex = j.get("exchanges", {})
        assert "coinswitch" in ex and "coindcx" in ex
        for name in ("coinswitch", "coindcx"):
            e = ex[name]
            for k in ("signals", "trades", "net_inr", "net_inr_per_hour",
                      "lag_window_s", "catch_up_s", "avg_abs_spread_pct",
                      "slippage_pct", "depth_10bps_inr", "slippage_ladder",
                      "skips", "feed", "equity_curve"):
                assert k in e, f"{name} missing key {k}"
            lw = e["lag_window_s"]
            for k in ("median", "p75", "n"):
                assert k in lw, f"{name} lag_window_s missing {k}"
            slp = e["slippage_pct"]
            assert "median" in slp and "p90" in slp
            ladder = e["slippage_ladder"]
            for k in ("10000", "100000", "1000000"):
                assert k in ladder, f"{name} slippage_ladder missing {k}"
            sk = e["skips"]
            for k in ("phantom", "thin", "edge", "shrunk"):
                assert k in sk, f"{name} skips missing {k}"
            feed = e["feed"]
            assert feed.get("connected") is True, f"{name} not connected"
            assert feed.get("subscribed") is True, f"{name} not subscribed"
            assert isinstance(feed.get("msgs_per_s"), (int, float))
            assert isinstance(e["equity_curve"], list)
        v = j.get("verdict", {})
        assert v.get("winner") in ("coinswitch", "coindcx", "tie", None)
        assert v.get("confidence") in ("low", "medium", "high", "none")
        assert isinstance(v.get("lines"), list) and len(v["lines"]) > 0


# --- /api/telemetry liquidity_guard ---
class TestTelemetryLiquidityGuard:
    def test_liquidity_guard(self):
        j = requests.get(f"{API}/telemetry", timeout=10).json()
        lg = j.get("feed", {}).get("liquidity_guard")
        assert isinstance(lg, dict), f"liquidity_guard missing: {j.get('feed')}"
        for k in ("phantom_skips", "thin_skips", "edge_skips", "shrunk"):
            assert isinstance(lg.get(k), int), f"{k} not int: {lg.get(k)}"
        assert isinstance(lg.get("max_slippage_pct"), (int, float))


# --- /api/positions/closed new fields ---
class TestClosedPositionsNewFields:
    def test_new_liq_fields_present(self):
        j = requests.get(f"{API}/positions/closed?limit=20", timeout=10).json()
        items = j.get("items", []) if isinstance(j, dict) else j
        assert isinstance(items, list) and len(items) > 0
        for it in items:
            assert "liq_state" in it
            assert "liq_slippage_pct" in it
            assert "requested_notional_inr" in it


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
