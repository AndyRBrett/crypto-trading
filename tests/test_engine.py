"""Tests for the engine's risk layer: sizing and protective exits.

These exercise the helpers directly with a fake storage/explainer so no disk,
network, or LLM is involved.
"""

import time

from bot.config import Config
from bot.engine import Engine
from bot.strategy import BUY


class FakeStorage:
    def __init__(self):
        self._meta: dict = {}

    def load_trades(self):
        return []

    def save_trade(self, trade):
        pass

    def save_equity(self, *a, **k):
        pass

    def export_state(self, *a, **k):
        pass

    def save_signal(self, *a, **k):
        pass

    def get_meta(self, key: str):
        return self._meta.get(key)

    def set_meta(self, key: str, value: str):
        self._meta[key] = value

    def close(self):
        pass


class FakeExplainer:
    def explain(self, *a, **k):
        return "test explanation"


def make_engine(**cfg_overrides):
    cfg = Config()
    for k, v in cfg_overrides.items():
        setattr(cfg, k, v)
    return Engine(
        cfg,
        market_data=object(),
        storage=FakeStorage(),
        explainer=FakeExplainer(),
    )


def test_position_size_risks_fixed_fraction():
    # 1% of $10k = $100 risk. Stop distance = 2 * ATR = 2 * 50 = 100 -> qty 1.0,
    # but the 30% equity cap ($3000 / $1000 = 3) and cash cap don't bind here.
    eng = make_engine(
        starting_cash=10_000,
        risk_per_trade_pct=0.01,
        stop_loss_atr_mult=2.0,
        max_position_pct=0.30,
    )
    qty = eng._position_size(price=1000.0, atr=50.0, prices={})
    assert abs(qty - 1.0) < 1e-6


def test_position_size_capped_by_max_position():
    # Tiny ATR would size huge on risk alone; the 30% cap must bind.
    eng = make_engine(starting_cash=10_000, risk_per_trade_pct=0.01, max_position_pct=0.30)
    qty = eng._position_size(price=1000.0, atr=0.5, prices={})
    assert abs(qty - (10_000 * 0.30 / 1000.0)) < 1e-6  # 3.0


def test_position_size_zero_for_dust():
    eng = make_engine(starting_cash=5)  # below the $10 dust floor
    assert eng._position_size(price=1000.0, atr=50.0, prices={}) == 0.0


def _open_long(eng, product, price, qty):
    eng.portfolio.execute(BUY, product, price, qty, timestamp=time.time() - 100)


def test_protective_exit_stop_loss():
    eng = make_engine(stop_loss_atr_mult=2.0, trailing_stop=False)
    _open_long(eng, "BTC-USD", price=1000.0, qty=1.0)
    pos = eng.portfolio.position("BTC-USD")
    candles = [{"time": 0, "high": 1010, "low": 990}]
    # stop = 1000 - 2*50 = 900. Price below it -> exit.
    reason = eng._protective_exit("BTC-USD", pos, price=895.0, atr=50.0, candles=candles)
    assert reason and "stop-loss" in reason.lower()
    # Just above the stop -> no exit.
    assert eng._protective_exit("BTC-USD", pos, 905.0, 50.0, candles) is None


def test_protective_exit_take_profit():
    eng = make_engine(take_profit_atr_mult=4.0, trailing_stop=False)
    _open_long(eng, "ETH-USD", price=100.0, qty=2.0)
    pos = eng.portfolio.position("ETH-USD")
    candles = [{"time": 0, "high": 101, "low": 99}]
    # target = 100 + 4*5 = 120.
    assert eng._protective_exit("ETH-USD", pos, 121.0, 5.0, candles) is not None
    assert "take-profit" in eng._protective_exit("ETH-USD", pos, 121.0, 5.0, candles).lower()
    assert eng._protective_exit("ETH-USD", pos, 119.0, 5.0, candles) is None


def test_trailing_stop_ratchets_up():
    # Price ran up to a high of 1500; trailing stop = 1500 - 2*50 = 1400.
    # Take-profit set far away so the trailing stop is what's under test.
    eng = make_engine(stop_loss_atr_mult=2.0, trailing_stop=True, take_profit_atr_mult=100.0)
    _open_long(eng, "BTC-USD", price=1000.0, qty=1.0)
    pos = eng.portfolio.position("BTC-USD")
    candles = [{"time": time.time(), "high": 1500, "low": 1200}]
    # 1410 is above the trailing stop -> hold.
    assert eng._protective_exit("BTC-USD", pos, 1410.0, 50.0, candles) is None
    # 1390 fell below the ratcheted stop -> exit, even though far above entry.
    assert eng._protective_exit("BTC-USD", pos, 1390.0, 50.0, candles) is not None
