"""Tests for the parameter sweep."""

from bot.config import Config
from bot.sweep import sweep


def _candles(closes):
    return [
        {"time": 1700000000 + i * 3600, "open": c, "high": c * 1.01, "low": c * 0.99, "close": c}
        for i, c in enumerate(closes)
    ]


def test_sweep_ranks_and_skips_invalid_combos():
    cfg = Config(starting_cash=10_000, fee_rate=0.006)
    closes = [100 + i + 3 * (i % 7) for i in range(160)]
    grid = {
        "fast_period": [5, 10, 50],
        "slow_period": [10, 20],          # 50>=10 etc. -> some invalid combos
        "trend_filter": [False],
        "adx_filter": [False],
        "rsi_overbought": [100],
    }
    runs = sweep("ema_crossover", _candles(closes), cfg, grid=grid, product_id="BTC-USD")
    assert runs, "expected at least one valid combo"
    # fast must be < slow in every surviving combo.
    assert all(r.params["fast_period"] < r.params["slow_period"] for r in runs)
    # Sorted by in-sample net return, descending.
    rets = [r.in_sample.total_return_pct for r in runs]
    assert rets == sorted(rets, reverse=True)
    # No holdout requested -> no out-of-sample result.
    assert all(r.holdout is None for r in runs)


def test_sweep_holdout_populates_out_of_sample():
    cfg = Config(starting_cash=10_000, fee_rate=0.006)
    closes = [100 + i + 3 * (i % 5) for i in range(300)]
    grid = {
        "fast_period": [5, 10],
        "slow_period": [20],
        "trend_filter": [False],
        "adx_filter": [False],
        "rsi_overbought": [100],
    }
    runs = sweep("ema_crossover", _candles(closes), cfg, grid=grid,
                 product_id="BTC-USD", holdout=0.3)
    assert runs
    assert all(r.holdout is not None for r in runs)
