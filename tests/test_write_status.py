import os

from bot.portfolio import Trade
from bot.storage import Storage

import write_status


def _trade(ts, side, pnl=0.0, price=100.0, product="BTC-USD", qty=1.0):
    return Trade(
        timestamp=ts, product_id=product, side=side, price=price,
        quantity=qty, fee=0.0, cash_after=0.0, realized_pnl=pnl,
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


def test_benchmark_and_equity_curve(tmp_path):
    now = 2_000_000_000.0
    day = 86_400
    s = Storage(os.path.join(str(tmp_path), "trading.bench.db"))
    # One round trip in BTC: deploy $100 of notional, realize +$5.
    s.save_trade(_trade(now - 5 * day, "BUY", price=100.0))
    s.save_trade(_trade(now - 2 * day, "SELL", pnl=5.0, price=105.0))
    # Per-tick marks frame the window: BTC ran 100 -> 110 (buy-and-hold +10%).
    s.save_signal(now - 5 * day, "BTC-USD", "BUY", 100.0, "entry")
    s.save_signal(now - 60, "BTC-USD", "HOLD", 110.0, "hold")
    # Equity snapshots (timestamp isn't settable via save_equity, so insert direct).
    s.conn.execute(
        "INSERT INTO equity(timestamp, cash, market_value, equity) VALUES (?,?,?,?)",
        (now - 5 * day, 1000.0, 0.0, 1000.0),
    )
    s.conn.execute(
        "INSERT INTO equity(timestamp, cash, market_value, equity) VALUES (?,?,?,?)",
        (now - 60, 1010.0, 0.0, 1010.0),
    )
    s.conn.commit()
    s.close()
    os.chdir(str(tmp_path))

    status = write_status.collect_metrics(now)

    bm = status["benchmark"]
    assert bm["deployed_notional"] == 100.0
    assert bm["strategy_pnl"] == 5.0
    assert bm["buy_hold_pnl"] == 10.0          # 100 * (110/100 - 1)
    assert bm["strategy_return_pct"] == 5.0
    assert bm["buy_hold_return_pct"] == 10.0
    assert bm["alpha_pct"] == -5.0             # strategy trailed buy-and-hold

    curve = status["equity_curve"]
    assert len(curve) == 2
    assert curve[0]["equity"] == 1000.0 and curve[-1]["equity"] == 1010.0


def test_no_benchmark_without_deployed_capital(tmp_path):
    now = 2_000_000_000.0
    s = Storage(os.path.join(str(tmp_path), "trading.flat.db"))
    s.save_signal(now - 60, "BTC-USD", "HOLD", 100.0, "no signal")
    s.close()
    os.chdir(str(tmp_path))
    status = write_status.collect_metrics(now)
    assert "benchmark" not in status  # nothing bought in the window to benchmark


def test_decision_log_and_rejection_reasons(tmp_path):
    now = 2_000_000_000.0
    s = Storage(os.path.join(str(tmp_path), "trading.dec.db"))
    s.save_signal(now - 60, "BTC-USD", "BUY", 100.0, "entry",
                  outcome="acted", slippage_bps=20.0)
    s.save_signal(now - 60, "ETH-USD", "HOLD", 50.0, "no signal", outcome="hold")
    s.save_signal(now - 60, "SOL-USD", "BUY", 10.0, "at cap",
                  outcome="rejected", reject_code="max_open_positions")
    s.save_signal(now - 60, "ADA-USD", "BUY", 1.0, "no cash",
                  outcome="rejected", reject_code="insufficient_balance")
    s.close()
    os.chdir(str(tmp_path))

    status = write_status.collect_metrics(now)
    assert status["signals_evaluated"] == 4
    assert len(status["decisions"]) == 4
    assert status["rejection_reasons"] == {
        "max_open_positions": 1,
        "insufficient_balance": 1,
    }
    assert status["avg_slippage_bps"] == 20.0


def test_missing_store_is_an_error(tmp_path):
    os.chdir(str(tmp_path))
    status = write_status.collect_metrics(1_700_000_000.0)
    assert status["errors"]
    assert "trades" not in status  # nothing readable, so counts are omitted
    assert status["signals_acted"] == 0
