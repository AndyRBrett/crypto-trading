"""Risk-adjusted performance metrics from an equity curve.

A bare P&L number — say a 30-day −$453 — isn't interpretable on its own: is it
within normal variance for this book, or a real regression? Sharpe, Sortino and
max drawdown put that number in context by scaling return against the risk taken
to earn it. They are computed straight from the already-persisted equity curve
(the ``equity`` table the bot snapshots every tick), so this adds a read-only
interpretation layer with no new data collection.

Conventions (documented so the numbers are reproducible and comparable):

* **Lookback window:** 30 days by default. It matches the headline 30-day P&L the
  overseer already tracks, and is long enough to estimate variance while still
  reflecting the current regime rather than ancient history. The window actually
  used is reported back in the result as ``window_days``.
* **Sampling → daily returns:** the bot snapshots equity irregularly (ticks at
  most hourly, sometimes with multi-hour gaps), so we resample to one observation
  per UTC calendar day — the last equity recorded that day — and take simple
  returns between consecutive days. Resampling to a fixed frequency is what makes
  the annualization factor well defined.
* **Annualization:** crypto trades 24/7 with no market-closed days, so daily
  figures are annualized with the 365-day convention — mean return × 365 and
  standard deviation × √365 (so Sharpe/Sortino scale by √365).
* **Risk-free rate:** 0. This is a paper-trading book with no financing leg, so
  excess return equals return; stating it so the assumption is explicit rather
  than implied.
* **Sortino downside deviation:** root-mean-square of the *negative* daily
  returns against a 0% target, averaged over *all* observations (the standard
  target-downside-deviation definition), then annualized by √365.
* **Max drawdown:** the largest peak-to-trough decline in equity over the window,
  measured on the full-resolution curve (not the daily resample, so an intraday
  trough still counts), reported as a positive percentage magnitude.

Metrics that can't be meaningfully computed are omitted rather than invented:
Sharpe/Sortino/volatility need at least two daily returns with non-zero
dispersion, and Sortino additionally needs at least one losing day (otherwise
there is no downside to divide by). A flat equity curve therefore yields a 0%
max drawdown and no Sharpe — which is the honest answer, not a bug.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

# Crypto trades 24/7 — every calendar day is a trading day, so daily statistics
# annualize with 365 (× for the mean, √ for the deviation).
TRADING_DAYS_PER_YEAR = 365
RISK_WINDOW_DAYS = 30


def _resample_daily(curve: list[tuple[float, float]]) -> list[float]:
    """Last equity value of each UTC calendar day, in chronological order.

    ``curve`` is ``[(timestamp, equity), ...]`` sorted ascending; a later
    timestamp on the same day overwrites the earlier one, leaving the day's
    closing equity.
    """
    by_day: dict[str, float] = {}
    for ts, equity in curve:
        day = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
        by_day[day] = equity
    return [by_day[d] for d in sorted(by_day)]


def daily_returns(curve: list[tuple[float, float]]) -> list[float]:
    """Simple day-over-day returns from the daily-resampled equity curve."""
    eqs = _resample_daily(curve)
    out = []
    for prev, cur in zip(eqs, eqs[1:]):
        if prev > 0:
            out.append(cur / prev - 1.0)
    return out


def max_drawdown(curve: list[tuple[float, float]]) -> float:
    """Largest peak-to-trough decline as a non-positive fraction (e.g. -0.12).

    Walks the full-resolution curve tracking the running peak; the drawdown at
    each point is ``equity / peak - 1`` and the worst (most negative) is kept.
    """
    peak: float | None = None
    worst = 0.0
    for _ts, equity in curve:
        if peak is None or equity > peak:
            peak = equity
        if peak and peak > 0:
            dd = equity / peak - 1.0
            if dd < worst:
                worst = dd
    return worst


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def _stdev(xs: list[float], ddof: int = 1) -> float:
    """Sample standard deviation (ddof=1); 0.0 when there's too little data."""
    n = len(xs)
    if n - ddof <= 0:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - ddof))


def risk_metrics(
    curve: list[tuple[float, float]],
    window_days: int = RISK_WINDOW_DAYS,
    now: float | None = None,
) -> dict:
    """Risk-adjusted metrics over the trailing ``window_days`` of an equity curve.

    ``curve`` is ``[(timestamp, equity), ...]`` (any order); it is sorted and
    clipped to the window before computing. Returns a dict with whichever of
    ``sharpe`` / ``sortino`` / ``max_drawdown_pct`` / ``volatility_pct`` could be
    computed, plus ``window_days`` and the ``samples`` (daily-return count) the
    ratios are based on. Returns ``{}`` when the window holds fewer than two
    equity snapshots (nothing to measure).
    """
    curve = sorted(curve)
    if now is not None:
        start = now - window_days * 86_400
        curve = [(ts, eq) for ts, eq in curve if ts >= start]
    if len(curve) < 2:
        return {}

    rets = daily_returns(curve)
    out: dict = {"window_days": window_days, "samples": len(rets)}

    # Max drawdown is defined for any curve of >= 2 points.
    out["max_drawdown_pct"] = round(abs(max_drawdown(curve)) * 100, 2)

    if len(rets) >= 2:
        ann = math.sqrt(TRADING_DAYS_PER_YEAR)
        mean = _mean(rets)
        sd = _stdev(rets)
        out["volatility_pct"] = round(sd * ann * 100, 2)
        if sd > 0:
            out["sharpe"] = round(mean / sd * ann, 2)
        # Downside deviation: RMS of the negative returns over all observations.
        downside = math.sqrt(sum(min(r, 0.0) ** 2 for r in rets) / len(rets))
        if downside > 0:
            out["sortino"] = round(mean / downside * ann, 2)

    return out
