from bot.strategy import BUY, HOLD, SELL, Strategy, StrategyConfig


def candles(closes):
    return [{"close": c} for c in closes]


def make_strategy(**overrides):
    # Core crossover tests pin SMA and turn the regime filters off so the
    # behaviour stays deterministic with short candle series.
    cfg = StrategyConfig(
        fast_period=2,
        slow_period=4,
        ma_type="sma",
        rsi_period=2,
        rsi_overbought=70.0,
        rsi_oversold=30.0,
        trend_filter=False,
        adx_filter=False,
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


def ohlc(closes):
    # Build candles with a small symmetric high/low band around each close.
    return [
        {"time": i, "close": c, "high": c + 0.5, "low": c - 0.5}
        for i, c in enumerate(closes)
    ]


def test_trend_filter_blocks_counter_trend_buy():
    # Bullish crossover near recent lows, but price is still far below the
    # long-term trend MA (early prices were much higher) -> no BUY.
    s = make_strategy(rsi_overbought=95.0, trend_filter=True, trend_period=7)
    sig = s.generate_signal("BTC-USD", candles([100, 100, 100, 8, 7, 6, 5, 11]))
    assert sig.action == HOLD
    assert any("counter-trend" in r.lower() for r in sig.reasons)


def test_trend_filter_allows_with_trend_buy():
    # Bullish crossover with price above a (short) rising trend MA -> BUY.
    s = make_strategy(rsi_overbought=95.0, trend_filter=True, trend_period=3)
    sig = s.generate_signal("BTC-USD", candles([10, 10, 10, 10, 9, 14]))
    assert sig.action == BUY
    assert "trend_ma" in sig.indicators


def test_adx_filter_blocks_chop():
    # A real bullish crossover, but ADX stays low on a choppy series -> skip.
    s = make_strategy(rsi_overbought=95.0, adx_filter=True, adx_period=3, adx_min=60.0)
    closes = [10, 11, 10, 11, 10, 11, 10, 9, 12]
    sig = s.generate_signal("BTC-USD", ohlc(closes))
    assert sig.action == HOLD
    assert "adx" in sig.indicators


def test_hold_carries_distance_to_each_threshold():
    # A HOLD must still record how close it came to firing, so a no_signal can be
    # tuned rather than being an opaque gap.
    s = make_strategy(rsi_overbought=95.0, trend_filter=True, trend_period=3,
                      adx_filter=True, adx_period=3, adx_min=60.0)
    sig = s.generate_signal("BTC-USD", ohlc([10, 11, 10, 11, 10, 11, 10, 9, 12]))
    assert sig.action == HOLD
    th = sig.thresholds
    # The four entry gates each get a signed distance to where they'd flip.
    assert "ma_gap_pct" in th
    assert "rsi_to_overbought" in th
    assert "price_to_trend_pct" in th
    assert "adx_to_min" in th
    # ma_gap_pct is the signed % gap between the fast and slow MA.
    expected_gap = round(
        (sig.indicators["fast_sma"] - sig.indicators["slow_sma"])
        / sig.indicators["slow_sma"] * 100, 3
    )
    assert th["ma_gap_pct"] == expected_gap
    # adx_to_min is ADX minus the floor; it's negative here (chop blocked it).
    assert th["adx_to_min"] == round(sig.indicators["adx"] - 60.0, 2)


def test_thresholds_omit_filters_that_are_off():
    s = make_strategy(trend_filter=False, adx_filter=False)
    sig = s.generate_signal("BTC-USD", candles([10, 11, 10, 11, 10, 11]))
    assert "price_to_trend_pct" not in sig.thresholds
    assert "adx_to_min" not in sig.thresholds
