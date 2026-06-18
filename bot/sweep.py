"""Parameter sweep over a strategy's settings.

Runs a grid of parameter combinations through the backtester and ranks them, so
you can find settings with real post-fee edge instead of eyeballing one config
at a time.

Guard against overfitting built in: with a ``holdout`` fraction, each combo is
optimized on the in-sample head of the series and *also* evaluated on the
held-out tail it never saw. A setting that's only good in-sample is a curve fit;
one that holds up out-of-sample is a candidate. Always judge by the holdout
column, not the in-sample ranking.
"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass
from itertools import product
from multiprocessing import Pool
from typing import Sequence

from .backtest import BacktestResult, run_backtest
from .strategy import StrategyConfig
from .strategies import make_strategy

# Which grid keys are strategy-level vs engine/risk-level config fields.
_STRATEGY_FIELDS = {f.name for f in dataclasses.fields(StrategyConfig)}
_CONFIG_RISK_FIELDS = {
    "fee_rate", "risk_per_trade_pct", "max_position_pct",
    "stop_loss_atr_mult", "take_profit_atr_mult", "trailing_stop", "fallback_stop_pct",
}

# Sensible starting grids per strategy. Keep them modest — combinations multiply.
DEFAULT_GRIDS: dict[str, dict[str, list]] = {
    "ema_crossover": {
        "fast_period": [10, 20, 30],
        "slow_period": [50, 100],
        "adx_min": [15, 20, 25],
        "rsi_overbought": [70, 85, 100],   # 100 ≈ "disable the overbought gate"
        "stop_loss_atr_mult": [2.0, 3.0],
        "take_profit_atr_mult": [3.0, 4.0, 6.0],
    },
    "rsi_mean_reversion": {
        "rsi_period": [7, 14],
        "rsi_mr_oversold": [20, 25, 30],
        "rsi_mr_overbought": [50, 55, 60],
        "stop_loss_atr_mult": [2.0, 3.0],
        "take_profit_atr_mult": [3.0, 4.0],
    },
    "donchian_breakout": {
        "donchian_period": [20, 30, 55],
        "donchian_exit_period": [10, 20],
        "stop_loss_atr_mult": [2.0, 3.0],
        "take_profit_atr_mult": [4.0, 6.0],
    },
}


@dataclass
class SweepRun:
    params: dict
    in_sample: BacktestResult
    holdout: BacktestResult | None  # out-of-sample; None when holdout==0

    def line(self) -> str:
        params = "  ".join(f"{k}={v}" for k, v in self.params.items())
        s = self.in_sample
        out = (
            f"in: net {s.total_return_pct:+6.2f}% gross {s.gross_return_pct:+6.2f}% "
            f"tr {s.num_trades:>2} win {s.win_rate*100:3.0f}%"
        )
        if self.holdout is not None:
            h = self.holdout
            out += (
                f"  ||  out: net {h.total_return_pct:+6.2f}% "
                f"tr {h.num_trades:>2} win {h.win_rate*100:3.0f}%"
            )
        return f"{out}   [{params}]"


def _invalid(params: dict) -> bool:
    """Reject nonsensical combinations (e.g. fast MA slower than the slow MA)."""
    if params.get("fast_period", 0) and params.get("slow_period", 0):
        if params["fast_period"] >= params["slow_period"]:
            return True
    if params.get("donchian_exit_period", 0) and params.get("donchian_period", 0):
        if params["donchian_exit_period"] > params["donchian_period"]:
            return True
    return False


def _apply(base_config, params: dict):
    strat_over = {k: v for k, v in params.items() if k in _STRATEGY_FIELDS}
    cfg_over = {k: v for k, v in params.items() if k in _CONFIG_RISK_FIELDS}
    scfg = dataclasses.replace(base_config.strategy, **strat_over)
    cfg = dataclasses.replace(base_config, **cfg_over)
    return cfg, scfg


# Per-worker shared state (set once per process to avoid re-pickling the candle
# series for every combo). Holds (strategy_type, train, test, base_config, product_id).
_JOB: tuple = ()


def _init_worker(job: tuple) -> None:
    global _JOB
    _JOB = job


def _run_combo(params: dict) -> "SweepRun | None":
    strategy_type, train, test, base_config, product_id = _JOB
    cfg, scfg = _apply(base_config, params)
    strat = make_strategy(strategy_type, scfg)
    if len(train) <= strat.min_candles():
        return None
    in_sample = run_backtest(strat, train, cfg, product_id)
    holdout_res = None
    if test is not None:
        # Prepend warmup bars so the strategy is "spun up" entering the test
        # window — trades then happen only in the held-out region.
        warm = train[-strat.min_candles():]
        window = warm + test
        if len(window) > strat.min_candles():
            holdout_res = run_backtest(strat, window, cfg, product_id)
    return SweepRun(params, in_sample, holdout_res)


def sweep(
    strategy_type: str,
    candles: Sequence[dict],
    base_config,
    grid: dict[str, list] | None = None,
    product_id: str = "BTC-USD",
    holdout: float = 0.0,
    progress=None,
) -> list[SweepRun]:
    """Backtest every combination in ``grid``; return runs sorted by in-sample net return.

    ``holdout`` (0..1): fraction of the series held out for out-of-sample scoring.
    ``progress``: optional callable(done, total) invoked per combo for UI feedback.
    """
    grid = grid if grid is not None else DEFAULT_GRIDS.get(strategy_type)
    if not grid:
        raise ValueError(f"no grid for strategy_type {strategy_type!r}; pass one explicitly")

    candles = list(candles)
    n = len(candles)
    if 0.0 < holdout < 1.0:
        split = int(n * (1 - holdout))
        train, test = candles[:split], candles[split:]
    else:
        train, test = candles, None

    keys = list(grid)
    combos = [
        params
        for combo in product(*(grid[k] for k in keys))
        if not _invalid(params := dict(zip(keys, combo)))
    ]
    total = len(combos)

    job = (strategy_type, train, test, base_config, product_id)
    runs: list[SweepRun] = []

    # Combos are independent, and each is hundreds of pure-Python backtests, so
    # fan them out across cores. Serial for small grids (pool overhead isn't
    # worth it, and it keeps tests simple).
    workers = min(os.cpu_count() or 1, 8)
    if workers > 1 and total > 16:
        with Pool(workers, initializer=_init_worker, initargs=(job,)) as pool:
            for done, res in enumerate(pool.imap_unordered(_run_combo, combos, chunksize=4), 1):
                if progress is not None:
                    progress(done, total)
                if res is not None:
                    runs.append(res)
    else:
        _init_worker(job)
        for done, params in enumerate(combos, 1):
            if progress is not None:
                progress(done, total)
            res = _run_combo(params)
            if res is not None:
                runs.append(res)

    runs.sort(key=lambda r: r.in_sample.total_return_pct, reverse=True)
    return runs
