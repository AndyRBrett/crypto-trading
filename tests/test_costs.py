"""Tests for the transaction-cost gate (issue #44).

Pure-function tests over bot/costs.py plus the engine wiring: an entry whose
projected move doesn't clear its round-trip cost is rejected with
``below_cost_floor``, and exits are never touched.
"""

import time

from bot.config import Config
from bot import costs
from bot.backtest import run_backtest
from bot.engine import ACTED, BELOW_COST_FLOOR, IN_POSITION
from bot.strategy import BUY, SELL, Signal

from tests.test_engine import FakeExplainer, FakeStorage


def _cfg(**overrides):
    cfg = Config()
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


# -- pure cost math ---------------------------------------------------------


def test_median_slippage_uses_magnitude_and_middle_sample():
    # Signed samples: a fill above or below the signal price costs the same.
    assert costs.median_slippage_bps([-30.0, 10.0, 20.0]) == 20.0
    assert costs.median_slippage_bps([10.0, 30.0]) == 20.0
    # No history yet -> fees-only floor, not a crash.
    assert costs.median_slippage_bps(None) == 0.0
    assert costs.median_slippage_bps([]) == 0.0
    # Nulls in the history (unfilled rows) are ignored.
    assert costs.median_slippage_bps([None, 40.0, None]) == 40.0


def test_round_trip_cost_counts_both_legs():
    cfg = _cfg(fee_rate=0.006)  # 60 bps one way
    assert costs.round_trip_cost_bps(cfg, []) == 120.0
    # +25 bps median slippage on each leg.
    assert costs.round_trip_cost_bps(cfg, [20.0, 25.0, 30.0]) == 170.0


def test_expected_edge_is_the_take_profit_distance():
    cfg = _cfg(take_profit_atr_mult=4.0)
    # Target = 4 * 50 = 200 on a $1,000 price -> 20% -> 2,000 bps.
    assert costs.expected_edge_bps(cfg, price=1000.0, atr=50.0) == 2000.0
    # A tighter ATR projects a proportionally smaller move.
    assert costs.expected_edge_bps(cfg, price=1000.0, atr=5.0) == 200.0


def test_expected_edge_without_atr_uses_reward_risk_on_fallback_stop():
    # No ATR -> no take-profit target; fall back to 6/2 = 3:1 on an 8% stop = 24%.
    cfg = _cfg(take_profit_atr_mult=6.0, stop_loss_atr_mult=2.0, fallback_stop_pct=0.08)
    assert costs.expected_edge_bps(cfg, price=1000.0, atr=None) == 2400.0


def test_verdict_blocks_only_when_enabled():
    # Cost 120 bps x 1.5 margin = 180 bps required; the trade projects 100 bps.
    cfg = _cfg(fee_rate=0.006, take_profit_atr_mult=4.0, cost_floor_margin=1.5)
    measured = costs.cost_floor_verdict(cfg, price=1000.0, atr=2.5, samples=[])
    assert measured["edge_bps"] == 100.0
    assert measured["cost_bps"] == 120.0
    assert measured["required_bps"] == 180.0
    # Measured but inert until the gate is switched on.
    assert measured["enabled"] is False and measured["blocked"] is False

    cfg.cost_floor_enabled = True
    assert costs.cost_floor_verdict(cfg, 1000.0, 2.5, [])["blocked"] is True
    # A wide-enough projected move clears the same floor.
    assert costs.cost_floor_verdict(cfg, 1000.0, 50.0, [])["blocked"] is False


def test_slippage_history_raises_the_floor():
    cfg = _cfg(fee_rate=0.006, take_profit_atr_mult=4.0, cost_floor_margin=1.0,
               cost_floor_enabled=True)
    # 200 bps projected clears a fees-only 120 bps floor...
    assert costs.cost_floor_verdict(cfg, 1000.0, 5.0, [])["blocked"] is False
    # ...but not once fills show 50 bps of slippage each way (120 + 100 = 220).
    verdict = costs.cost_floor_verdict(cfg, 1000.0, 5.0, [40.0, 50.0, 60.0])
    assert verdict["cost_bps"] == 220.0 and verdict["samples"] == 3
    assert verdict["blocked"] is True


# -- engine wiring ----------------------------------------------------------


class SlippageStorage(FakeStorage):
    """FakeStorage with a canned per-fill slippage history."""

    def __init__(self, samples=(), signals=None):
        super().__init__()
        self.samples = list(samples)
        self.signals = signals if signals is not None else []

    def recent_slippage_bps(self, product_id, limit=20):
        return self.samples[:limit]

    def save_signal(self, *a, **k):
        self.signals.append(k)


def _engine(storage=None, **cfg_overrides):
    from bot.engine import Engine

    return Engine(
        _cfg(**cfg_overrides),
        market_data=object(),
        storage=storage or SlippageStorage(),
        explainer=FakeExplainer(),
    )


def _signal(action, price=1000.0, atr=2.5, product="BTC-USD"):
    return Signal(
        product_id=product, action=action, price=price,
        indicators={"atr": atr}, reasons=["test reason"],
    )


def test_engine_rejects_entry_below_cost_floor():
    eng = _engine(starting_cash=10_000, fee_rate=0.006, take_profit_atr_mult=4.0,
                  cost_floor_enabled=True, cost_floor_margin=1.5)
    # ATR 2.5 on $1,000 projects 100 bps against a 180 bps requirement.
    trade, code = eng._manage(_signal(BUY), 1000.0, [], prices={})
    assert trade is None and code == BELOW_COST_FLOOR
    # A wider ATR projects far past the floor and trades as before.
    trade, code = eng._manage(_signal(BUY, atr=50.0), 1000.0, [], prices={})
    assert trade is not None and code == ACTED


def test_engine_gate_is_inert_until_enabled():
    eng = _engine(starting_cash=10_000, fee_rate=0.006, take_profit_atr_mult=4.0)
    trade, code = eng._manage(_signal(BUY), 1000.0, [], prices={})
    assert trade is not None and code == ACTED


def test_engine_gate_uses_logged_slippage():
    # Fees alone (120 bps x 1.0) leave a 200 bps projection fine; adding 50 bps
    # of realized slippage per leg pushes the floor to 220 bps and blocks it.
    kw = dict(starting_cash=10_000, fee_rate=0.006, take_profit_atr_mult=4.0,
              cost_floor_enabled=True, cost_floor_margin=1.0)
    clean = _engine(SlippageStorage(samples=[]), **kw)
    trade, code = clean._manage(_signal(BUY, atr=5.0), 1000.0, [], prices={})
    assert trade is not None and code == ACTED

    costly = _engine(SlippageStorage(samples=[40.0, 50.0, 60.0]), **kw)
    trade, code = costly._manage(_signal(BUY, atr=5.0), 1000.0, [], prices={})
    assert trade is None and code == BELOW_COST_FLOOR


def test_engine_never_gates_an_exit():
    """An open position must always be able to close, however thin the move."""
    eng = _engine(starting_cash=10_000, fee_rate=0.006, take_profit_atr_mult=4.0,
                  cost_floor_enabled=True, cost_floor_margin=1.5, trailing_stop=False)
    eng.portfolio.execute(BUY, "BTC-USD", 1000.0, 1.0, timestamp=time.time() - 100)
    # Same thin-ATR signal that was blocked as an entry above: as a SELL on an
    # open long it closes the position.
    trade, code = eng._manage(_signal(SELL), 1000.0, [], prices={})
    assert trade is not None and code == ACTED


def test_engine_shorts_are_gated_too():
    eng = _engine(starting_cash=10_000, fee_rate=0.006, take_profit_atr_mult=4.0,
                  allow_short=True, cost_floor_enabled=True, cost_floor_margin=1.5)
    trade, code = eng._manage(_signal(SELL), 1000.0, [], prices={})
    assert trade is None and code == BELOW_COST_FLOOR


def test_storage_reads_back_recent_slippage(tmp_path):
    from bot.storage import Storage

    st = Storage(str(tmp_path / "t.db"))
    st.save_signal(1.0, "BTC-USD", BUY, 100.0, "r", outcome="acted", slippage_bps=10.0)
    st.save_signal(2.0, "BTC-USD", BUY, 100.0, "r", outcome="hold")  # no fill
    st.save_signal(3.0, "ETH-USD", BUY, 100.0, "r", outcome="acted", slippage_bps=99.0)
    st.save_signal(4.0, "BTC-USD", BUY, 100.0, "r", outcome="acted", slippage_bps=20.0)
    # Newest first, unfilled rows and other products excluded.
    assert st.recent_slippage_bps("BTC-USD") == [20.0, 10.0]
    assert st.recent_slippage_bps("BTC-USD", limit=1) == [20.0]
    assert st.recent_slippage_bps("SOL-USD") == []
    st.close()


def test_backtest_shares_the_gate():
    """The backtester applies the same floor, so a sweep measures the live rule."""
    # A steady uptrend the regime strategy is always long in; a 1,000x margin
    # makes the floor unreachable, so enabling it must remove the entries.
    candles = [
        {"time": i * 3600, "open": 100 + i, "high": 101 + i, "low": 99 + i,
         "close": 100 + i, "volume": 10}
        for i in range(300)
    ]
    from bot.strategies import make_strategy

    def _run(cfg):
        strat = make_strategy("regime", cfg.strategy)
        return run_backtest(strat, candles, cfg, product_id="BTC-USD")

    base = _run(_cfg())
    assert base.trades, "baseline should take the uptrend"
    gated = _run(_cfg(cost_floor_enabled=True, cost_floor_margin=1000.0))
    assert gated.trades == []


def test_cost_floor_overrides_resolve_per_account(tmp_path):
    """A per-account override wins; an unset one inherits the top-level value."""
    from bot.config import Config
    from bot.runner import Runner

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
cost_floor_enabled: true
cost_floor_margin: 1.5
accounts:
  - name: tight
    products: [BTC-USD]
    cost_floor_margin: 3.0
  - name: inherits
    products: [ETH-USD]
"""
    )
    cfg = Config.load(str(cfg_file))
    tight, inherits = cfg.accounts
    assert tight.cost_floor_margin == 3.0
    assert inherits.cost_floor_margin is None  # unset -> inherit

    resolve = Runner.__dict__["_account_config"]
    stub = type("S", (), {"config": cfg})()
    assert resolve(stub, tight).cost_floor_margin == 3.0
    assert resolve(stub, inherits).cost_floor_margin == 1.5
    assert resolve(stub, inherits).cost_floor_enabled is True
