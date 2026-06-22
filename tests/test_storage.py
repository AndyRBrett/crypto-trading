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
    s.save_signal(2.0, "ETH-USD", "BUY", 50.0, "new row", outcome="acted", slippage_bps=3.0)
    acts = s.load_activity()
    assert acts[1]["reason"] == "old row" and acts[1]["outcome"] == ""
    assert acts[0]["outcome"] == "acted" and acts[0]["slippage_bps"] == 3.0
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
