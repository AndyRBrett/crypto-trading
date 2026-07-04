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
    """Seed a trade store under tmp_path and cd into it (write_status globs cwd).

    write_status replays the log through Portfolio.from_trades (recomputing
    realized P&L), so the history must be coherent round trips — fabricated
    pnl values on positionless SELLs would replay as short opens instead.
    """
    s = Storage(os.path.join(tmp_path, "trading.test.db"))
    day = 86_400
    # 7d window: two closed round trips, one win -> win_rate 0.5, low sample.
    # Fees are 0 in these fixtures, so realized = (exit - entry) * qty.
    s.save_trade(_trade(now - 2.1 * day, "BUY", price=100.0))
    s.save_trade(_trade(now - 2.0 * day, "SELL", price=96.0))    # -4
    s.save_trade(_trade(now - 1.1 * day, "BUY", price=100.0))
    s.save_trade(_trade(now - 1.0 * day, "SELL", price=110.0))   # +10
    # Older closes land only in the 30d / 90d windows.
    s.save_trade(_trade(now - 20.1 * day, "BUY", price=100.0))
    s.save_trade(_trade(now - 20 * day, "SELL", price=105.0))    # +5
    s.save_trade(_trade(now - 60.1 * day, "BUY", price=100.0))
    s.save_trade(_trade(now - 60 * day, "SELL", price=120.0))    # +20
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
    assert status["trades"] == 5            # 2 round trips within 7d + run-window BUY
    assert status["pnl"] == 6.0             # 10 - 4
    assert status["win_rate"] == 0.5        # 1 of 2 closes profitable
    assert status["win_rate_low_sample"] is True

    assert status["trades_30d"] == 7
    assert status["pnl_30d"] == 11.0        # 10 - 4 + 5
    assert status["trades_90d"] == 9
    assert status["pnl_90d"] == 31.0        # 10 - 4 + 5 + 20

    assert status["signals_evaluated"] == 2  # BUY + HOLD this run
    assert status["signals_acted"] == 1      # only the BUY became a fill
    assert status["errors"] == []


def test_low_sample_flag_clears_with_enough_trades(tmp_path):
    now = 1_700_000_000.0
    s = Storage(os.path.join(str(tmp_path), "trading.test.db"))
    for i in range(10):  # ten profitable round trips in the 7d window
        s.save_trade(_trade(now - 3600 * (i + 1) - 600, "BUY", price=100.0))
        s.save_trade(_trade(now - 3600 * (i + 1), "SELL", price=101.0))
    s.close()
    os.chdir(str(tmp_path))

    status = write_status.collect_metrics(now)
    assert status["win_rate"] == 1.0
    assert "win_rate_low_sample" not in status


def test_short_cover_counts_as_closed_trade(tmp_path):
    """Regression: a short's P&L realizes on the covering BUY leg. The old
    SELL-only accounting missed short covers entirely (and misread the short
    open as a close), so a losing short week looked flat."""
    now = 1_700_000_000.0
    day = 86_400
    s = Storage(os.path.join(str(tmp_path), "trading.short.db"))
    # Short 1 @ 100, cover @ 90: +10 realized on the BUY leg.
    s.save_trade(_trade(now - 2 * day, "SELL", price=100.0))
    s.save_trade(_trade(now - 1 * day, "BUY", price=90.0))
    # Short 1 @ 100, cover @ 105: -5 realized on the BUY leg.
    s.save_trade(_trade(now - 2 * day, "SELL", price=100.0, product="ETH-USD"))
    s.save_trade(_trade(now - 1 * day, "BUY", price=105.0, product="ETH-USD"))
    s.close()
    os.chdir(str(tmp_path))

    status = write_status.collect_metrics(now)
    assert status["pnl"] == 5.0        # +10 - 5, both realized on BUY covers
    assert status["win_rate"] == 0.5   # 1 of the 2 covers profitable


def test_stale_formula_pnl_is_normalized_by_replay(tmp_path):
    """Regression: rows logged before the 2026-06-18 fee-accounting fix carry
    realized_pnl without the entry-fee share; the status must report the
    replayed (current-formula) value, not the stale stored one."""
    now = 1_700_000_000.0
    day = 86_400
    s = Storage(os.path.join(str(tmp_path), "trading.stale.db"))
    buy = _trade(now - 2 * day, "BUY", price=100.0)
    buy.fee = 2.0
    s.save_trade(buy)
    # Stored pnl fabricated as the old formula's answer (exit-fee only:
    # 10 - 2 = 8); the replay must yield 10 - 2 (exit) - 2 (entry share) = 6.
    sell = _trade(now - 1 * day, "SELL", price=110.0, pnl=8.0)
    sell.fee = 2.0
    s.save_trade(sell)
    s.close()
    os.chdir(str(tmp_path))

    status = write_status.collect_metrics(now)
    assert status["pnl"] == 6.0  # replayed, not the stale stored 8.0


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


def test_risk_metrics_from_equity_curve(tmp_path):
    now = 2_000_000_000.0
    day = 86_400
    s = Storage(os.path.join(str(tmp_path), "trading.risk.db"))
    # Daily equity snapshots over a week: a wobble with one losing day, so all of
    # Sharpe / Sortino / max drawdown are defined.
    eqs = [10_000, 10_100, 10_050, 10_200, 10_150, 10_300, 10_250, 10_400]
    for i, eq in enumerate(eqs):
        s.conn.execute(
            "INSERT INTO equity(timestamp, cash, market_value, equity) VALUES (?,?,?,?)",
            (now - (len(eqs) - i) * day, eq, 0.0, eq),
        )
    s.conn.commit()
    s.close()
    os.chdir(str(tmp_path))

    rm = write_status.collect_metrics(now)["risk_metrics"]
    assert rm["window_days"] == 30
    assert rm["samples"] == len(eqs) - 1
    assert "sharpe" in rm and "sortino" in rm
    # Worst peak-to-trough is 10,100 -> 10,050 (each dip recovers to a new high).
    assert rm["max_drawdown_pct"] == round(abs(10_050 / 10_100 - 1) * 100, 2)


def test_no_risk_metrics_without_enough_equity(tmp_path):
    now = 2_000_000_000.0
    s = Storage(os.path.join(str(tmp_path), "trading.thin.db"))
    s.conn.execute(
        "INSERT INTO equity(timestamp, cash, market_value, equity) VALUES (?,?,?,?)",
        (now - 86_400, 10_000.0, 0.0, 10_000.0),
    )
    s.conn.commit()
    s.close()
    os.chdir(str(tmp_path))
    # A single snapshot isn't enough to measure variance -> metric omitted.
    assert "risk_metrics" not in write_status.collect_metrics(now)


def test_decisions_surface_threshold_distance_on_hold(tmp_path):
    now = 2_000_000_000.0
    s = Storage(os.path.join(str(tmp_path), "trading.thr.db"))
    s.save_signal(
        now - 60, "BTC-USD", "HOLD", 100.0, "no crossover", outcome="hold",
        features={"thresholds": {"ma_gap_pct": -0.5, "rsi_to_overbought": 21.0}},
    )
    s.close()
    os.chdir(str(tmp_path))
    decisions = write_status.collect_metrics(now)["decisions"]
    assert decisions[0]["thresholds"] == {"ma_gap_pct": -0.5, "rsi_to_overbought": 21.0}


def test_missing_store_is_an_error(tmp_path):
    os.chdir(str(tmp_path))
    status = write_status.collect_metrics(1_700_000_000.0)
    assert status["errors"]
    assert "trades" not in status  # nothing readable, so counts are omitted
    assert status["signals_acted"] == 0
