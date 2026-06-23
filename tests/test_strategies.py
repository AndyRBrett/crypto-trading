"""Tests for the strategy registry/factory and the alternative strategies."""

import pytest

from bot.strategy import BUY, HOLD, SELL, Signal, Strategy, StrategyConfig
from bot.strategies import (
    DonchianBreakoutStrategy,
    RsiMeanReversionStrategy,
    available,
    make_strategy,
)


class FakeSentiment:
    def __init__(self, score, label="negative", summary="bad news"):
        self.score = score
        self.label = label
        self.summary = summary


def ohlc(rows):
    """rows = list of (high, low, close); time/open derived."""
    out = []
    for i, (h, l, c) in enumerate(rows):
        out.append({"time": 1700000000 + i * 3600, "open": c, "high": h, "low": l, "close": c})
    return out


def closes(values):
    return [{"close": c} for c in values]


# -- registry / factory -----------------------------------------------------


def test_registry_lists_all_three():
    assert set(available()) >= {"ema_crossover", "rsi_mean_reversion", "donchian_breakout"}


def test_make_strategy_returns_right_class():
    assert isinstance(make_strategy("ema_crossover", StrategyConfig()), Strategy)
    assert isinstance(make_strategy("rsi_mean_reversion", StrategyConfig()), RsiMeanReversionStrategy)
    assert isinstance(make_strategy("donchian_breakout", StrategyConfig()), DonchianBreakoutStrategy)


def test_make_strategy_unknown_raises():
    with pytest.raises(ValueError):
        make_strategy("nope", StrategyConfig())


# -- RSI mean reversion -----------------------------------------------------


def _rsi_strategy(**ov):
    cfg = StrategyConfig(rsi_period=3, atr_period=3, rsi_mr_oversold=30, rsi_mr_overbought=55)
    for k, v in ov.items():
        setattr(cfg, k, v)
    return RsiMeanReversionStrategy(cfg)


def test_rsi_mean_reversion_buys_oversold():
    s = _rsi_strategy()
    # Steady decline -> RSI pegs low -> BUY (fade the weakness).
    sig = s.generate_signal("BTC-USD", ohlc([(c, c, c) for c in [10, 9, 8, 7, 6, 5]]))
    assert sig.action == BUY
    assert "rsi" in sig.indicators
    assert "atr" in sig.indicators


def test_rsi_mean_reversion_sells_overbought():
    s = _rsi_strategy()
    sig = s.generate_signal("BTC-USD", ohlc([(c, c, c) for c in [5, 6, 7, 8, 9, 10]]))
    assert sig.action == SELL


def test_rsi_mean_reversion_holds_midrange():
    s = _rsi_strategy(rsi_mr_oversold=5, rsi_mr_overbought=95)
    sig = s.generate_signal("BTC-USD", ohlc([(c, c, c) for c in [10, 11, 10, 11, 10, 11]]))
    assert sig.action == HOLD


def test_rsi_mean_reversion_hold_records_threshold_distance():
    s = _rsi_strategy(rsi_mr_oversold=5, rsi_mr_overbought=95)
    sig = s.generate_signal("BTC-USD", ohlc([(c, c, c) for c in [10, 11, 10, 11, 10, 11]]))
    th = sig.thresholds
    assert th["rsi_to_oversold"] == round(sig.indicators["rsi"] - 5, 2)
    assert th["rsi_to_overbought"] == round(95 - sig.indicators["rsi"], 2)


def test_rsi_mean_reversion_insufficient_data_holds():
    s = _rsi_strategy()
    sig = s.generate_signal("BTC-USD", closes([10, 11]))
    assert sig.action == HOLD


# -- Donchian breakout ------------------------------------------------------


def _donchian(**ov):
    cfg = StrategyConfig(donchian_period=3, donchian_exit_period=2, atr_period=3)
    for k, v in ov.items():
        setattr(cfg, k, v)
    return DonchianBreakoutStrategy(cfg)


def test_donchian_buys_on_breakout():
    s = _donchian()
    # Flat channel then a close above the prior 3-bar high -> BUY.
    rows = [(10, 9, 10)] * 4 + [(20, 18, 20)]
    sig = s.generate_signal("BTC-USD", ohlc(rows))
    assert sig.action == BUY
    assert "donchian_upper" in sig.indicators
    assert "atr" in sig.indicators


def test_donchian_sells_on_channel_break():
    s = _donchian()
    # Flat channel then a close below the prior 2-bar low -> SELL.
    rows = [(10, 9, 10)] * 4 + [(8, 2, 3)]
    sig = s.generate_signal("BTC-USD", ohlc(rows))
    assert sig.action == SELL


def test_donchian_holds_inside_channel():
    s = _donchian()
    rows = [(10, 9, 10)] * 4 + [(10, 9, 9.5)]
    sig = s.generate_signal("BTC-USD", ohlc(rows))
    assert sig.action == HOLD


def test_donchian_hold_records_distance_to_channel_edges():
    s = _donchian()
    rows = [(10, 9, 10)] * 4 + [(10, 9, 9.5)]
    sig = s.generate_signal("BTC-USD", ohlc(rows))
    th = sig.thresholds
    upper, lower = sig.indicators["donchian_upper"], sig.indicators["donchian_lower"]
    assert th["breakout_dist_pct"] == round((9.5 - upper) / upper * 100, 3)
    assert th["exit_dist_pct"] == round((9.5 - lower) / lower * 100, 3)
    # Inside the channel: not yet broken out (<=0) nor below the exit (>=0).
    assert th["breakout_dist_pct"] <= 0 and th["exit_dist_pct"] >= 0


# -- shared sentiment gating across all strategies --------------------------


def test_rsi_sentiment_vetoes_buy():
    s = _rsi_strategy()
    rows = ohlc([(c, c, c) for c in [10, 9, 8, 7, 6, 5]])
    sig = s.generate_signal("BTC-USD", rows, sentiment=FakeSentiment(-0.9))
    assert sig.action == HOLD


def test_donchian_sentiment_vetoes_buy():
    s = _donchian()
    rows = ohlc([(10, 9, 10)] * 4 + [(20, 18, 20)])
    sig = s.generate_signal("BTC-USD", rows, sentiment=FakeSentiment(-0.9))
    assert sig.action == HOLD


# -- new long/short + regime strategies -------------------------------------


def test_registry_includes_long_short_and_regime():
    assert {"trend_long_short", "regime"} <= set(available())


def _long_short():
    # RSI gates widened out of the way so the test exercises the trend logic.
    return make_strategy(
        "trend_long_short",
        StrategyConfig(fast_period=3, slow_period=6, ma_type="sma", trend_period=10,
                       adx_filter=False, rsi_oversold=-1.0, rsi_overbought=101.0),
    )


def test_trend_long_short_shorts_a_downtrend():
    sig = _long_short().generate_signal("BTC-USD", closes([200 - 3 * i for i in range(30)]))
    assert sig.action == SELL          # fast < slow, price below the trend MA
    assert "atr" in sig.indicators     # the engine needs ATR to size/stop


def test_trend_long_short_longs_an_uptrend():
    sig = _long_short().generate_signal("BTC-USD", closes([100 + 3 * i for i in range(30)]))
    assert sig.action == BUY


def test_regime_holds_above_trend_ma_and_exits_below():
    strat = make_strategy("regime", StrategyConfig(trend_period=10, atr_period=5))
    assert strat.generate_signal("BTC-USD", closes([100 + 2 * i for i in range(20)])).action == BUY
    assert strat.generate_signal("BTC-USD", closes([200 - 2 * i for i in range(20)])).action == SELL


def test_regime_ignores_sentiment():
    # The regime model is price-only; a bearish news score must not change a BUY.
    strat = make_strategy("regime", StrategyConfig(trend_period=10, atr_period=5))
    sig = strat.generate_signal(
        "BTC-USD", closes([100 + 2 * i for i in range(20)]), sentiment=FakeSentiment(-0.9)
    )
    assert sig.action == BUY
