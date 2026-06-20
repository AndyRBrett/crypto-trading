import os

from bot.portfolio import Trade
from bot.storage import Storage

import write_status


def _trade(ts, side, pnl=0.0):
    return Trade(
        timestamp=ts, product_id="BTC-USD", side=side, price=100.0,
        quantity=1.0, fee=0.0, cash_after=0.0, realized_pnl=pnl,
    )


def _store_in(tmp_path, now):
    """Seed a trade store under tmp_path and cd into it (write_status globs cwd)."""
    s = Storage(os.path.join(tmp_path, "trading.test.db"))
    day = 86_400
    # 7d window: two closed trades, one win -> win_rate 0.5, low sample.
    s.save_trade(_trade(now - 1 * day, "BUY"))
    s.save_trade(_trade(now - 1 * day, "SELL", pnl=10.0))
    s.save_trade(_trade(now - 2 * day, "SELL", pnl=-4.0))
    # Older closes land only in the 30d / 90d windows.
    s.save_trade(_trade(now - 20 * day, "SELL", pnl=5.0))
    s.save_trade(_trade(now - 60 * day, "SELL", pnl=20.0))
    # A fill inside the run window counts as a signal that was acted on.
    s.save_trade(_trade(now - 60, "BUY"))
    s.save_signal(now - 60, "BTC-USD", "BUY", 100.0, "crossover")
    s.save_signal(now - 60, "ETH-USD", "HOLD", 50.0, "no signal")
    s.close()
    os.chdir(tmp_path)


def test_windows_and_low_sample(tmp_path):
    now = 1_700_000_000.0
    _store_in(str(tmp_path), now)
    status = write_status.collect_metrics(now)

    assert status["window_days"] == 7
    assert status["trades"] == 4            # 3 within 7d + the run-window BUY
    assert status["pnl"] == 6.0             # 10 - 4
    assert status["win_rate"] == 0.5        # 1 of 2 closes profitable
    assert status["win_rate_low_sample"] is True

    assert status["trades_30d"] == 5
    assert status["pnl_30d"] == 11.0        # 10 - 4 + 5
    assert status["trades_90d"] == 6
    assert status["pnl_90d"] == 31.0        # 10 - 4 + 5 + 20

    assert status["signals_evaluated"] == 2  # BUY + HOLD this run
    assert status["signals_acted"] == 1      # only the BUY became a fill
    assert status["errors"] == []


def test_low_sample_flag_clears_with_enough_trades(tmp_path):
    now = 1_700_000_000.0
    s = Storage(os.path.join(str(tmp_path), "trading.test.db"))
    for i in range(10):  # ten closed trades in the 7d window
        s.save_trade(_trade(now - 3600 * (i + 1), "SELL", pnl=1.0))
    s.close()
    os.chdir(str(tmp_path))

    status = write_status.collect_metrics(now)
    assert status["win_rate"] == 1.0
    assert "win_rate_low_sample" not in status


def test_missing_store_is_an_error(tmp_path):
    os.chdir(str(tmp_path))
    status = write_status.collect_metrics(1_700_000_000.0)
    assert status["errors"]
    assert "trades" not in status  # nothing readable, so counts are omitted
    assert status["signals_acted"] == 0
