from bot.strategy import BUY, HOLD, SELL, Strategy, StrategyConfig


def candles(closes):
    return [{"close": c} for c in closes]


def make_strategy(**overrides):
    cfg = StrategyConfig(
        fast_period=2,
        slow_period=4,
        rsi_period=2,
        rsi_overbought=70.0,
        rsi_oversold=30.0,
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return Strategy(cfg)


def test_insufficient_data_holds():
    s = make_strategy()
    sig = s.generate_signal("BTC-USD", candles([10, 11]))
    assert sig.action == HOLD


def test_bullish_crossover_buys():
    s = make_strategy(rsi_overbought=95.0)
    # Decline then a jump up -> fast SMA crosses above slow SMA on the last bar.
    sig = s.generate_signal("BTC-USD", candles([10, 10, 10, 10, 8, 13]))
    assert sig.action == BUY
    assert sig.indicators["fast_sma"] > sig.indicators["slow_sma"]


def test_bearish_crossover_sells():
    s = make_strategy()
    # Rise then a drop -> fast SMA crosses below slow SMA on the last bar.
    sig = s.generate_signal("BTC-USD", candles([10, 10, 10, 10, 12, 7]))
    assert sig.action == SELL


def test_rsi_overbought_takes_profit():
    s = make_strategy(rsi_overbought=70.0)
    # Steady uptrend: no fresh crossover, but RSI pegs high -> SELL to take profit.
    sig = s.generate_signal("BTC-USD", candles([1, 2, 3, 4, 5, 6, 7, 8]))
    assert sig.action == SELL


def test_rsi_blocks_overbought_buy():
    s = make_strategy(rsi_overbought=10.0)
    # Bullish crossover, but RSI is above the (very low) overbought threshold.
    sig = s.generate_signal("BTC-USD", candles([10, 10, 10, 10, 8, 13]))
    assert sig.action == HOLD


def test_choppy_market_holds():
    s = make_strategy()
    sig = s.generate_signal("BTC-USD", candles([10, 11, 10, 11, 10, 11]))
    assert sig.action == HOLD
