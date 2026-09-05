"""Tests for volatility-targeted sizing (issue #53).

Covers the estimators, the sizing bound, the engine/backtest wiring, and the
property that matters most: the bound can only ever shrink a position.
"""

import math
import time

from bot import risk, volatility
from bot.config import Config
from bot.engine import Engine
from bot.strategy import BUY, Signal

from tests.test_breaker import CurveStorage
from tests.test_engine import FakeExplainer

HOUR = 3600


def _cfg(**overrides):
    cfg = Config()
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


# -- estimators -------------------------------------------------------------


def test_bars_per_year_matches_the_247_convention():
    assert volatility.bars_per_year(HOUR) == 365 * 24
    assert volatility.bars_per_year(86_400) == 365
    assert volatility.bars_per_year(0) is None
    assert volatility.bars_per_year(None) is None


def test_realized_vol_annualizes_daily_dispersion():
    # Alternating +1% / -1% daily closes: per-bar stdev ~1%, annualized by sqrt(365).
    closes = [100.0]
    for i in range(40):
        closes.append(closes[-1] * (1.01 if i % 2 == 0 else 1 / 1.01))
    vol = volatility.realized_vol(closes, 20, volatility.bars_per_year(86_400))
    assert vol is not None
    assert abs(vol - 0.01 * math.sqrt(365)) < 0.02


def test_unmeasurable_vol_is_none_not_zero():
    """A zero volatility would divide into an unbounded position size."""
    per_year = volatility.bars_per_year(HOUR)
    assert volatility.realized_vol([100.0] * 30, 20, per_year) is None  # no dispersion
    assert volatility.realized_vol([100.0, 101.0], 20, per_year) is None  # too few
    assert volatility.realized_vol([100.0] * 30, 20, None) is None  # unknown bar length
    assert volatility.atr_vol(0.0, 100.0, per_year) is None
    assert volatility.atr_vol(5.0, 0.0, per_year) is None


def test_estimate_falls_back_to_atr():
    cfg = _cfg(vol_lookback_bars=20)
    # No usable closes -> the ATR estimate stands in.
    est = volatility.estimate_vol(cfg, [], atr=1.5, price=100.0, seconds_per_bar=HOUR)
    assert est is not None
    assert abs(est - 0.015 * math.sqrt(365 * 24)) < 1e-9
    # Neither closes nor ATR -> no estimate, so no bound.
    assert volatility.estimate_vol(cfg, [], None, 100.0, HOUR) is None


def test_estimate_prefers_realized_over_atr():
    cfg = _cfg(vol_lookback_bars=20)
    closes = [100.0]
    for i in range(40):
        closes.append(closes[-1] * (1.001 if i % 2 == 0 else 1 / 1.001))
    # A wildly larger ATR is ignored while real closes are available.
    est = volatility.estimate_vol(cfg, closes, atr=20.0, price=100.0, seconds_per_bar=HOUR)
    atr_based = volatility.atr_vol(20.0, 100.0, volatility.bars_per_year(HOUR))
    assert est < atr_based / 10


# -- the sizing bound -------------------------------------------------------


def test_vol_target_qty_hits_the_target_contribution():
    cfg = _cfg(vol_target_enabled=True, vol_target_pct=0.20)
    qty = volatility.vol_target_qty(cfg, equity=50_000, price=1000.0, vol=0.80)
    # notional * vol == equity * target
    assert abs(qty * 1000.0 * 0.80 - 50_000 * 0.20) < 1e-6


def test_no_bound_when_disabled_or_unmeasurable():
    on = _cfg(vol_target_enabled=True)
    assert volatility.vol_target_qty(_cfg(), 50_000, 1000.0, 0.8) is None
    assert volatility.vol_target_qty(on, 50_000, 1000.0, None) is None
    assert volatility.vol_target_qty(on, 50_000, 1000.0, 0.0) is None
    assert volatility.vol_target_qty(
        _cfg(vol_target_enabled=True, vol_target_pct=0.0), 50_000, 1000.0, 0.8
    ) is None


def test_bound_binds_where_the_equity_cap_used_to():
    """The gap this closes: in calm regimes the flat 30% cap wins and sizing
    stops tracking volatility, so two very different assets take one notional."""
    equity = 50_000.0
    price = 60_000.0
    atr = price * 0.015  # calm: the risk bound is looser than the equity cap
    off = risk.position_size(_cfg(), equity, equity, price, atr)
    assert abs(off * price - equity * 0.30) < 1.0  # cap binds today

    per_year = volatility.bars_per_year(HOUR)
    vol = volatility.atr_vol(atr, price, per_year)
    on = risk.position_size(
        _cfg(vol_target_enabled=True), equity, equity, price, atr, asset_vol=vol
    )
    assert on < off
    assert abs(on * price * vol - equity * 0.20) < 1.0


def test_bound_never_enlarges_a_position():
    """A strict vol-target would size *up* through the equity cap in calm
    regimes; this one is a min() bound, so the cap stays the backstop."""
    equity = 50_000.0
    price, atr = 1000.0, 200.0  # tiny vol relative to the risk budget
    off = risk.position_size(_cfg(), equity, equity, price, atr)
    on = risk.position_size(
        _cfg(vol_target_enabled=True, vol_target_pct=99.0),
        equity, equity, price, atr, asset_vol=0.01,
    )
    assert on == off


def test_bound_applies_to_shorts_too():
    equity = 50_000.0
    price, atr = 1000.0, 15.0
    vol = volatility.atr_vol(atr, price, volatility.bars_per_year(HOUR))
    off = risk.position_size(_cfg(), equity, equity, price, atr, direction="short")
    on = risk.position_size(
        _cfg(vol_target_enabled=True), equity, equity, price, atr,
        direction="short", asset_vol=vol,
    )
    assert on < off


# -- engine / backtest ------------------------------------------------------


def _candles(closes, start=0):
    return [
        {"time": start + i * HOUR, "open": c, "high": c * 1.01, "low": c * 0.99,
         "close": c, "volume": 10}
        for i, c in enumerate(closes)
    ]


def _engine(**cfg_overrides):
    return Engine(
        _cfg(**cfg_overrides),
        market_data=object(),
        storage=CurveStorage(),
        explainer=FakeExplainer(),
    )


def test_engine_sizes_a_wild_asset_smaller_than_a_calm_one():
    calm, wild = [1000.0], [1000.0]
    for i in range(60):
        calm.append(calm[-1] * (1.001 if i % 2 == 0 else 1 / 1.001))
        wild.append(wild[-1] * (1.05 if i % 2 == 0 else 1 / 1.05))
    kw = dict(starting_cash=50_000, vol_target_enabled=True)
    eng = _engine(**kw)
    eng._refresh_breaker()
    calm_qty = eng._position_size(1000.0, 15.0, {}, candles=_candles(calm))
    wild_qty = eng._position_size(1000.0, 15.0, {}, candles=_candles(wild))
    assert wild_qty < calm_qty

    # With the feature off, the identical inputs size identically — the flat cap.
    plain = _engine(starting_cash=50_000)
    plain._refresh_breaker()
    assert plain._position_size(1000.0, 15.0, {}, candles=_candles(calm)) == \
        plain._position_size(1000.0, 15.0, {}, candles=_candles(wild))


def test_engine_ignores_the_forming_candle():
    """Vol is measured on closed candles: a partial bar's move hasn't happened."""
    closes = [1000.0]
    for i in range(60):
        closes.append(closes[-1] * (1.001 if i % 2 == 0 else 1 / 1.001))
    eng = _engine(starting_cash=50_000, vol_target_enabled=True)
    settled = _candles(closes, start=int(time.time()) - 61 * HOUR)
    quiet = eng._asset_vol(settled, atr=15.0, price=1000.0)
    # A violent still-forming bar appended on the end must not move the estimate.
    spiked = settled + [{"time": int(time.time()), "open": 1000.0, "high": 2000.0,
                         "low": 500.0, "close": 2000.0, "volume": 10}]
    assert eng._asset_vol(spiked, atr=15.0, price=1000.0) == quiet


def test_engine_trades_normally_when_vol_is_unmeasurable():
    eng = _engine(starting_cash=50_000, vol_target_enabled=True)
    eng._refresh_breaker()
    signal = Signal(product_id="BTC-USD", action=BUY, price=1000.0,
                    indicators={"atr": 50.0}, reasons=["r"])
    trade, code = eng._manage(signal, 1000.0, [], prices={})
    assert trade is not None  # no candles, no ATR history -> no bound, not a block


def test_backtest_shares_the_vol_bound():
    from bot.backtest import run_backtest
    from bot.strategies import make_strategy

    closes = [1000.0 + i for i in range(220)]
    for i in range(60):  # a violent stretch the bound should size down into
        closes.append(closes[-1] * (1.06 if i % 2 == 0 else 1 / 1.05))
    candles = _candles(closes)

    def _run(cfg):
        return run_backtest(make_strategy("regime", cfg.strategy), candles, cfg,
                            product_id="BTC-USD")

    base = _run(_cfg())
    bounded = _run(_cfg(vol_target_enabled=True, vol_target_pct=0.05))
    assert base.trades and bounded.trades

    def _last_entry_notional(result):
        # Entries during the violent stretch are where the bound bites; the
        # first entry happens back in the calm ramp, where it doesn't.
        entries = [t for t in result.trades if t.side == BUY]
        assert entries, "expected at least one entry"
        return entries[-1].price * entries[-1].quantity

    assert _last_entry_notional(bounded) < _last_entry_notional(base)


def test_vol_target_overrides_resolve_per_account(tmp_path):
    from bot.runner import Runner

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
vol_target_enabled: true
vol_target_pct: 0.2
accounts:
  - name: tight
    products: [BTC-USD]
    vol_target_pct: 0.05
  - name: inherits
    products: [ETH-USD]
"""
    )
    cfg = Config.load(str(cfg_file))
    tight, inherits = cfg.accounts
    resolve = Runner.__dict__["_account_config"]
    stub = type("S", (), {"config": cfg})()
    assert resolve(stub, tight).vol_target_pct == 0.05
    assert resolve(stub, inherits).vol_target_pct == 0.2
    assert resolve(stub, inherits).vol_target_enabled is True
