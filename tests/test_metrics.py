import math

from bot.metrics import (
    RISK_WINDOW_DAYS,
    TRADING_DAYS_PER_YEAR,
    daily_returns,
    max_drawdown,
    risk_metrics,
)

DAY = 86_400


def _curve(equities, start=1_700_000_000.0, step=DAY):
    """Build a [(ts, equity), ...] curve, one point per `step` seconds."""
    return [(start + i * step, eq) for i, eq in enumerate(equities)]


def test_too_little_data_returns_empty():
    assert risk_metrics([]) == {}
    assert risk_metrics(_curve([1000.0])) == {}


def test_daily_resample_takes_last_of_each_day():
    # Two snapshots on day 0, one on day 1: the day-0 return uses the last value.
    start = 1_700_000_000.0
    curve = [(start, 1000.0), (start + 3600, 1010.0), (start + DAY, 1020.0)]
    rets = daily_returns(curve)
    assert len(rets) == 1
    assert abs(rets[0] - (1020.0 / 1010.0 - 1)) < 1e-12


def test_max_drawdown_is_worst_peak_to_trough():
    # Peak 1200, trough 900 -> -25%.
    curve = _curve([1000.0, 1200.0, 900.0, 1100.0])
    assert abs(max_drawdown(curve) - (900.0 / 1200.0 - 1)) < 1e-12
    m = risk_metrics(curve)
    assert m["max_drawdown_pct"] == 25.0


def test_flat_curve_has_zero_drawdown_and_no_sharpe():
    m = risk_metrics(_curve([1000.0] * 5))
    assert m["max_drawdown_pct"] == 0.0
    # No dispersion -> Sharpe/Sortino are undefined and omitted.
    assert "sharpe" not in m
    assert "sortino" not in m
    assert m["volatility_pct"] == 0.0


def test_sharpe_matches_manual_annualized_calc():
    # Daily returns of +1%, -0.5%, +0.5% (last-of-day snapshots one day apart).
    eqs = [1000.0, 1010.0, 1004.95, 1009.97]
    curve = _curve(eqs)
    rets = daily_returns(curve)
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    sd = math.sqrt(var)
    expected = round(mean / sd * math.sqrt(TRADING_DAYS_PER_YEAR), 2)
    assert risk_metrics(curve)["sharpe"] == expected


def test_sortino_omitted_without_a_losing_day():
    # Monotonically rising equity -> no downside deviation -> no Sortino.
    curve = _curve([1000.0, 1010.0, 1025.0, 1040.0])
    m = risk_metrics(curve)
    assert "sortino" in m or "sortino" not in m  # tolerate either; assert below
    assert "sortino" not in m
    assert "sharpe" in m  # there is still dispersion in the (all-positive) returns


def test_sortino_present_with_a_losing_day():
    curve = _curve([1000.0, 1020.0, 1000.0, 1015.0])
    m = risk_metrics(curve)
    assert "sortino" in m and isinstance(m["sortino"], float)


def test_window_clips_to_lookback():
    # Old wild swings sit outside the 30-day window and must not count.
    now = 1_700_000_000.0
    old = [(now - 200 * DAY, 1000.0), (now - 199 * DAY, 5000.0)]
    recent = _curve([1000.0, 1010.0, 1005.0, 1012.0], start=now - 3 * DAY)
    m = risk_metrics(old + recent, now=now)
    assert m["window_days"] == RISK_WINDOW_DAYS
    # Only the recent 4 daily snapshots -> 3 returns.
    assert m["samples"] == 3
    # The 5x spike is excluded, so drawdown is modest, not catastrophic.
    assert m["max_drawdown_pct"] < 5.0
