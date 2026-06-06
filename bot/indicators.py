"""Technical indicators.

Pure functions over lists of closing prices, ordered oldest -> newest.
Each returns the *latest* indicator value (a float), or None when there is
not enough data. Keeping these pure makes them trivial to unit test.
"""

from __future__ import annotations

from typing import Sequence


def sma(values: Sequence[float], period: int) -> float | None:
    """Simple moving average of the last ``period`` values."""
    if period <= 0 or len(values) < period:
        return None
    window = values[-period:]
    return sum(window) / period


def ema(values: Sequence[float], period: int) -> float | None:
    """Exponential moving average of the last ``period``-weighted values.

    Seeded with the SMA of the first ``period`` values, then smoothed across
    the remainder. Returns None if there are fewer than ``period`` values.
    """
    if period <= 0 or len(values) < period:
        return None
    k = 2 / (period + 1)
    # Seed with the SMA of the first window.
    current = sum(values[:period]) / period
    for price in values[period:]:
        current = price * k + current * (1 - k)
    return current


def rsi(values: Sequence[float], period: int = 14) -> float | None:
    """Wilder's Relative Strength Index over the last ``period`` deltas.

    Returns a value in [0, 100], or None if there is insufficient data.
    """
    if period <= 0 or len(values) < period + 1:
        return None

    gains = 0.0
    losses = 0.0
    # Initial average gain/loss over the first ``period`` deltas.
    for i in range(1, period + 1):
        delta = values[i] - values[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    avg_gain = gains / period
    avg_loss = losses / period

    # Wilder smoothing across the remaining deltas.
    for i in range(period + 1, len(values)):
        delta = values[i] - values[i - 1]
        gain = delta if delta > 0 else 0.0
        loss = -delta if delta < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1 + rs))
