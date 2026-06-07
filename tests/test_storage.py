import os
import tempfile

from bot.storage import Storage


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
