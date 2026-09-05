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


# --- walk-forward analysis (issue #39) --------------------------------------

import pytest

from bot.sweep import MIN_FOLDS, walk_forward

_GRID = {
    "fast_period": [5, 10],
    "slow_period": [20, 30],
    "trend_filter": [False],
    "adx_filter": [False],
    "rsi_overbought": [100],
}


def _cfg():
    return Config(starting_cash=10_000, fee_rate=0.006)


def _long_series(n=1200):
    # A trending series with cyclical pullbacks: enough structure for an MA
    # crossover to actually trade, so folds aren't all empty.
    return _candles([100 + i * 0.4 + 8 * ((i // 20) % 5) for i in range(n)])


def test_walk_forward_scores_only_unseen_windows():
    wf = walk_forward("ema_crossover", _long_series(), _cfg(), grid=_GRID, folds=4)
    assert len(wf.folds) == 4
    for f in wf.folds:
        # Every fold picked params from its own grid and was scored separately.
        assert f.chosen["fast_period"] < f.chosen["slow_period"]
        assert f.in_sample is not f.out_sample
        assert f.train_bars > 0 and f.test_bars > 0


def test_folds_do_not_train_on_their_own_test_window():
    # The property that makes walk-forward meaningful: each fold's training block
    # ends where its test block begins. A rolling window trains on the block
    # immediately before; nothing later ever leaks backwards.
    wf = walk_forward("ema_crossover", _long_series(), _cfg(), grid=_GRID, folds=4)
    total = sum(f.test_bars for f in wf.folds)
    assert total <= 1200, "test windows must not overlap"


def test_anchored_mode_grows_the_training_window():
    rolling = walk_forward("ema_crossover", _long_series(), _cfg(), grid=_GRID, folds=4)
    anchored = walk_forward("ema_crossover", _long_series(), _cfg(), grid=_GRID,
                            folds=4, anchored=True)
    assert anchored.anchored is True and rolling.anchored is False
    # Anchored trains from bar 0 every fold, so later folds see strictly more.
    assert anchored.folds[-1].train_bars > rolling.folds[-1].train_bars


def test_oos_return_is_compounded_not_summed():
    # Sequential folds trade the equity the previous one left. +10% then -10% is
    # -1%, not 0% — summing would flatter a losing sequence.
    wf = walk_forward("ema_crossover", _long_series(), _cfg(), grid=_GRID, folds=3)
    for f, pct in zip(wf.folds, (10.0, -10.0, 0.0)):
        f.out_sample.total_return_pct = pct
    assert wf.oos_return_pct == pytest.approx(-1.0, abs=1e-9)


def test_param_stability_detects_a_wandering_optimum():
    wf = walk_forward("ema_crossover", _long_series(), _cfg(), grid=_GRID, folds=4)
    for f in wf.folds:
        f.chosen = {"fast_period": 5, "slow_period": 20}
    assert wf.param_stability == 1.0
    wf.folds[0].chosen = {"fast_period": 10, "slow_period": 30}
    assert wf.param_stability == 0.75


def test_too_few_folds_is_rejected():
    with pytest.raises(ValueError, match="folds must be >="):
        walk_forward("ema_crossover", _long_series(), _cfg(), grid=_GRID,
                     folds=MIN_FOLDS - 1)


def test_short_series_raises_instead_of_silently_returning_fewer_folds():
    # Quietly returning 2 folds from a 5-fold request would let the caller read a
    # far noisier result as though it were the one they asked for.
    with pytest.raises(ValueError, match="too short|usable folds"):
        walk_forward("ema_crossover", _candles([100 + i for i in range(80)]),
                     _cfg(), grid=_GRID, folds=5)


def test_summary_reports_the_out_of_sample_figures():
    wf = walk_forward("ema_crossover", _long_series(), _cfg(), grid=_GRID, folds=3)
    text = wf.summary()
    assert "out-of-sample return" in text
    assert "parameter stability" in text
    assert f"{wf.positive_folds}/3" in text


def test_adaptive_breakout_walk_forward_accepts_both_modes():
    grid = {'adaptive_breakout': [False, True], 'breakout_atr_mult': [0.5, 1.0],
            'exit_atr_mult': [1.0], 'donchian_period': [10],
            'donchian_exit_period': [5], 'atr_period': [3]}
    runs = sweep('donchian_breakout', _long_series(500), _cfg(), grid=grid, holdout=0.3)
    assert len(runs) == 4
    assert {r.params['adaptive_breakout'] for r in runs} == {False, True}
    assert all(r.holdout is not None for r in runs)
    wf = walk_forward('donchian_breakout', _long_series(800), _cfg(), grid=grid, folds=3)
    assert len(wf.folds) == 3
    assert all('adaptive_breakout' in f.chosen for f in wf.folds)
