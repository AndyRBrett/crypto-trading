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
