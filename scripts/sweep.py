#!/usr/bin/env python3
"""Sweep a strategy's parameters and rank them by post-fee edge.

Usage:
    python -m scripts.sweep --strategy ema_crossover --product BTC-USD
    python -m scripts.sweep --strategy donchian_breakout --product SOL-USD \
        --granularity ONE_HOUR --count 1000 --fee 0.0025 --holdout 0.3
    # override the grid for one parameter (repeatable):
    python -m scripts.sweep --strategy ema_crossover --param rsi_overbought=70,100

Optimizes on the in-sample head of the series and reports each combo's
out-of-sample (holdout) result alongside. Rank is by in-sample net return; judge
candidates by the holdout column — a combo that's only good in-sample is a curve
fit, not an edge.
"""

from __future__ import annotations

import argparse
import sys

from bot.config import Config
from bot.market_data import MarketData
from bot.sweep import DEFAULT_GRIDS, sweep


def _parse_value(s: str):
    s = s.strip()
    for cast in (int, float):
        try:
            return cast(s)
        except ValueError:
            pass
    if s.lower() in ("true", "false"):
        return s.lower() == "true"
    return s


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Sweep strategy parameters.")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--strategy", default="ema_crossover",
                        help=f"one of: {', '.join(DEFAULT_GRIDS)}")
    parser.add_argument("--product", default="BTC-USD")
    parser.add_argument("--count", type=int, default=None)
    parser.add_argument("--granularity", default=None)
    parser.add_argument("--fee", type=float, default=None, help="override fee rate")
    parser.add_argument("--holdout", type=float, default=0.3,
                        help="fraction held out for out-of-sample scoring (0 to disable)")
    parser.add_argument("--top", type=int, default=15, help="rows to print")
    parser.add_argument("--param", action="append", default=[],
                        help="override a grid axis, e.g. --param fast_period=10,20,30")
    args = parser.parse_args(argv)

    config = Config.load(args.config)
    if args.fee is not None:
        import dataclasses
        config = dataclasses.replace(config, fee_rate=args.fee)

    grid = dict(DEFAULT_GRIDS.get(args.strategy, {}))
    for spec in args.param:
        if "=" not in spec:
            parser.error(f"--param must be name=v1,v2,...  (got {spec!r})")
        name, _, values = spec.partition("=")
        grid[name.strip()] = [_parse_value(v) for v in values.split(",")]
    if not grid:
        parser.error(f"no grid for strategy {args.strategy!r}; supply --param axes")

    granularity = args.granularity or config.candle_granularity
    count = args.count or max(config.candle_count, 2000)
    market = MarketData(config)
    try:
        candles = market.get_history(args.product, granularity=granularity, count=count)
    except Exception as exc:  # pragma: no cover - network/exchange errors
        print(f"fetch failed for {args.product}: {exc}", file=sys.stderr)
        return 1

    combos = 1
    for v in grid.values():
        combos *= len(v)
    print(
        f"Sweep — {args.strategy} {args.product} {granularity} "
        f"candles={len(candles)} fee={config.fee_rate:.4f} holdout={args.holdout:.0%} "
        f"grid≈{combos} combos\n"
        f"axes: " + "  ".join(f"{k}={v}" for k, v in grid.items()) + "\n" + "-" * 100
    )

    def _progress(done, total):
        print(f"\r  scanning {done}/{total} combos…", end="", file=sys.stderr, flush=True)

    runs = sweep(args.strategy, candles, config, grid=grid,
                 product_id=args.product, holdout=args.holdout, progress=_progress)
    print("", file=sys.stderr)  # newline after the progress line
    if not runs:
        print("No valid combinations ran (check candle count vs. min_candles).")
        return 1

    for run in runs[: args.top]:
        print("  " + run.line())

    # Highlight robustness: positive both in-sample and out-of-sample.
    if args.holdout:
        robust = [
            r for r in runs
            if r.in_sample.total_return_pct > 0
            and r.holdout and r.holdout.total_return_pct > 0
        ]
        print("-" * 100)
        print(f"{len(robust)} of {len(runs)} combos are net-positive in BOTH in-sample and holdout.")
        if robust:
            best = max(robust, key=lambda r: r.holdout.total_return_pct)
            print("Best by holdout:\n  " + best.line())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
