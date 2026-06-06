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
