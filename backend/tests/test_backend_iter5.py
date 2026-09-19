"""Iteration 5 backend tests: closed CSV export, per-coin pause, hurdle sim, benchmark, verdict."""
import os
import time
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://arbitrage-engine-24.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"


@pytest.fixture(scope="module")
def s():
    return requests.Session()


# --- Closed positions filter + CSV export --------------------------------------
class TestClosedPositions:
    def test_closed_all(self, s):
        r = s.get(f"{API}/positions/closed")
        assert r.status_code == 200
        assert "items" in r.json()

    def test_closed_filter_ada(self, s):
        r = s.get(f"{API}/positions/closed", params={"coin": "ADA"})
        assert r.status_code == 200
        items = r.json()["items"]
        assert all(i["coin"] == "ADA" for i in items)

    def test_csv_all(self, s):
        r = s.get(f"{API}/positions/closed/export.csv")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/csv")
        cd = r.headers.get("content-disposition", "")
        assert "attachment" in cd and "quantlag_trades_all_" in cd and cd.endswith('.csv"')
        header = r.text.splitlines()[0]
        for col in ["id", "coin", "direction", "mode", "size_inr", "net_pnl_inr",
                    "close_reason", "opened_at_iso", "closed_at_iso", "hold_sec"]:
            assert col in header, f"missing col {col} in header: {header}"

    def test_csv_filter_btc(self, s):
        r = s.get(f"{API}/positions/closed/export.csv", params={"coin": "BTC"})
        assert r.status_code == 200
        cd = r.headers.get("content-disposition", "")
        assert "_BTC_" in cd
        lines = r.text.splitlines()
        header = lines[0].split(",")
        coin_idx = header.index("coin")
        for row in lines[1:]:
            if row.strip():
                assert row.split(",")[coin_idx] == "BTC"


# --- Per-coin pause/resume -----------------------------------------------------
class TestCoinPause:
    def test_pause_and_resume_eth(self, s):
        r = s.post(f"{API}/coins/ETH/pause")
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] and data["paused"] is True and "ETH" in data["paused_coins"]

        time.sleep(2.2)
        spreads = s.get(f"{API}/spreads").json()["items"]
        eth = next((i for i in spreads if i["coin"] == "ETH"), None)
        assert eth is not None and eth.get("paused") is True
        assert eth.get("signal_state") == "PAUSED"

        r = s.post(f"{API}/coins/ETH/resume")
        assert r.status_code == 200
        assert r.json()["paused"] is False
        time.sleep(2.2)
        spreads = s.get(f"{API}/spreads").json()["items"]
        eth = next((i for i in spreads if i["coin"] == "ETH"), None)
        assert eth["paused"] is False
        assert eth["signal_state"] != "PAUSED"

    def test_pause_unknown_404(self, s):
        r = s.post(f"{API}/coins/FOO/pause")
        assert r.status_code == 404


# --- Hurdle simulate -----------------------------------------------------------
class TestHurdleSim:
    def test_basic(self, s):
        r = s.get(f"{API}/hurdle/simulate", params={"hurdle_pct": 0.05, "window_s": 600})
        assert r.status_code == 200
        j = r.json()
        assert "draft" in j and "current" in j
        d = j["draft"]
        assert d["hurdle_pct"] == 0.05 and d["window_s"] == 600
        for k in ["covered_s", "total_episodes", "total_seconds_above", "coins"]:
            assert k in d
        assert len(d["coins"]) == 10
        for c in d["coins"]:
            assert set(c.keys()) >= {"coin", "episodes", "seconds_above"}
        assert j["current"]["hurdle_pct"] == pytest.approx(0.15, rel=0.01)

    def test_lower_hurdle_more_episodes(self, s):
        low = s.get(f"{API}/hurdle/simulate", params={"hurdle_pct": 0.02}).json()["draft"]
        cur = s.get(f"{API}/hurdle/simulate", params={"hurdle_pct": 0.15}).json()["draft"]
        assert low["total_episodes"] >= cur["total_episodes"]

    def test_invalid(self, s):
        assert s.get(f"{API}/hurdle/simulate", params={"hurdle_pct": 0}).status_code == 400
        assert s.get(f"{API}/hurdle/simulate", params={"hurdle_pct": 20}).status_code == 400


# --- Benchmark -----------------------------------------------------------------
class TestBenchmark:
    def test_status_has_result(self, s):
        # Wait if a run is in progress
        for _ in range(40):
            j = s.get(f"{API}/benchmark/status").json()
            if not j["running"]:
                break
            time.sleep(1)
        j = s.get(f"{API}/benchmark/status").json()
        assert j["running"] is False
        res = j["result"]
        assert res is not None
        for ex in ["binance", "coinswitch", "coindcx"]:
            e = res["exchanges"][ex]
            assert e["tcp_rtt_ms"]["n"] > 0
            assert e["tcp_rtt_ms"]["p50"] > 0
            assert e["ws_tick_to_receipt_ms"]["n"] > 0
            assert e["ws_msgs"] > 0
            for k in ["ws_msgs_per_s", "ws_max_gap_ms", "ws_reconnects",
                      "spread_bps_median", "depth_10bps_usdt_median"]:
                assert k in e, f"{ex} missing {k}"
        assert res["exchanges"]["binance"].get("divergence_bps") is None
        for ex in ["coinswitch", "coindcx"]:
            d = res["exchanges"][ex]["divergence_bps"]
            assert d is not None and "p50" in d and "p99" in d
            q = res["exchanges"][ex]["divergence_quantiles"]
            assert isinstance(q, list) and len(q) == 101

    def test_short_duration_validation(self, s):
        r = s.post(f"{API}/benchmark/run", json={"duration_s": 5, "symbols": ["BTC"]})
        assert r.status_code == 422

    def test_run_and_concurrent_409(self, s):
        # Ensure idle
        for _ in range(40):
            if not s.get(f"{API}/benchmark/status").json()["running"]:
                break
            time.sleep(1)
        r = s.post(f"{API}/benchmark/run", json={"duration_s": 10, "symbols": ["BTC"]})
        assert r.status_code == 200
        assert r.json()["symbols"] == ["BTC"]

        r2 = s.post(f"{API}/benchmark/run", json={"duration_s": 10, "symbols": ["BTC"]})
        assert r2.status_code == 409

        # partial should be present with running
        st = s.get(f"{API}/benchmark/status").json()
        assert st["running"] is True
        assert st["partial"] is not None
        elapsed_first = st["partial"].get("elapsed_s", 0)

        # Wait for completion
        for _ in range(30):
            time.sleep(1)
            st = s.get(f"{API}/benchmark/status").json()
            if not st["running"]:
                break
        assert st["running"] is False
        assert st["result"]["duration_s"] == 10
        assert st["result"]["symbols"] == ["BTC"]


# --- Verdict -------------------------------------------------------------------
class TestVerdict:
    def test_default(self, s):
        r = s.get(f"{API}/benchmark/verdict")
        assert r.status_code == 200
        j = r.json()
        assert j["winner"] in ["coinswitch", "coindcx"]
        for k in ["winner_pair", "winner_label", "margin", "confidence", "any_qualified", "rows"]:
            assert k in j
        assert len(j["rows"]) == 2
        # sorted desc by score.total
        assert j["rows"][0]["score"]["total"] >= j["rows"][1]["score"]["total"]
        for row in j["rows"]:
            for k in ["exchange", "pair", "taker_fee_pct", "hurdle_pct", "hurdle_bps", "exec_rtt_est_ms",
                      "gate_feed", "gate_exec", "qualified", "opportunity_share", "opportunities_per_min",
                      "mean_excess_bps", "expected_inr_per_hour", "score", "flags"]:
                assert k in row, f"missing {k}"
            for s_k in ["net_edge", "fees", "latency", "book", "stability", "total"]:
                assert s_k in row["score"]
        # default coinswitch fee == 0.05
        cs = next(r for r in j["rows"] if r["exchange"] == "coinswitch")
        assert cs["taker_fee_pct"] == pytest.approx(0.05, rel=0.01)

    def test_custom_fees(self, s):
        r = s.get(f"{API}/benchmark/verdict", params={"coinswitch_fee_pct": 0.1, "coindcx_fee_pct": 0.02})
        assert r.status_code == 200
        j = r.json()
        dcx = next(r for r in j["rows"] if r["exchange"] == "coindcx")
        assert dcx["taker_fee_pct"] == pytest.approx(0.02, rel=0.01)
        # hurdle = 2*fee + slippage + extra = 0.04 + 0.03 + 0.02 = 0.09
        assert dcx["hurdle_pct"] == pytest.approx(0.09, abs=0.005)


# --- Telemetry / regressions ---------------------------------------------------
class TestRegression:
    def test_telemetry(self, s):
        j = s.get(f"{API}/telemetry").json()
        assert j["feed"]["source"] == "COINSWITCH_WS"
        assert j["telegram"]["enabled"] is True
        assert j["feed"]["rest_fallback"]["rate_limiter"] is None

    def test_endpoints_ok(self, s):
        for ep in ["/status", "/config", "/logs", "/telegram/status"]:
            assert s.get(f"{API}{ep}").status_code == 200

    def test_spreads_shape(self, s):
        j = s.get(f"{API}/spreads").json()
        assert len(j["items"]) == 10
        for it in j["items"]:
            assert isinstance(it.get("paused"), bool)
            assert len(it.get("heat", [])) == 60
            assert "near_hurdle_hits_60s" in it and "peak_ratio_60s" in it
