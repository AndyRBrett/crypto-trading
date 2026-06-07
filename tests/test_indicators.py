from bot import indicators


def test_sma_basic():
    assert indicators.sma([1, 2, 3, 4, 5], 5) == 3
    assert indicators.sma([1, 2, 3, 4, 5], 2) == 4.5


def test_sma_insufficient_data():
    assert indicators.sma([1, 2], 5) is None
    assert indicators.sma([], 1) is None


def test_ema_matches_sma_when_flat():
    # A flat series should have EMA equal to the constant value.
    assert indicators.ema([10] * 20, 5) == 10


def test_ema_insufficient_data():
    assert indicators.ema([1, 2], 5) is None


def test_rsi_all_gains_is_100():
    rising = list(range(1, 30))
    assert indicators.rsi(rising, 14) == 100.0


def test_rsi_all_losses_is_low():
    falling = list(range(30, 1, -1))
    val = indicators.rsi(falling, 14)
    assert val is not None and val < 1.0


def test_rsi_midrange_for_choppy_series():
    series = []
    price = 100.0
    for i in range(40):
        price += 1 if i % 2 == 0 else -1
        series.append(price)
    val = indicators.rsi(series, 14)
    assert val is not None and 30 < val < 70


def test_rsi_insufficient_data():
    assert indicators.rsi([1, 2, 3], 14) is None


def test_atr_constant_true_range():
    # Bars step up by 1 with a 1.0-wide range, so each true range is
    # max(1.0, |high-prevClose|=1.5, |low-prevClose|=0.5) = 1.5 -> ATR 1.5.
    closes = [10, 11, 12, 13, 14]
    highs = [c + 0.5 for c in closes]
    lows = [c - 0.5 for c in closes]
    val = indicators.atr(highs, lows, closes, period=2)
    assert val is not None and abs(val - 1.5) < 1e-9


def test_atr_insufficient_data():
    assert indicators.atr([1, 2], [0, 1], [1, 2], period=14) is None


def test_adx_insufficient_data():
    closes = list(range(10))
    highs = [c + 1 for c in closes]
    lows = [c - 1 for c in closes]
    assert indicators.adx(highs, lows, closes, period=14) is None


def test_adx_high_for_strong_trend():
    closes = list(range(1, 40))  # steady uptrend
    highs = [c + 0.5 for c in closes]
    lows = [c - 0.5 for c in closes]
    val = indicators.adx(highs, lows, closes, period=14)
    assert val is not None and 0 <= val <= 100 and val > 40


def test_adx_low_for_chop():
    closes, price = [], 100.0
    for i in range(60):
        price += 1 if i % 2 == 0 else -1
        closes.append(price)
    highs = [c + 0.5 for c in closes]
    lows = [c - 0.5 for c in closes]
    trend = indicators.adx(
        [c + 0.5 for c in range(1, 40)], [c - 0.5 for c in range(1, 40)], list(range(1, 40)), 14
    )
    chop = indicators.adx(highs, lows, closes, 14)
    assert chop is not None and chop < trend
