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


def true_ranges(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]
) -> list[float]:
    """Per-bar true range: max(high-low, |high-prevClose|, |low-prevClose|)."""
    trs: list[float] = []
    for i in range(1, len(closes)):
        h, l, prev_close = highs[i], lows[i], closes[i - 1]
        trs.append(max(h - l, abs(h - prev_close), abs(l - prev_close)))
    return trs


def atr(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> float | None:
    """Average True Range (Wilder smoothing). A volatility measure in price units.

    Used for stop distances and position sizing. Returns None without enough data.
    """
    if period <= 0 or len(closes) < period + 1:
        return None
    trs = true_ranges(highs, lows, closes)
    current = sum(trs[:period]) / period
    for tr in trs[period:]:
        current = (current * (period - 1) + tr) / period
    return current


def adx(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> float | None:
    """Average Directional Index (Wilder). Measures *trend strength* in [0, 100].

    Low ADX (< ~20) means range-bound/choppy; high ADX means a strong trend.
    Returns None without enough data (needs ~2*period bars).
    """
    n = len(closes)
    if period <= 0 or n < 2 * period + 1:
        return None

    plus_dm: list[float] = []
    minus_dm: list[float] = []
    trs: list[float] = []
    for i in range(1, n):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0.0)
        h, l, prev_close = highs[i], lows[i], closes[i - 1]
        trs.append(max(h - l, abs(h - prev_close), abs(l - prev_close)))

    def _wilder(values: list[float]) -> list[float]:
        running = sum(values[:period])
        out = [running]
        for v in values[period:]:
            running = running - running / period + v
            out.append(running)
        return out

    tr_s = _wilder(trs)
    pdm_s = _wilder(plus_dm)
    mdm_s = _wilder(minus_dm)

    dxs: list[float] = []
    for tr_v, pdm_v, mdm_v in zip(tr_s, pdm_s, mdm_s):
        if tr_v == 0:
            dxs.append(0.0)
            continue
        plus_di = 100 * pdm_v / tr_v
        minus_di = 100 * mdm_v / tr_v
        denom = plus_di + minus_di
        dxs.append(100 * abs(plus_di - minus_di) / denom if denom else 0.0)

    if len(dxs) < period:
        return None
    current = sum(dxs[:period]) / period
    for dx in dxs[period:]:
        current = (current * (period - 1) + dx) / period
    return current
