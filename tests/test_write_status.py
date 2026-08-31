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


def test_merge_equity_seeds_from_pre_window_snapshots():
    """Regression (2026-06-29 artifact): clipping each store to the reporting
    window *before* merging left the forward-fill with no seed, so each store
    contributed $0 until its first in-window snapshot landed — on a 5-store
    book the curve opened at one store's $10k and "5x'd" to $49.5k. Snapshots
    from before ``clip_start`` must seed the fill; only points at-or-after it
    are emitted."""
    a = [(50.0, 100.0), (110.0, 101.0), (120.0, 102.0)]
    b = [(60.0, 200.0), (111.0, 201.0)]
    curve = write_status._merge_equity([a, b], clip_start=100.0)
    assert curve == [(110.0, 301.0), (111.0, 302.0), (120.0, 303.0)]


def test_merge_equity_drops_retired_store():
    """A store whose last snapshot predates the curve start is retired (e.g.
    the legacy single-account trading.db), not merely quiet — it must not
    forward-fill stale equity into the whole curve."""
    live = [(50.0, 100.0), (110.0, 101.0)]
    retired = [(10.0, 500.0), (20.0, 500.0)]
    assert write_status._merge_equity([live, retired], clip_start=100.0) == [(110.0, 101.0)]
    # Same rule under the risk window's common-start clip: without it the dead
    # store's constant equity inflates the whole risk curve's level, muting
    # drawdown/volatility percentages.
    assert write_status._merge_equity([live, retired], clip_to_common_start=True) == [
        (50.0, 100.0),
        (110.0, 101.0),
    ]


def test_equity_curve_first_point_sums_all_live_stores(tmp_path):
    """End-to-end regression for the 2026-06-29 $10k -> $49.5k status jump:
    the headline curve's first point must already sum every live store (each
    seeded from its last pre-window snapshot), not ramp up store-by-store."""
    now = 2_000_000_000.0
    day = 86_400
    # Two stores whose first in-window snapshots land at different times; both
    # have pre-window history to seed from.
    for name, first_in_window, eq in (
        ("a", now - 5 * day, 1000.0),
        ("b", now - 4 * day, 2000.0),
    ):
        s = Storage(os.path.join(str(tmp_path), f"trading.{name}.db"))
        for ts in (now - 20 * day, first_in_window, now - 60):
            s.conn.execute(
                "INSERT INTO equity(timestamp, cash, market_value, equity) VALUES (?,?,?,?)",
                (ts, eq, 0.0, eq),
            )
        s.conn.commit()
        s.close()
    os.chdir(str(tmp_path))

    curve = write_status.collect_metrics(now)["equity_curve"]
    # Only in-window timestamps are emitted (3 distinct), and every point sums
    # both stores — no cold-start ramp.
    assert len(curve) == 3
    assert [p["equity"] for p in curve] == [3000.0, 3000.0, 3000.0]


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


def test_stale_equity_skip_is_surfaced_as_an_error(tmp_path):
    # engine.tick() persists last_equity_skip_at/_products when it skips a
    # snapshot for lack of a fresh price (bot/engine.py); until a later tick
    # snapshots successfully and clears it, that store's contribution to the
    # merged equity curve is flatlining with no other signal of why (issue
    # #50 — a 25h price freeze that read as a quiet market, not a fault).
    now = 1_700_000_000.0
    _store_in(str(tmp_path), now)
    s = Storage(os.path.join(str(tmp_path), "trading.test.db"))
    s.set_meta("last_equity_skip_at", str(now - 60))
    s.set_meta("last_equity_skip_products", "ETH-USD")
    s.close()

    status = write_status.collect_metrics(now)
    assert any("ETH-USD" in e and "stale" in e for e in status["errors"])


def test_cleared_equity_skip_is_not_surfaced(tmp_path):
    now = 1_700_000_000.0
    _store_in(str(tmp_path), now)
    s = Storage(os.path.join(str(tmp_path), "trading.test.db"))
    s.set_meta("last_equity_skip_at", "")
    s.close()

    status = write_status.collect_metrics(now)
    # Named rather than `== []`, so an unrelated error fails its own test
    # instead of this one.
    assert not any("equity snapshot stale" in e for e in status["errors"])


def test_unreadable_skip_stamp_still_reports(tmp_path):
    # The alarm must not go quiet on its own error path. A store stuck with a
    # corrupt stamp is still stuck, and reporting nothing rebuilds exactly the
    # bug this whole block exists to catch: a live fault with `errors: []`.
    now = 1_700_000_000.0
    _store_in(str(tmp_path), now)
    s = Storage(os.path.join(str(tmp_path), "trading.test.db"))
    s.set_meta("last_equity_skip_at", "not-a-timestamp")
    s.set_meta("last_equity_skip_products", "ETH-USD")
    s.close()

    status = write_status.collect_metrics(now)
    assert any("ETH-USD" in e and "stale" in e for e in status["errors"])


def test_a_frozen_store_is_what_the_curve_freeze_looked_like(tmp_path):
    # The symptom as filed (issue #50), not just the mechanism: SEVEN equity
    # points ~3-4h apart, byte-identical to the cent, with errors empty.
    #
    # It looks impossible for a skipped snapshot to produce ROWS — a skip writes
    # none. _merge_equity is why it does: at each observed timestamp it
    # forward-fills every store's last snapshot and sums. So a store that stops
    # snapshotting is carried forward at a fixed value while another store's
    # ticks keep supplying timestamps, and the total advances in time without
    # moving. That is the curve in the issue, and this pins it.
    now = 1_700_000_000.0
    _store_in(str(tmp_path), now)

    frozen = Storage(os.path.join(str(tmp_path), "trading.frozen.db"))
    frozen.save_equity(1000.0, 500.0, 1500.0)      # last good snapshot, then stuck
    frozen.set_meta("last_equity_skip_at", str(now - 25 * 3600))
    frozen.set_meta("last_equity_skip_products", "ETH-USD,SOL-USD")
    frozen.close()

    status = write_status.collect_metrics(now)
    stale = [e for e in status["errors"] if "equity snapshot stale" in e]
    assert stale, "a store frozen for 25h reported no error — the original bug"
    assert "ETH-USD,SOL-USD" in stale[0]


# --- signal proximity: quiet market vs tight thresholds (issue #38) ---------

def _dec(thresholds, reject_code="no_signal"):
    return {"product_id": "ETH-USD", "action": "HOLD", "outcome": "hold",
            "reject_code": reject_code, "thresholds": thresholds}


def test_proximity_is_none_without_thresholds():
    # Nothing to say → the key is absent rather than present and empty.
    assert write_status.signal_proximity([]) is None
    assert write_status.signal_proximity([{"outcome": "acted"}]) is None


def test_far_from_trigger_reads_as_quiet_market():
    # Price sat 5% from the trigger all window: the thresholds are fine, there
    # was simply nothing to trade. Widening them here would be the wrong fix.
    prox = write_status.signal_proximity([_dec({"breakout_dist_pct": -5.1}),
                                _dec({"breakout_dist_pct": -4.4})])
    assert prox["verdict"] == "quiet-market"
    assert prox["near_trigger"] == 0
    assert prox["metrics"]["breakout_dist_pct"]["closest"] == 4.4


def test_repeated_near_misses_flag_tight_thresholds():
    # Price kept grazing the trigger and never crossed — the strategy is watching
    # the right move and declining it. That IS a calibration problem.
    prox = write_status.signal_proximity([_dec({"breakout_dist_pct": -0.2}),
                                _dec({"breakout_dist_pct": -0.4})])
    assert prox["verdict"] == "thresholds-may-be-tight"
    assert prox["near_trigger"] == 2


def test_metrics_are_grouped_never_pooled():
    # THE UNIT-SAFETY TEST. rsi_to_overbought is in RSI points and
    # breakout_dist_pct is in percent; pooling them would average 39.5 against
    # 0.3 and produce a confident, meaningless "closest" figure.
    prox = write_status.signal_proximity([_dec({"breakout_dist_pct": -0.3,
                                      "rsi_to_overbought": 39.5})])
    assert set(prox["metrics"]) == {"breakout_dist_pct", "rsi_to_overbought"}
    assert prox["metrics"]["breakout_dist_pct"]["closest"] == 0.3
    assert prox["metrics"]["rsi_to_overbought"]["closest"] == 39.5


def test_point_denominated_metrics_do_not_vote_on_the_verdict():
    # An RSI sitting 0.2 POINTS from overbought is not a 0.2% near-miss, so it
    # must not trip the percentage-based verdict on its own.
    prox = write_status.signal_proximity([_dec({"rsi_to_overbought": 0.2})])
    assert "near_trigger" not in prox["metrics"]["rsi_to_overbought"]
    assert prox["verdict"] == "unknown", "no percentage gaps recorded → not provably quiet"


def test_in_position_decisions_are_measured_against_the_exit_edge():
    # Already holding: the trigger being waited on is the exit, not the entry.
    prox = write_status.signal_proximity([_dec({"exit_dist_pct": 0.1}, reject_code="in_position")])
    assert prox["metrics"]["exit_dist_pct"]["near_trigger"] == 1
    assert prox["verdict"] == "thresholds-may-be-tight"


def test_median_is_averaged_for_even_sample_counts():
    prox = write_status.signal_proximity([_dec({"ma_gap_pct": 1.0}), _dec({"ma_gap_pct": 2.0})])
    assert prox["metrics"]["ma_gap_pct"]["median"] == 1.5


def test_unparseable_threshold_values_are_skipped_not_fatal():
    prox = write_status.signal_proximity([_dec({"ma_gap_pct": "n/a", "breakout_dist_pct": -0.3})])
    assert "ma_gap_pct" not in prox["metrics"]
    assert prox["metrics"]["breakout_dist_pct"]["samples"] == 1


def test_real_status_payload_covers_every_decision():
    # Guards the bug found while building this: an earlier cut read only the
    # donchian keys and silently described 2 of 10 live decisions as though they
    # were all of them.
    import json as _json
    from pathlib import Path
    payload = _json.loads(
        (Path(__file__).resolve().parent.parent / "overseer-status.json").read_text())
    prox = write_status.signal_proximity(payload["decisions"])
    covered = sum(m["samples"] for m in prox["metrics"].values())
    expected = sum(len(d.get("thresholds") or {}) for d in payload["decisions"])
    assert covered == expected


# --- per-asset attribution (issue #51) -------------------------------------
#
# Portfolio P&L answers "how did we do" and cannot answer "which asset is
# costing us" — a book that is flat overall can be one asset bleeding into
# another one carrying it.

def test_realized_drawdown_is_dollars_not_a_fraction():
    # bot.metrics.max_drawdown returns equity/peak - 1, which needs a positive
    # base. A cumulative P&L curve starts at zero and can sit negative, where
    # that formula divides by zero or flips sign. This one is absolute.
    legs = [(1.0, 100.0), (2.0, -30.0), (3.0, -20.0), (4.0, 60.0)]
    # cumulative: 100, 70, 50, 110 -> peak 100, trough 50
    assert write_status.realized_drawdown(legs) == -50.0


def test_realized_drawdown_handles_a_book_that_never_wins():
    # Starts at zero and only falls: peak stays 0, so the drawdown is the whole
    # loss. The fractional formula would divide by a peak of 0 here.
    assert write_status.realized_drawdown([(1.0, -10.0), (2.0, -5.0)]) == -15.0
    assert write_status.realized_drawdown([]) == 0.0
    assert write_status.realized_drawdown([(1.0, 5.0), (2.0, 5.0)]) == 0.0


def test_realized_drawdown_sorts_before_walking():
    # Store-ordered rows must not produce a different answer from time-ordered
    # ones — a caller shouldn't be able to get a subtly wrong number for free.
    ordered = [(1.0, 100.0), (2.0, -80.0), (3.0, 40.0)]
    shuffled = [ordered[2], ordered[0], ordered[1]]
    assert write_status.realized_drawdown(shuffled) == write_status.realized_drawdown(ordered)
    assert write_status.realized_drawdown(ordered) == -80.0


def test_attribution_splits_pnl_by_asset_and_window():
    now = 1_700_000_000.0
    day = 86_400
    windows = (7, 30, 90)
    starts = {d: now - d * day for d in windows}
    legs = [
        (now - 2 * day,  "BTC-USD", 100.0),   # in every window
        (now - 40 * day, "BTC-USD", -250.0),  # 90d only
        (now - 1 * day,  "ETH-USD", -60.0),
    ]
    out = write_status.attribution(legs, starts, windows)

    assert out["BTC-USD"]["7d"]["pnl"] == 100.0
    assert out["BTC-USD"]["90d"]["pnl"] == -150.0
    assert out["BTC-USD"]["90d"]["closed"] == 2
    # The winner over 7d is the loser over 90d — the whole point of the split.
    assert out["ETH-USD"]["7d"]["pnl"] == -60.0
    # A window with no closed trade for an asset is absent, not zero.
    assert "7d" in out["ETH-USD"] and out["ETH-USD"]["7d"]["closed"] == 1


def test_attribution_omits_assets_with_nothing_closed():
    # A symbol the strategy never closed is not a flat performer, and reporting
    # it as 0.00 would put it in a table next to real results.
    assert write_status.attribution([], {7: 0.0}, (7,)) == {}


def test_attribution_reaches_the_status_file(tmp_path):
    now = 1_700_000_000.0
    _store_in(str(tmp_path), now)
    status = write_status.collect_metrics(now)
    assert "attribution" in status, "per-asset breakdown missing from the status file"
    for per_window in status["attribution"].values():
        for row in per_window.values():
            assert set(row) == {"pnl", "closed", "realized_drawdown"}
