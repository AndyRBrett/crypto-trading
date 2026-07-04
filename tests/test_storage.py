import json
import os
import tempfile

from bot.portfolio import Portfolio
from bot.storage import Storage, export_combined_state


def test_signal_log_roundtrip():
    tmp = tempfile.mkdtemp()
    s = Storage(os.path.join(tmp, "t.db"))
    s.save_signal(1.0, "BTC-USD", "HOLD", 100.0, "no crossover")
    s.save_signal(2.0, "ETH-USD", "BUY", 50.0, "bullish crossover")
    acts = s.load_activity()
    assert len(acts) == 2
    # Newest first.
    assert acts[0]["product_id"] == "ETH-USD" and acts[0]["action"] == "BUY"
    assert acts[1]["reason"] == "no crossover"
    s.close()


def test_signal_log_records_outcome_and_slippage():
    tmp = tempfile.mkdtemp()
    s = Storage(os.path.join(tmp, "t.db"))
    s.save_signal(
        1.0, "BTC-USD", "BUY", 100.0, "bullish crossover",
        outcome="acted", reject_code="", slippage_bps=12.5,
    )
    s.save_signal(
        2.0, "ETH-USD", "BUY", 50.0, "at max positions",
        outcome="rejected", reject_code="max_open_positions",
    )
    acts = s.load_activity()
    assert acts[0]["reject_code"] == "max_open_positions"
    assert acts[0]["outcome"] == "rejected"
    assert acts[0]["slippage_bps"] is None  # nothing filled
    assert acts[1]["outcome"] == "acted"
    assert acts[1]["slippage_bps"] == 12.5
    s.close()


def test_save_signal_defaults_stay_backward_compatible():
    tmp = tempfile.mkdtemp()
    s = Storage(os.path.join(tmp, "t.db"))
    s.save_signal(1.0, "BTC-USD", "HOLD", 100.0, "no crossover")  # legacy 5-arg call
    act = s.load_activity()[0]
    assert act["outcome"] == "" and act["reject_code"] == "" and act["slippage_bps"] is None
    assert act["features"] == {}  # parsed back as an empty dict, not the raw "{}"
    s.close()


def test_signal_log_persists_feature_snapshot():
    tmp = tempfile.mkdtemp()
    s = Storage(os.path.join(tmp, "t.db"))
    feats = {
        "indicators": {"rsi": 48.0, "atr": 12.3},
        "thresholds": {"ma_gap_pct": -0.42, "rsi_to_overbought": 22.0},
        "strength": 0.0,
    }
    s.save_signal(1.0, "BTC-USD", "HOLD", 100.0, "no crossover", features=feats)
    act = s.load_activity()[0]
    # The whole snapshot round-trips as structured JSON, queryable for tuning.
    assert act["features"]["thresholds"]["ma_gap_pct"] == -0.42
    assert act["features"]["indicators"]["rsi"] == 48.0
    s.close()


def test_migrates_legacy_signal_log_without_decision_columns():
    import sqlite3

    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "legacy.db")
    # Simulate a store created before the decision-log columns existed.
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE signal_log (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "timestamp REAL NOT NULL, product_id TEXT NOT NULL, action TEXT NOT NULL, "
        "price REAL NOT NULL, reason TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO signal_log(timestamp, product_id, action, price, reason) "
        "VALUES (1.0, 'BTC-USD', 'HOLD', 100.0, 'old row')"
    )
    conn.commit()
    conn.close()

    # Opening through Storage migrates the table in place; old rows survive and
    # new writes carry the decision-log fields.
    s = Storage(db)
    s.save_signal(2.0, "ETH-USD", "BUY", 50.0, "new row", outcome="acted", slippage_bps=3.0,
                  features={"thresholds": {"ma_gap_pct": 0.1}})
    acts = s.load_activity()
    assert acts[1]["reason"] == "old row" and acts[1]["outcome"] == ""
    assert acts[1]["features"] == {}  # migrated old row backfills the default
    assert acts[0]["outcome"] == "acted" and acts[0]["slippage_bps"] == 3.0
    assert acts[0]["features"]["thresholds"]["ma_gap_pct"] == 0.1
    s.close()


def test_load_activity_respects_limit():
    tmp = tempfile.mkdtemp()
    s = Storage(os.path.join(tmp, "t.db"))
    for i in range(10):
        s.save_signal(float(i), "BTC-USD", "HOLD", 100.0 + i, f"tick {i}")
    acts = s.load_activity(limit=3)
    assert len(acts) == 3
    assert acts[0]["reason"] == "tick 9"  # most recent
    s.close()


def test_account_state_block_shape():
    tmp = tempfile.mkdtemp()
    s = Storage(os.path.join(tmp, "t.db"))
    pf = Portfolio(starting_cash=10000.0, fee_rate=0.0)
    pf.execute("BUY", "BTC-USD", 100.0, 1.0)
    block = s.account_state(
        pf, {"BTC-USD": 110.0}, {"BTC-USD": {"action": "HOLD"}},
        name="trend", strategy="ema_crossover", products=["BTC-USD"],
    )
    assert block["name"] == "trend"
    assert block["strategy"] == "ema_crossover"
    assert block["products"] == ["BTC-USD"]
    assert block["positions"][0]["product_id"] == "BTC-USD"
    assert "equity" in block and "trades" in block
    # No shared market data leaks into the per-account block.
    assert "prices" not in block and "price_history" not in block
    s.close()


def test_export_combined_state_writes_unified_shape():
    tmp = tempfile.mkdtemp()
    s = Storage(os.path.join(tmp, "t.db"))
    pf1 = Portfolio(starting_cash=10000.0, fee_rate=0.0)
    pf2 = Portfolio(starting_cash=5000.0, fee_rate=0.0)
    b1 = s.account_state(pf1, {}, {}, name="a", strategy="ema_crossover", products=["BTC-USD"])
    b2 = s.account_state(pf2, {}, {}, name="b", strategy="donchian_breakout", products=["ETH-USD"])
    out = os.path.join(tmp, "state.json")
    export_combined_state(out, [b1, b2], {"BTC-USD": 100.0}, {"BTC-USD": []})
    state = json.loads(open(out).read())
    assert state["schema"] == "multi-account-v1"
    assert len(state["accounts"]) == 2
    assert state["products"] == ["BTC-USD", "ETH-USD"]
    assert state["portfolio_total"]["starting_cash"] == 15000.0
    assert state["prices"] == {"BTC-USD": 100.0}
    s.close()


def test_account_state_stats_block():
    with tempfile.TemporaryDirectory() as d:
        s = Storage(os.path.join(d, "t.db"))
        p = Portfolio(10_000, 0.0)
        # Long round trip: +10 win. Short round trip: -5 loss (realized on the
        # covering BUY — stats must be direction-agnostic).
        p.execute("BUY", "BTC-USD", 100.0, 1.0, timestamp=1)
        p.execute("SELL", "BTC-USD", 110.0, 1.0, timestamp=2)
        p.execute("SELL", "ETH-USD", 100.0, 1.0, timestamp=3)
        p.execute("BUY", "ETH-USD", 105.0, 1.0, timestamp=4)
        block = s.account_state(p, {"BTC-USD": 110.0, "ETH-USD": 105.0}, {})
        st = block["stats"]
        assert st["fills"] == 4
        assert st["round_trips"] == 2
        assert st["wins"] == 1 and st["losses"] == 1
        assert st["win_rate"] == 0.5
        assert st["profit_factor"] == 2.0   # 10 gross win / 5 gross loss
        assert st["avg_win"] == 10.0 and st["avg_loss"] == -5.0
        assert st["fees_paid"] == 0.0
        s.close()


def test_export_combined_state_aggregates_exposure():
    with tempfile.TemporaryDirectory() as d:
        s = Storage(os.path.join(d, "t.db"))
        long_p = Portfolio(10_000, 0.0)
        long_p.execute("BUY", "BTC-USD", 100.0, 10.0, timestamp=1)   # +$1200 @ 120
        short_p = Portfolio(10_000, 0.0)
        short_p.execute("SELL", "ETH-USD", 50.0, 4.0, timestamp=1)   # -$180 @ 45
        prices = {"BTC-USD": 120.0, "ETH-USD": 45.0}
        blocks = [
            s.account_state(long_p, prices, {}, name="a", products=["BTC-USD"]),
            s.account_state(short_p, prices, {}, name="b", products=["ETH-USD"]),
        ]
        path = os.path.join(d, "state.json")
        export_combined_state(path, blocks, prices)
        state = json.load(open(path))
        t = state["portfolio_total"]
        assert t["gross_long"] == 1200.0
        assert t["gross_short"] == 180.0
        assert t["net_exposure"] == 1020.0
        assert t["exposure_by_asset"] == {"BTC-USD": 1200.0, "ETH-USD": -180.0}
        s.close()
