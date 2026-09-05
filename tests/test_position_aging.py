"""Tests for the position aging / rotation cap (issue #52).

A trade that neither stops out nor reaches its target is otherwise held forever,
and a full book rejects every new signal with ``in_position``. These cover the
pure rule (bot/risk.py), the engine and backtester wiring, and the overseer's
``exit_reasons`` tally.
"""

import json
import time

from bot import risk
from bot.config import Config
from bot.engine import ACTED, IN_POSITION, Engine
from bot.strategy import BUY, HOLD, SELL, Signal

from tests.test_engine import FakeExplainer, FakeStorage

DAY = 86_400


def _cfg(**overrides):
    cfg = Config()
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


# -- the rule ---------------------------------------------------------------


def test_disabled_by_default():
    now = time.time()
    # Held a year, flat: still no aging exit until max_hold_days is configured.
    assert risk.aging_exit_reason(
        _cfg(), opened_at=now - 365 * DAY, now=now, entry=100.0, price=100.0
    ) is None


def test_closes_a_stale_flat_position():
    now = time.time()
    cfg = _cfg(max_hold_days=10)
    reason = risk.aging_exit_reason(cfg, now - 11 * DAY, now, entry=100.0, price=100.5)
    assert reason and reason.startswith("Position aging")
    assert "11.0 days" in reason
    # Inside the limit -> untouched.
    assert risk.aging_exit_reason(cfg, now - 9 * DAY, now, 100.0, 100.5) is None


def test_the_reprieve_needs_a_meaningful_gain():
    """Merely green isn't enough: +0.5% after a month is the stale hold this
    cap exists to rotate out, so the default threshold is a real gain."""
    now = time.time()
    cfg = _cfg(max_hold_days=10)  # default threshold
    assert cfg.max_hold_min_gain_pct > 0
    assert risk.aging_exit_reason(cfg, now - 30 * DAY, now, 100.0, 100.5) is not None
    assert risk.aging_exit_reason(cfg, now - 30 * DAY, now, 100.0, 110.0) is None


def test_a_winner_is_left_to_the_trailing_stop():
    now = time.time()
    cfg = _cfg(max_hold_days=10, max_hold_min_gain_pct=0.05)
    # +8% after 30 days: working, so it keeps its slot.
    assert risk.aging_exit_reason(cfg, now - 30 * DAY, now, 100.0, 108.0) is None
    # +2% after 30 days: not going anywhere, rotate it out.
    assert risk.aging_exit_reason(cfg, now - 30 * DAY, now, 100.0, 102.0) is not None
    # A loser is always rotated out once aged.
    assert risk.aging_exit_reason(cfg, now - 30 * DAY, now, 100.0, 95.0) is not None


def test_gain_is_direction_aware():
    now = time.time()
    cfg = _cfg(max_hold_days=10, max_hold_min_gain_pct=0.05)
    # A short at 100 with price at 90 is +10%: working, keeps its slot.
    assert risk.aging_exit_reason(
        cfg, now - 30 * DAY, now, 100.0, 90.0, direction="short"
    ) is None
    # The same price is -10% for a long, so the long ages out.
    assert risk.aging_exit_reason(cfg, now - 30 * DAY, now, 100.0, 90.0) is not None


def test_never_reads_as_a_stop_out():
    """An aged-out exit must not start the post-stop re-entry cooldown, or the
    freed capital couldn't be redeployed — which is the whole point."""
    now = time.time()
    reason = risk.aging_exit_reason(_cfg(max_hold_days=1), now - 5 * DAY, now, 100.0, 99.0)
    assert not reason.startswith(risk.STOP_REASON_PREFIX)


def test_flat_or_unknown_entry_is_ignored():
    now = time.time()
    cfg = _cfg(max_hold_days=1)
    assert risk.aging_exit_reason(cfg, None, now, 100.0, 100.0) is None
    assert risk.aging_exit_reason(cfg, now - 5 * DAY, now, 0.0, 100.0) is None


# -- engine -----------------------------------------------------------------


def _engine(**cfg_overrides):
    return Engine(
        _cfg(**cfg_overrides),
        market_data=object(),
        storage=FakeStorage(),
        explainer=FakeExplainer(),
    )


def _signal(action, price=1000.0, atr=50.0, product="BTC-USD"):
    return Signal(
        product_id=product, action=action, price=price,
        indicators={"atr": atr}, reasons=["test reason"],
    )


def test_engine_rotates_a_stale_holding_out():
    now = time.time()
    eng = _engine(starting_cash=10_000, max_hold_days=5, trailing_stop=False)
    eng.portfolio.execute(BUY, "BTC-USD", 1000.0, 1.0, timestamp=now - 30 * DAY)
    # A HOLD on a stale, going-nowhere position now closes it instead of
    # reporting in_position for the rest of time.
    trade, code = eng._manage(_signal(HOLD, price=1005.0), 1005.0, [], prices={})
    assert trade is not None and code == ACTED
    assert trade.side == SELL and trade.quantity == 1.0
    assert trade.reasons[0].startswith("Position aging")


def test_engine_leaves_a_fresh_holding_alone():
    now = time.time()
    eng = _engine(starting_cash=10_000, max_hold_days=5, trailing_stop=False)
    eng.portfolio.execute(BUY, "BTC-USD", 1000.0, 1.0, timestamp=now - 2 * DAY)
    trade, code = eng._manage(_signal(HOLD, price=1005.0), 1005.0, [], prices={})
    assert trade is None and code == IN_POSITION


def test_engine_prefers_a_stop_over_an_aging_exit():
    """Stops and targets are checked first — the reason recorded must be the
    real one, since the re-entry cooldown keys off the stop prefix."""
    now = time.time()
    eng = _engine(starting_cash=10_000, max_hold_days=1, trailing_stop=False)
    eng.portfolio.execute(BUY, "BTC-USD", 1000.0, 1.0, timestamp=now - 30 * DAY)
    candles = [{"time": 0, "high": 1010, "low": 990}]
    # Price below the 2-ATR stop: stop-loss wins over the aging cap.
    trade, code = eng._manage(_signal(HOLD, price=800.0), 800.0, candles, prices={})
    assert trade is not None and trade.reasons[0].startswith(risk.STOP_REASON_PREFIX)


def test_engine_ages_out_a_short():
    now = time.time()
    eng = _engine(starting_cash=10_000, max_hold_days=5, allow_short=True,
                  trailing_stop=False)
    eng.portfolio.execute(SELL, "BTC-USD", 1000.0, 1.0, timestamp=now - 30 * DAY)
    trade, code = eng._manage(_signal(HOLD, price=1002.0), 1002.0, [], prices={})
    assert trade is not None and code == ACTED
    assert trade.side == BUY  # covered
    assert trade.reasons[0].startswith("Position aging")


def test_freed_slot_lets_a_new_signal_trade():
    """The point of the cap: a stale holding stops blocking fresh signals."""
    now = time.time()
    eng = _engine(starting_cash=10_000, max_open_positions=1, max_hold_days=5,
                  trailing_stop=False)
    eng.portfolio.execute(BUY, "BTC-USD", 1000.0, 1.0, timestamp=now - 30 * DAY)
    # With the book full, a BUY elsewhere is rejected...
    from bot.engine import MAX_OPEN_POSITIONS

    trade, code = eng._manage(_signal(BUY, product="ETH-USD"), 1000.0, [], prices={})
    assert trade is None and code == MAX_OPEN_POSITIONS
    # ...until the stale BTC position ages out and frees the slot.
    eng._manage(_signal(HOLD, price=1005.0), 1005.0, [], prices={})
    trade, code = eng._manage(_signal(BUY, product="ETH-USD"), 1000.0, [], prices={})
    assert trade is not None and code == ACTED


def test_backtest_shares_the_aging_cap():
    from bot.backtest import run_backtest
    from bot.strategies import make_strategy

    # A slow grind up: the regime strategy buys and then simply holds.
    candles = [
        {"time": i * DAY, "open": 1000 + i * 0.1, "high": 1000 + i * 0.1 + 1,
         "low": 1000 + i * 0.1 - 1, "close": 1000 + i * 0.1, "volume": 10}
        for i in range(400)
    ]

    def _run(cfg):
        return run_backtest(make_strategy("regime", cfg.strategy), candles, cfg,
                            product_id="BTC-USD")

    base = _run(_cfg())

    def _aging(result):
        return [
            t for t in result.trades
            if t.reasons and t.reasons[0].startswith("Position aging")
        ]

    assert base.trades and not _aging(base), "baseline never ages anything out"
    aged = _run(_cfg(max_hold_days=20, max_hold_min_gain_pct=0.10))
    assert _aging(aged), "the cap should rotate the stale holding out"


def test_overseer_tallies_exit_reasons(tmp_path, monkeypatch):
    import write_status
    from bot.portfolio import Trade
    from bot.storage import Storage

    monkeypatch.chdir(tmp_path)
    now = time.time()
    st = Storage("trading.regime.db")

    def _t(side, price, qty, reason, ts):
        return Trade(timestamp=ts, product_id="BTC-USD", side=side, price=price,
                     quantity=qty, fee=0.0, cash_after=0.0, reasons=[reason])

    st.save_trade(_t(BUY, 1000.0, 1.0, "entry", now - 3 * DAY))
    st.save_trade(_t(SELL, 1010.0, 1.0, "Position aging: long held 30.0 days", now - 2 * DAY))
    st.save_trade(_t(BUY, 1000.0, 1.0, "entry", now - 2 * DAY))
    st.save_trade(_t(SELL, 900.0, 1.0, "Stop-loss: price $900 hit stop", now - DAY))
    st.close()

    status = write_status.collect_metrics()
    assert status["exit_reasons"] == {"position_aging": 1, "stop_loss": 1}


def test_overseer_classifies_unreadable_reasons_as_other(tmp_path, monkeypatch):
    import sqlite3

    import write_status
    from bot.storage import Storage

    monkeypatch.chdir(tmp_path)
    now = time.time()
    Storage("trading.regime.db").close()
    conn = sqlite3.connect("trading.regime.db")
    for side, price, reasons, ts in (
        (BUY, 1000.0, json.dumps(["entry"]), now - 2 * DAY),
        (SELL, 1010.0, "not json at all", now - DAY),
    ):
        conn.execute(
            "INSERT INTO trades(timestamp, product_id, side, price, quantity, fee, "
            "cash_after, realized_pnl, reasons, indicators, explanation) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (ts, "BTC-USD", side, price, 1.0, 0.0, 0.0, 0.0, reasons, "{}", ""),
        )
    conn.commit()
    conn.close()
    assert write_status.collect_metrics()["exit_reasons"] == {"other": 1}


def test_aging_overrides_resolve_per_account(tmp_path):
    from bot.runner import Runner

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
max_hold_days: 14
accounts:
  - name: patient
    products: [BTC-USD]
    max_hold_days: 60
  - name: inherits
    products: [ETH-USD]
"""
    )
    cfg = Config.load(str(cfg_file))
    patient, inherits = cfg.accounts
    resolve = Runner.__dict__["_account_config"]
    stub = type("S", (), {"config": cfg})()
    assert resolve(stub, patient).max_hold_days == 60
    assert resolve(stub, inherits).max_hold_days == 14
