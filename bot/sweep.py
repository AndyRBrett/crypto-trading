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


# ── WALK-FORWARD ─────────────────────────────────────────────────────────────
# `holdout` answers "does this setting survive one unseen tail?". That is one
# draw from a very noisy distribution: move the split by a week and the winner
# often changes, which is exactly how a curve fit passes a holdout and then
# loses money live. Walk-forward asks the harder question — "if I had re-tuned
# on a schedule and traded only what the tuning chose, what would I have made?"
# Each fold optimises on its training window and is then scored on the window
# immediately after it, which the optimiser never saw. Chaining those
# out-of-sample segments is the closest offline analogue of actually running it.
#
# The critical difference from a sweep: a sweep reports in AND out for every
# combo, so it is tempting to read the out-of-sample column of the best
# in-sample row. Walk-forward SELECTS per fold and scores only the selection —
# no peeking, because the choice is made before the test window is touched.

# Minimum folds for a walk-forward to say anything. Below this the result is one
# or two draws again and inherits the very noise this is meant to average out.
MIN_FOLDS = 3


@dataclass
class WalkForwardFold:
    index: int
    train_bars: int
    test_bars: int
    chosen: dict                      # params the optimiser picked on train
    in_sample: BacktestResult         # the chosen params over the train window
    out_sample: BacktestResult        # the SAME params over the unseen test window

    def line(self) -> str:
        params = "  ".join(f"{k}={v}" for k, v in self.chosen.items())
        return (
            f"fold {self.index:>2}  train {self.train_bars:>5}b  test {self.test_bars:>4}b  "
            f"in {self.in_sample.total_return_pct:+6.2f}%  "
            f"out {self.out_sample.total_return_pct:+6.2f}%  "
            f"tr {self.out_sample.num_trades:>2}   [{params}]"
        )


@dataclass
class WalkForwardResult:
    folds: list[WalkForwardFold]
    anchored: bool

    @property
    def oos_return_pct(self) -> float:
        """Compounded out-of-sample return across folds.

        Compounded, not summed: the folds are sequential and each one trades the
        equity the previous one left behind. Summing would overstate a losing
        sequence and understate a winning one.
        """
        equity = 1.0
        for f in self.folds:
            equity *= 1 + f.out_sample.total_return_pct / 100
        return (equity - 1) * 100

    @property
    def oos_trades(self) -> int:
        return sum(f.out_sample.num_trades for f in self.folds)

    @property
    def oos_win_rate(self) -> float:
        wins = sum(f.out_sample.wins for f in self.folds)
        losses = sum(f.out_sample.losses for f in self.folds)
        return wins / (wins + losses) if (wins + losses) else 0.0

    @property
    def worst_fold_pct(self) -> float:
        return min((f.out_sample.total_return_pct for f in self.folds), default=0.0)

    @property
    def positive_folds(self) -> int:
        return sum(1 for f in self.folds if f.out_sample.total_return_pct > 0)

    @property
    def param_stability(self) -> float:
        """Fraction of folds that re-picked the single most common parameter set.

        The tell that separates a real edge from a curve fit. A strategy whose
        optimum jumps to a different corner of the grid every fold has no stable
        setting to trade — a high average return built on that is an artifact of
        re-tuning, not something you could have captured.
        """
        if not self.folds:
            return 0.0
        counts: dict[str, int] = {}
        for f in self.folds:
            key = repr(sorted(f.chosen.items()))
            counts[key] = counts.get(key, 0) + 1
        return max(counts.values()) / len(self.folds)

    def summary(self) -> str:
        return (
            f"walk-forward ({'anchored' if self.anchored else 'rolling'}, "
            f"{len(self.folds)} folds)\n"
            f"  out-of-sample return  {self.oos_return_pct:+.2f}% (compounded)\n"
            f"  trades / win rate     {self.oos_trades} / {self.oos_win_rate*100:.0f}%\n"
            f"  positive folds        {self.positive_folds}/{len(self.folds)}\n"
            f"  worst fold            {self.worst_fold_pct:+.2f}%\n"
            f"  parameter stability   {self.param_stability*100:.0f}% "
            f"(share of folds re-picking the same settings)"
        )


def walk_forward(
    strategy_type: str,
    candles: Sequence[dict],
    base_config,
    grid: dict[str, list] | None = None,
    product_id: str = "BTC-USD",
    folds: int = 5,
    anchored: bool = False,
    progress=None,
) -> WalkForwardResult:
    """Rolling-origin walk-forward analysis.

    Splits the series into ``folds`` sequential test windows. For each, the grid
    is optimised on everything before it (``anchored``: from the very start;
    otherwise: a rolling window the same length as the training block) and the
    winning parameters alone are then scored on the test window.

    ``progress``: optional callable(done, total_folds).

    Raises ValueError if the series cannot support ``folds`` usable splits —
    silently returning fewer would let a two-fold result be read as a five-fold
    one.
    """
    candles = list(candles)
    n = len(candles)
    if folds < MIN_FOLDS:
        raise ValueError(f"folds must be >= {MIN_FOLDS}; got {folds}")

    # One test block per fold, with the first block reserved as the initial
    # training set — so a k-fold run needs k+1 blocks.
    block = n // (folds + 1)
    probe = make_strategy(strategy_type, base_config.strategy)
    min_c = probe.min_candles()
    if block <= min_c:
        raise ValueError(
            f"series too short for {folds} folds: each block is {block} bars but "
            f"{strategy_type} needs more than {min_c} to produce a signal. "
            f"Use fewer folds or a longer history."
        )

    out: list[WalkForwardFold] = []
    for i in range(folds):
        test_start = block * (i + 1)
        test_end = test_start + block if i < folds - 1 else n
        train_start = 0 if anchored else max(0, test_start - block)
        train = candles[train_start:test_start]
        test = candles[test_start:test_end]
        if len(train) <= min_c or len(test) <= min_c:
            continue

        # Optimise on train only. holdout=0: the test window is this fold's
        # out-of-sample, so carving another one out of train would shrink the
        # fit for no benefit.
        ranked = sweep(strategy_type, train, base_config, grid=grid,
                       product_id=product_id, holdout=0.0)
        if not ranked:
            continue
        best = ranked[0]

        # Score the chosen params on the unseen block, prepending warmup bars so
        # the strategy enters the window spun up — same convention _run_combo
        # uses for holdout, so trades still occur only inside the test region.
        cfg, scfg = _apply(base_config, best.params)
        strat = make_strategy(strategy_type, scfg)
        warm = train[-strat.min_candles():]
        oos = run_backtest(strat, warm + test, cfg, product_id)

        out.append(WalkForwardFold(
            index=i + 1, train_bars=len(train), test_bars=len(test),
            chosen=best.params, in_sample=best.in_sample, out_sample=oos,
        ))
        if progress is not None:
            progress(i + 1, folds)

    if len(out) < MIN_FOLDS:
        raise ValueError(
            f"only {len(out)} usable folds from {n} bars — need at least "
            f"{MIN_FOLDS}. Use a longer history or fewer folds."
        )
    return WalkForwardResult(folds=out, anchored=anchored)
