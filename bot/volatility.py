"""Realized-volatility estimates and the vol-target sizing bound.

Issue #53. The risk sizer already divides by ATR, so it *looks* volatility-aware
— but that only holds while the risk bound is the binding one. At live settings
it frequently isn't: with a $50k book, 1% risk and a 2-ATR stop, a 1.5%-ATR
asset sizes to a $16.7k risk bound against a $15k equity cap, so the flat
``max_position_pct`` wins and the position stops scaling with volatility at all.
Two assets at very different vol then take the same notional.

This module adds the missing bound: the notional whose expected annualized
volatility contribution equals a target share of equity —

    notional = equity * vol_target_pct / annualized_vol(asset)

which is textbook volatility targeting. It enters ``risk.position_size`` as one
more ``min()`` term, so it can only ever *reduce* a position, never enlarge one.
That is deliberate. A strict vol-target would size *up* in calm regimes, above
``max_position_pct``, and a book that does that is exactly the book that gets
hurt when a quiet regime ends — the equity cap stays the backstop, and the vol
bound tightens sizing in the volatile regimes where the cap is too generous.

Estimating the volatility:

* **Preferred:** the sample standard deviation of the last ``vol_lookback_bars``
  simple returns, annualized by the square root of the number of bars in a year
  (derived from the candle granularity, 365-day 24/7 crypto convention — the same
  convention ``bot/metrics.py`` uses, so the numbers are comparable).
* **Fallback:** ``ATR / price`` as the per-bar move, annualized the same way,
  used when there isn't enough close history. A true range runs wider than a
  standard deviation, so this reads high — which sizes *smaller*, the safe
  direction for a fallback.
* Neither available -> no bound at all, and sizing behaves exactly as before.
"""

from __future__ import annotations

import math
from typing import Sequence

# Crypto trades 24/7; a year is 365 days of bars (matches bot/metrics.py).
SECONDS_PER_YEAR = 365 * 86_400


def bars_per_year(seconds_per_bar: float | None) -> float | None:
    """How many bars of this length fit in a 365-day year."""
    if not seconds_per_bar or seconds_per_bar <= 0:
        return None
    return SECONDS_PER_YEAR / float(seconds_per_bar)


def realized_vol(
    closes: Sequence[float], lookback: int, per_year: float | None
) -> float | None:
    """Annualized standard deviation of the last ``lookback`` bar returns.

    Returns None when there aren't at least two usable returns or the series has
    no dispersion — an unmeasurable volatility must not become a zero one, which
    would divide into an unbounded position size.
    """
    if not per_year or lookback < 2:
        return None
    window = [float(c) for c in closes[-(lookback + 1):]]
    rets = [
        cur / prev - 1.0
        for prev, cur in zip(window, window[1:])
        if prev > 0
    ]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    vol = math.sqrt(var) * math.sqrt(per_year)
    return vol if vol > 0 else None


def atr_vol(atr: float | None, price: float, per_year: float | None) -> float | None:
    """Annualized volatility approximated from ATR as the per-bar move."""
    if not per_year or not atr or atr <= 0 or price <= 0:
        return None
    return (atr / price) * math.sqrt(per_year)


def estimate_vol(
    cfg,
    closes: Sequence[float] | None,
    atr: float | None,
    price: float,
    seconds_per_bar: float | None,
) -> float | None:
    """Best available annualized volatility for one asset, or None if unmeasurable."""
    per_year = bars_per_year(seconds_per_bar)
    lookback = int(getattr(cfg, "vol_lookback_bars", 20) or 20)
    vol = realized_vol(closes or [], lookback, per_year)
    if vol is None:
        vol = atr_vol(atr, price, per_year)
    return vol


def vol_target_qty(cfg, equity: float, price: float, vol: float | None) -> float | None:
    """Quantity whose annualized vol contribution is ``vol_target_pct`` of equity.

    None when vol targeting is off or the volatility couldn't be estimated, which
    callers read as "no bound".
    """
    if not getattr(cfg, "vol_target_enabled", False):
        return None
    if not vol or vol <= 0 or price <= 0 or equity <= 0:
        return None
    target = float(getattr(cfg, "vol_target_pct", 0.0) or 0.0)
    if target <= 0:
        return None
    return (equity * target) / (vol * price)
